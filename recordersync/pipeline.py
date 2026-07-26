"""세션 탐색, 매칭, 렌더를 연결하는 애플리케이션 서비스."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from recordersync.matching import MatchOptions, match_video_features
from recordersync.media import (
    FFmpegTools,
    MediaError,
    VideoInfo,
    discover_audio_files,
    discover_video_files,
)
from recordersync.models import AudioMatch, MatchStatus, RecordingSession
from recordersync.recommendation import RecommendationMode, recommend_mode
from recordersync.render import (
    FFmpegRenderer,
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
    ) -> None:
        self.tools = tools or FFmpegTools()
        self.renderer = renderer or FFmpegRenderer()

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

    def process(
        self,
        bundle: AnalysisBundle,
        output_dir: Path,
        *,
        mode: RenderMode = RenderMode.REPLACE,
        recommended_only: bool = False,
        camera_audio_volume: float | None = None,
        external_audio_volume: float = 1.0,
        overwrite: bool = False,
        output_prefix: str = "",
        output_suffix: str = "",
        progress_callback: ProgressCallback | None = None,
    ) -> MatchReport:
        sessions = {session.id: session for session in bundle.sessions}
        videos = {video.path: video for video in bundle.videos}
        processed: list[AudioMatch] = []

        render_total = sum(
            is_renderable_match(match, mode, recommended_only=recommended_only) for match in bundle.matches
        )
        render_completed = 0
        resolved_camera_volume = (
            camera_audio_volume if camera_audio_volume is not None else (1.0 if mode is RenderMode.FALLBACK else 0.1)
        )
        if progress_callback is not None:
            progress_callback("render", 0, render_total, "")

        for match in bundle.matches:
            if not is_renderable_match(match, mode, recommended_only=recommended_only):
                processed.append(match)
                continue
            video = videos.get(match.video_path)
            if video is None:
                processed.append(
                    replace(
                        match,
                        status=MatchStatus.ERROR,
                        reason="Matched result is missing render metadata",
                        output_path=None,
                        segments=(),
                    )
                )
                render_completed += 1
                if progress_callback is not None:
                    progress_callback(
                        "render",
                        render_completed,
                        render_total,
                        match.video_path.name,
                    )
                continue

            try:
                plan = build_render_plan(
                    match,
                    video,
                    sessions,
                    output_dir,
                    mode=mode,
                    camera_audio_volume=resolved_camera_volume,
                    external_audio_volume=external_audio_volume,
                    overwrite=overwrite,
                    output_prefix=output_prefix,
                    output_suffix=output_suffix,
                )
                rendered = self.renderer.render(plan)
            except (FileExistsError, ValueError, RuntimeError) as exc:
                processed.append(
                    replace(
                        match,
                        status=MatchStatus.ERROR,
                        reason=str(exc),
                        output_path=None,
                        segments=(),
                    )
                )
            else:
                processed.append(replace(match, output_path=rendered))
            render_completed += 1
            if progress_callback is not None:
                progress_callback(
                    "render",
                    render_completed,
                    render_total,
                    match.video_path.name,
                )

        return MatchReport(sessions=bundle.sessions, matches=tuple(processed))
