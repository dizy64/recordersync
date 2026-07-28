"""세션 탐색, 매칭, 렌더를 연결하는 애플리케이션 서비스."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from recordersync.audio_levels import AudioLevelPolicy, AudioLevelReport
from recordersync.matching import MatchOptions, match_video_features
from recordersync.media import (
    FFmpegTools,
    MediaError,
    VideoInfo,
    discover_audio_files,
    discover_video_files,
)
from recordersync.mix_analysis import (
    FFmpegMixAnalyzer,
    MixProfile,
    MixRecommendation,
)
from recordersync.models import AudioMatch, MatchStatus, RecordingSession
from recordersync.recommendation import RecommendationMode, recommend_mode
from recordersync.render import (
    AudioLevelRenderError,
    FFmpegRenderer,
    MixPolicy,
    RenderedOutput,
    RenderMode,
    build_render_plan,
)
from recordersync.report import MatchReport
from recordersync.sessions import group_recording_sessions

SelectionCallback = Callable[[str, tuple[Path, ...]], None]
ProgressCallback = Callable[[str, int, int, str], None]


@dataclass(frozen=True, slots=True)
class AnalysisBundle:
    sessions: tuple[RecordingSession, ...]
    videos: tuple[VideoInfo, ...]
    matches: tuple[AudioMatch, ...]

    def report(self) -> MatchReport:
        return MatchReport(sessions=self.sessions, matches=self.matches)


@dataclass(frozen=True, slots=True)
class _ProcessOptions:
    mode: RenderMode
    mix_profile: MixProfile
    recommend_mix_only: bool
    mix_policy: MixPolicy | None
    camera_audio_volume: float | None
    external_audio_volume: float | None
    external_highpass_hz: float | None
    overwrite: bool
    output_prefix: str
    output_suffix: str
    audio_level_policy: AudioLevelPolicy | None


@dataclass(frozen=True, slots=True)
class _ProcessedMatch:
    match: AudioMatch
    audio_levels: AudioLevelReport | None = None
    mix_recommendation: MixRecommendation | None = None


def _validate_process_options(options: _ProcessOptions) -> None:
    if options.mix_profile is MixProfile.AUTO:
        if options.mode is not RenderMode.MIX:
            raise ValueError("automatic mix analysis requires mix mode")
        if options.mix_policy is not None or any(
            value is not None
            for value in (
                options.camera_audio_volume,
                options.external_audio_volume,
                options.external_highpass_hz,
                options.audio_level_policy,
            )
        ):
            raise ValueError("automatic mix analysis cannot be combined with manual mix options")
    elif options.recommend_mix_only:
        raise ValueError("mix recommendation-only processing requires the auto profile")


def _failed_match(match: AudioMatch, reason: str) -> AudioMatch:
    return replace(
        match,
        status=MatchStatus.ERROR,
        reason=reason,
        output_path=None,
        segments=(),
    )


def is_renderable_match(
    match: AudioMatch,
    mode: RenderMode,
    *,
    recommended_only: bool = False,
) -> bool:
    """처리 모드와 추천 기준에 따라 매칭 결과의 렌더 허용 여부를 반환한다."""

    if match.status is MatchStatus.MATCHED:
        return True
    if match.status is not MatchStatus.PARTIAL or mode is not RenderMode.FALLBACK:
        return False
    return not recommended_only or recommend_mode(match).mode is RecommendationMode.FALLBACK


class RecorderSyncPipeline:
    """I/O 어댑터를 주입할 수 있는 배치 처리 오케스트레이터."""

    def __init__(
        self,
        tools: FFmpegTools | None = None,
        renderer: FFmpegRenderer | None = None,
        mix_analyzer: FFmpegMixAnalyzer | None = None,
    ) -> None:
        self.tools = tools or FFmpegTools()
        self.renderer = renderer or FFmpegRenderer()
        self.mix_analyzer = mix_analyzer or FFmpegMixAnalyzer()

    def analyze(
        self,
        video_dir: Path,
        audio_dir: Path,
        *,
        output_dir: Path | None = None,
        match_options: MatchOptions | None = None,
        session_gap_seconds: float = 10.0,
        selection_callback: SelectionCallback | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> AnalysisBundle:
        audio_paths = discover_audio_files(audio_dir)
        if not audio_paths:
            raise ValueError(f"No supported audio files found in: {audio_dir}")
        if selection_callback is not None:
            selection_callback("audio", tuple(audio_paths))
        resolved_output = output_dir or video_dir / "replace"
        video_paths = discover_video_files(video_dir, excluded_dirs={resolved_output})
        if not video_paths:
            raise ValueError(f"No supported video files found in: {video_dir}")
        if selection_callback is not None:
            selection_callback("video", tuple(video_paths))

        chunks = [self.tools.probe_audio(path) for path in audio_paths]
        sessions = tuple(group_recording_sessions(chunks, gap_seconds=session_gap_seconds))
        if progress_callback is not None:
            progress_callback("audio", 0, len(sessions), "")
        timelines = []
        for index, session in enumerate(sessions, start=1):
            timelines.append(self.tools.build_session_timeline(session))
            if progress_callback is not None:
                progress_callback("audio", index, len(sessions), session.id)

        videos: list[VideoInfo] = []
        matches: list[AudioMatch] = []
        if progress_callback is not None:
            progress_callback("match", 0, len(video_paths), "")
        for index, video_path in enumerate(video_paths, start=1):
            try:
                video = self.tools.probe_video(video_path)
                videos.append(video)
                if not video.has_audio:
                    matches.append(
                        AudioMatch(
                            video.path,
                            video.duration_seconds,
                            MatchStatus.ERROR,
                            reason="Camera audio is required for automatic matching",
                        )
                    )
                else:
                    features = self.tools.extract_features(video.path)
                    matches.append(
                        match_video_features(
                            video.path,
                            video.duration_seconds,
                            features,
                            timelines,
                            match_options,
                        )
                    )
            except (MediaError, ValueError) as exc:
                matches.append(
                    AudioMatch(
                        video_path,
                        0.0,
                        MatchStatus.ERROR,
                        reason=str(exc),
                    )
                )
            if progress_callback is not None:
                progress_callback("match", index, len(video_paths), video_path.name)
        return AnalysisBundle(sessions, tuple(videos), tuple(matches))

    def _recommend_mix(
        self,
        match: AudioMatch,
        video: VideoInfo,
        sessions: dict[str, RecordingSession],
        output_dir: Path,
        options: _ProcessOptions,
    ) -> MixRecommendation | None:
        if options.mix_profile is not MixProfile.AUTO:
            return None
        try:
            analysis_plan = build_render_plan(
                match,
                video,
                sessions,
                output_dir,
                mode=RenderMode.MIX,
                overwrite=options.overwrite,
                output_prefix=options.output_prefix,
                output_suffix=options.output_suffix,
            )
            return self.mix_analyzer.recommend(analysis_plan)
        except (ValueError, RuntimeError) as exc:
            return MixRecommendation.failed(f"analysis setup error: {exc}")

    def _process_match(
        self,
        match: AudioMatch,
        video: VideoInfo,
        sessions: dict[str, RecordingSession],
        output_dir: Path,
        options: _ProcessOptions,
    ) -> _ProcessedMatch:
        recommendation: MixRecommendation | None = None
        try:
            recommendation = self._recommend_mix(match, video, sessions, output_dir, options)
            if recommendation is not None:
                if not recommendation.passed or recommendation.policy is None:
                    return _ProcessedMatch(
                        _failed_match(match, "Automatic mix analysis failed"),
                        mix_recommendation=recommendation,
                    )
                if options.recommend_mix_only:
                    return _ProcessedMatch(match, mix_recommendation=recommendation)
            resolved_mix_policy = recommendation.policy if recommendation is not None else options.mix_policy
            plan = build_render_plan(
                match,
                video,
                sessions,
                output_dir,
                mode=options.mode,
                mix_policy=resolved_mix_policy,
                camera_audio_volume=options.camera_audio_volume,
                external_audio_volume=options.external_audio_volume,
                external_highpass_hz=options.external_highpass_hz,
                overwrite=options.overwrite,
                output_prefix=options.output_prefix,
                output_suffix=options.output_suffix,
                audio_level_policy=options.audio_level_policy,
            )
            rendered_output = (
                self.renderer.render_with_report(plan)
                if plan.audio_level_policy is not None
                else RenderedOutput(self.renderer.render(plan))
            )
        except AudioLevelRenderError as exc:
            return _ProcessedMatch(
                _failed_match(match, str(exc)),
                audio_levels=exc.report,
                mix_recommendation=recommendation,
            )
        except (FileExistsError, ValueError, RuntimeError) as exc:
            return _ProcessedMatch(
                _failed_match(match, str(exc)),
                mix_recommendation=recommendation,
            )
        return _ProcessedMatch(
            replace(match, output_path=rendered_output.output_path),
            audio_levels=rendered_output.audio_levels,
            mix_recommendation=(replace(recommendation, applied=True) if recommendation is not None else None),
        )

    def process(
        self,
        bundle: AnalysisBundle,
        output_dir: Path,
        *,
        mode: RenderMode = RenderMode.REPLACE,
        recommended_only: bool = False,
        mix_profile: MixProfile = MixProfile.CONSERVATIVE,
        recommend_mix_only: bool = False,
        mix_policy: MixPolicy | None = None,
        camera_audio_volume: float | None = None,
        external_audio_volume: float | None = None,
        external_highpass_hz: float | None = None,
        overwrite: bool = False,
        output_prefix: str = "",
        output_suffix: str = "",
        audio_level_policy: AudioLevelPolicy | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> MatchReport:
        options = _ProcessOptions(
            mode=mode,
            mix_profile=mix_profile,
            recommend_mix_only=recommend_mix_only,
            mix_policy=mix_policy,
            camera_audio_volume=camera_audio_volume,
            external_audio_volume=external_audio_volume,
            external_highpass_hz=external_highpass_hz,
            overwrite=overwrite,
            output_prefix=output_prefix,
            output_suffix=output_suffix,
            audio_level_policy=audio_level_policy,
        )
        _validate_process_options(options)

        sessions = {session.id: session for session in bundle.sessions}
        videos = {video.path: video for video in bundle.videos}
        processed: list[AudioMatch] = []
        audio_levels: list[AudioLevelReport | None] = []
        mix_recommendations: list[MixRecommendation | None] = []

        render_total = sum(
            is_renderable_match(match, mode, recommended_only=recommended_only) for match in bundle.matches
        )
        render_completed = 0
        progress_stage = "mix" if recommend_mix_only else "render"
        if progress_callback is not None:
            progress_callback(progress_stage, 0, render_total, "")

        for match in bundle.matches:
            if not is_renderable_match(match, mode, recommended_only=recommended_only):
                processed.append(match)
                audio_levels.append(None)
                mix_recommendations.append(None)
                continue
            video = videos.get(match.video_path)
            if video is None:
                processed.append(_failed_match(match, "Matched result is missing render metadata"))
                audio_levels.append(None)
                mix_recommendations.append(None)
            else:
                result = self._process_match(match, video, sessions, output_dir, options)
                processed.append(result.match)
                audio_levels.append(result.audio_levels)
                mix_recommendations.append(result.mix_recommendation)
            render_completed += 1
            if progress_callback is not None:
                progress_callback(
                    progress_stage,
                    render_completed,
                    render_total,
                    match.video_path.name,
                )

        return MatchReport(
            sessions=bundle.sessions,
            matches=tuple(processed),
            audio_levels=tuple(audio_levels),
            mix_recommendations=tuple(mix_recommendations),
        )
