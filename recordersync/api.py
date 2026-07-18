"""TubeArchive 등 다른 애플리케이션에서 재사용할 공개 Python API."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from recordersync.matching import MatchOptions, match_video_features
from recordersync.media import FFmpegTools, MediaError, VideoInfo, discover_audio_files
from recordersync.models import AudioMatch, MatchStatus, RecordingSession
from recordersync.render import RenderMode, RenderPlan, RenderSegment, resolve_output_path
from recordersync.sessions import group_recording_sessions


def discover_sessions(
    audio_dir: Path,
    *,
    tools: FFmpegTools | None = None,
    gap_seconds: float = 10.0,
) -> list[RecordingSession]:
    """오디오 폴더를 probe하고 연속 녹음 세션 목록을 반환한다."""

    resolved_tools = tools or FFmpegTools()
    paths = discover_audio_files(audio_dir)
    if not paths:
        raise ValueError(f"No supported audio files found in: {audio_dir}")
    chunks = [resolved_tools.probe_audio(path) for path in paths]
    return group_recording_sessions(chunks, gap_seconds=gap_seconds)


def match_videos(
    video_paths: Sequence[Path],
    sessions: Sequence[RecordingSession],
    *,
    tools: FFmpegTools | None = None,
    options: MatchOptions | None = None,
) -> list[AudioMatch]:
    """렌더 부작용 없이 여러 영상을 외부 녹음 세션에 독립 매칭한다."""

    resolved_tools = tools or FFmpegTools()
    timelines = [resolved_tools.build_session_timeline(session) for session in sessions]
    matches: list[AudioMatch] = []
    for path in video_paths:
        duration_seconds = 0.0
        try:
            video = resolved_tools.probe_video(path)
            duration_seconds = video.duration_seconds
            if not video.has_audio:
                matches.append(
                    AudioMatch(
                        path,
                        duration_seconds,
                        MatchStatus.ERROR,
                        reason="Camera audio is required for automatic matching",
                    )
                )
                continue
            matches.append(
                match_video_features(
                    path,
                    duration_seconds,
                    resolved_tools.extract_features(path),
                    timelines,
                    options,
                )
            )
        except (MediaError, OSError, ValueError) as exc:
            matches.append(
                AudioMatch(
                    path,
                    duration_seconds,
                    MatchStatus.ERROR,
                    reason=str(exc),
                )
            )
    return matches


def build_render_plan(
    match: AudioMatch,
    video: VideoInfo,
    session: RecordingSession | Sequence[RecordingSession],
    output_dir: Path,
    *,
    mode: RenderMode = RenderMode.REPLACE,
    camera_audio_volume: float | None = None,
    external_audio_volume: float = 1.0,
    overwrite: bool = False,
    output_prefix: str = "",
    output_suffix: str = "",
) -> RenderPlan:
    """승인된 매칭을 렌더 계획으로 변환한다."""

    if match.status is MatchStatus.PARTIAL and mode is not RenderMode.FALLBACK:
        raise ValueError("Partial audio can only be rendered in fallback mode")
    if match.status not in {MatchStatus.MATCHED, MatchStatus.PARTIAL}:
        raise ValueError("Only matched or partial audio can be rendered")

    resolved_sessions = (session,) if isinstance(session, RecordingSession) else tuple(session)
    session_by_id = {item.id: item for item in resolved_sessions}
    if match.segments:
        missing = {
            segment.session_id
            for segment in match.segments
            if segment.session_id not in session_by_id
        }
        if missing:
            raise ValueError("Match does not belong to the supplied recording sessions")
        render_segments = tuple(
            RenderSegment(
                session=session_by_id[segment.session_id],
                video_start_seconds=segment.video_start_seconds,
                external_start_seconds=segment.external_start_seconds,
                duration_seconds=segment.duration_seconds,
                tempo_ratio=segment.tempo_ratio,
            )
            for segment in match.segments
        )
    else:
        if (
            match.session_id is None
            or match.external_start_seconds is None
            or match.session_id not in session_by_id
        ):
            raise ValueError("Match does not belong to the supplied recording session")
        render_segments = ()

    primary_session = (
        render_segments[0].session if render_segments else session_by_id[match.session_id or ""]
    )
    external_start = (
        render_segments[0].external_start_seconds
        if render_segments
        else match.external_start_seconds or 0.0
    )
    tempo_ratio = render_segments[0].tempo_ratio if render_segments else match.tempo_ratio
    resolved_camera_volume = (
        camera_audio_volume
        if camera_audio_volume is not None
        else (1.0 if mode is RenderMode.FALLBACK else 0.1)
    )
    return RenderPlan(
        video=video,
        session=primary_session,
        output_path=resolve_output_path(
            video.path,
            output_dir,
            prefix=output_prefix,
            suffix=output_suffix,
        ),
        external_start_seconds=external_start,
        tempo_ratio=tempo_ratio,
        mode=mode,
        camera_audio_volume=resolved_camera_volume,
        external_audio_volume=external_audio_volume,
        overwrite=overwrite,
        segments=render_segments if mode is RenderMode.FALLBACK else (),
    )
