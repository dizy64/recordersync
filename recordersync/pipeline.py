"""세션 탐색, 매칭, 렌더를 연결하는 애플리케이션 서비스."""

from __future__ import annotations

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
from recordersync.render import (
    FFmpegRenderer,
    RenderMode,
    RenderPlan,
    resolve_output_path,
)
from recordersync.report import MatchReport
from recordersync.sessions import group_recording_sessions


@dataclass(frozen=True, slots=True)
class AnalysisBundle:
    sessions: tuple[RecordingSession, ...]
    videos: tuple[VideoInfo, ...]
    matches: tuple[AudioMatch, ...]

    def report(self) -> MatchReport:
        return MatchReport(sessions=self.sessions, matches=self.matches)


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
    ) -> AnalysisBundle:
        audio_paths = discover_audio_files(audio_dir)
        if not audio_paths:
            raise ValueError(f"No supported audio files found in: {audio_dir}")
        resolved_output = output_dir or video_dir / "replace"
        video_paths = discover_video_files(video_dir, excluded_dirs={resolved_output})
        if not video_paths:
            raise ValueError(f"No supported video files found in: {video_dir}")

        chunks = [self.tools.probe_audio(path) for path in audio_paths]
        sessions = tuple(group_recording_sessions(chunks, gap_seconds=session_gap_seconds))
        timelines = [self.tools.build_session_timeline(session) for session in sessions]

        videos: list[VideoInfo] = []
        matches: list[AudioMatch] = []
        for video_path in video_paths:
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
                    continue
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
        return AnalysisBundle(sessions, tuple(videos), tuple(matches))

    def process(
        self,
        bundle: AnalysisBundle,
        output_dir: Path,
        *,
        mode: RenderMode = RenderMode.REPLACE,
        camera_audio_volume: float = 0.1,
        overwrite: bool = False,
        output_prefix: str = "",
        output_suffix: str = "",
    ) -> MatchReport:
        sessions = {session.id: session for session in bundle.sessions}
        videos = {video.path: video for video in bundle.videos}
        processed: list[AudioMatch] = []

        for match in bundle.matches:
            if match.status is not MatchStatus.MATCHED:
                processed.append(match)
                continue
            if (
                match.session_id is None
                or match.external_start_seconds is None
                or match.session_id not in sessions
                or match.video_path not in videos
            ):
                processed.append(
                    replace(
                        match,
                        status=MatchStatus.ERROR,
                        reason="Matched result is missing render metadata",
                    )
                )
                continue

            output_path = resolve_output_path(
                match.video_path,
                output_dir,
                prefix=output_prefix,
                suffix=output_suffix,
            )
            plan = RenderPlan(
                video=videos[match.video_path],
                session=sessions[match.session_id],
                output_path=output_path,
                external_start_seconds=match.external_start_seconds,
                tempo_ratio=match.tempo_ratio,
                mode=mode,
                camera_audio_volume=camera_audio_volume,
                overwrite=overwrite,
            )
            try:
                rendered = self.renderer.render(plan)
            except (FileExistsError, ValueError, RuntimeError) as exc:
                processed.append(
                    replace(
                        match,
                        status=MatchStatus.ERROR,
                        reason=str(exc),
                    )
                )
            else:
                processed.append(replace(match, output_path=rendered))

        return MatchReport(sessions=bundle.sessions, matches=tuple(processed))
