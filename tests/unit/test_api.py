"""향후 TubeArchive가 호출할 공개 Python API 계약."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

from recordersync.api import build_render_plan, discover_sessions, match_videos
from recordersync.matching import FeatureTimeline, MatchOptions
from recordersync.media import FFmpegTools, MediaError, VideoInfo
from recordersync.models import (
    AudioChunk,
    AudioMatch,
    AudioMatchSegment,
    MatchStatus,
    RecordingSession,
)
from recordersync.render import RenderMode


def test_세션_탐색은_오디오_디렉터리를_분석하고_그룹화한다(tmp_path: Path) -> None:
    first = tmp_path / "REC_001.wav"
    second = tmp_path / "REC_002.wav"
    first.touch()
    second.touch()
    tools = MagicMock(spec=FFmpegTools)
    tools.probe_audio.side_effect = [
        AudioChunk(first, 60, 48_000, 2, "pcm_f32le", None),
        AudioChunk(second, 60, 48_000, 2, "pcm_f32le", None),
    ]

    sessions = discover_sessions(tmp_path, tools=tools)

    assert len(sessions) == 1
    assert sessions[0].chunks[1].path == second


def test_영상_매칭은_렌더링하지_않고_결과를_반환한다() -> None:
    rng = np.random.default_rng(23)
    video_features = rng.normal(size=(6, 80)).astype(np.float32)
    video_features = (video_features - video_features.mean(axis=1, keepdims=True)) / (
        video_features.std(axis=1, keepdims=True)
    )
    session_features = rng.normal(scale=0.02, size=(6, 400)).astype(np.float32)
    session_features[:, 150:230] += video_features
    session_features = (session_features - session_features.mean(axis=1, keepdims=True)) / (
        session_features.std(axis=1, keepdims=True)
    )
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("REC.wav"), 20, 48_000, 2, "pcm_f32le", None),),
    )
    video = VideoInfo(Path("clip.mov"), 4, 3840, 2160, True)
    tools = MagicMock(spec=FFmpegTools)
    tools.build_session_timeline.return_value = FeatureTimeline(session.id, session_features, 0.05)
    tools.probe_video.return_value = video
    tools.extract_features.return_value = video_features

    matches = match_videos(
        [video.path],
        [session],
        tools=tools,
        options=MatchOptions(min_confidence=0.7),
    )

    assert matches[0].status is MatchStatus.MATCHED


def test_영상_매칭은_probe_실패를_격리하고_다음_영상을_계속한다() -> None:
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("REC.wav"), 20, 48_000, 2, "pcm_f32le", None),),
    )
    broken_path = Path("broken.mov")
    next_path = Path("next.mov")
    tools = MagicMock(spec=FFmpegTools)
    tools.build_session_timeline.return_value = FeatureTimeline(session.id, np.ones((6, 100), dtype=np.float32), 0.05)
    tools.probe_video.side_effect = [
        MediaError("Failed to probe broken.mov"),
        VideoInfo(next_path, 4, 1920, 1080, False),
    ]

    matches = match_videos([broken_path, next_path], [session], tools=tools)

    assert [match.status for match in matches] == [MatchStatus.ERROR, MatchStatus.ERROR]
    assert matches[0].duration_seconds == 0
    assert matches[0].reason == "Failed to probe broken.mov"
    assert matches[1].reason == "Camera audio is required for automatic matching"
    assert tools.probe_video.call_count == 2


def test_영상_매칭은_IO_실패를_격리하고_다음_영상을_계속한다() -> None:
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("REC.wav"), 20, 48_000, 2, "pcm_f32le", None),),
    )
    broken_path = Path("broken.mov")
    next_path = Path("next.mov")
    tools = MagicMock(spec=FFmpegTools)
    tools.build_session_timeline.return_value = FeatureTimeline(session.id, np.ones((6, 100), dtype=np.float32), 0.05)
    tools.probe_video.side_effect = [
        PermissionError("permission denied: broken.mov"),
        VideoInfo(next_path, 4, 1920, 1080, False),
    ]

    matches = match_videos([broken_path, next_path], [session], tools=tools)

    assert [match.status for match in matches] == [MatchStatus.ERROR, MatchStatus.ERROR]
    assert matches[0].reason == "permission denied: broken.mov"
    assert matches[1].reason == "Camera audio is required for automatic matching"
    assert tools.probe_video.call_count == 2


def test_영상_매칭은_특징_추출_실패를_격리하고_다음_영상을_계속한다() -> None:
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("REC.wav"), 20, 48_000, 2, "pcm_f32le", None),),
    )
    broken_path = Path("broken.mov")
    next_path = Path("next.mov")
    tools = MagicMock(spec=FFmpegTools)
    tools.build_session_timeline.return_value = FeatureTimeline(session.id, np.ones((6, 100), dtype=np.float32), 0.05)
    tools.probe_video.side_effect = [
        VideoInfo(broken_path, 4, 1920, 1080, True),
        VideoInfo(next_path, 4, 1920, 1080, False),
    ]
    tools.extract_features.side_effect = ValueError("decoded features are invalid")

    matches = match_videos([broken_path, next_path], [session], tools=tools)

    assert [match.status for match in matches] == [MatchStatus.ERROR, MatchStatus.ERROR]
    assert matches[0].duration_seconds == 4
    assert matches[0].reason == "decoded features are invalid"
    assert matches[1].reason == "Camera audio is required for automatic matching"
    assert tools.probe_video.call_count == 2


def test_렌더_계획_생성은_승인된_매칭을_연결한다() -> None:
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("REC.wav"), 20, 48_000, 2, "pcm_f32le", None),),
    )
    video = VideoInfo(Path("clip.mov"), 4, 3840, 2160, True)
    match = AudioMatch(
        video.path,
        4,
        MatchStatus.MATCHED,
        session_id=session.id,
        external_start_seconds=2.5,
        tempo_ratio=1.0001,
    )

    plan = build_render_plan(
        match,
        video,
        session,
        Path("replace"),
        mode=RenderMode.MIX,
        camera_audio_volume=0.1,
        external_audio_volume=0.8,
        output_prefix="final_",
        output_suffix="_synced",
    )

    assert plan.external_start_seconds == 2.5
    assert plan.output_path == Path("replace/final_clip_synced.mp4")
    assert plan.mode is RenderMode.MIX
    assert plan.external_audio_volume == 0.8


def test_렌더_계획_생성은_부분_매칭의_여러_세션을_연결한다() -> None:
    sessions = (
        RecordingSession(
            "session-001",
            (AudioChunk(Path("first.wav"), 20, 48_000, 2, "pcm_f32le", None),),
        ),
        RecordingSession(
            "session-002",
            (AudioChunk(Path("second.wav"), 20, 48_000, 2, "pcm_f32le", None),),
        ),
    )
    video = VideoInfo(Path("clip.mov"), 10, 1920, 1080, True)
    match = AudioMatch(
        video.path,
        10,
        MatchStatus.PARTIAL,
        segments=(
            AudioMatchSegment("session-001", 1, 2, 3),
            AudioMatchSegment("session-002", 7, 4, 2),
        ),
    )

    plan = build_render_plan(
        match,
        video,
        sessions,
        Path("replace"),
        mode=RenderMode.FALLBACK,
        camera_audio_volume=1.0,
    )

    assert [segment.session.id for segment in plan.segments] == [
        "session-001",
        "session-002",
    ]
