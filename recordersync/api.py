"""TubeArchive 등 다른 애플리케이션에서 재사용할 공개 Python API."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from recordersync.matching import MatchOptions, match_video_features
from recordersync.media import FFmpegTools, VideoInfo, discover_audio_files
from recordersync.models import AudioMatch, MatchStatus, RecordingSession
from recordersync.render import RenderMode, RenderPlan, resolve_output_path
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
        video = resolved_tools.probe_video(path)
        if not video.has_audio:
            matches.append(
                AudioMatch(
                    path,
                    video.duration_seconds,
                    MatchStatus.ERROR,
                    reason="Camera audio is required for automatic matching",
                )
            )
            continue
        matches.append(
            match_video_features(
                path,
                video.duration_seconds,
                resolved_tools.extract_features(path),
                timelines,
                options,
            )
        )
    return matches


def build_render_plan(
    match: AudioMatch,
    video: VideoInfo,
    session: RecordingSession,
    output_dir: Path,
    *,
    mode: RenderMode = RenderMode.REPLACE,
    camera_audio_volume: float = 0.1,
    overwrite: bool = False,
) -> RenderPlan:
    """승인된 매칭을 렌더 계획으로 변환한다."""

    if match.status is not MatchStatus.MATCHED:
        raise ValueError("Only matched audio can be rendered")
    if match.session_id != session.id or match.external_start_seconds is None:
        raise ValueError("Match does not belong to the supplied recording session")
    return RenderPlan(
        video=video,
        session=session,
        output_path=resolve_output_path(video.path, output_dir),
        external_start_seconds=match.external_start_seconds,
        tempo_ratio=match.tempo_ratio,
        mode=mode,
        camera_audio_volume=camera_audio_volume,
        overwrite=overwrite,
    )
