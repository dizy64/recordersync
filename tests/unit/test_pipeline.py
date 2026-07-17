"""배치 분석·렌더 오케스트레이션 단위 테스트."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

import numpy as np

from recordersync.matching import FeatureTimeline, MatchOptions
from recordersync.media import FFmpegTools, VideoInfo
from recordersync.models import AudioChunk, AudioMatch, MatchStatus, RecordingSession
from recordersync.pipeline import AnalysisBundle, RecorderSyncPipeline
from recordersync.render import FFmpegRenderer, RenderMode


def _features() -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(21)
    video = rng.normal(size=(6, 100)).astype(np.float32)
    video = (video - video.mean(axis=1, keepdims=True)) / video.std(axis=1, keepdims=True)
    session = rng.normal(scale=0.02, size=(6, 700)).astype(np.float32)
    session[:, 250:350] += video
    session = (session - session.mean(axis=1, keepdims=True)) / session.std(axis=1, keepdims=True)
    return video, session


def test_pipeline_analyze_discovers_sessions_and_matches_video(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    video_dir = tmp_path / "video"
    audio_dir.mkdir()
    video_dir.mkdir()
    audio_path = audio_dir / "REC_001.wav"
    video_path = video_dir / "clip.mov"
    audio_path.touch()
    video_path.touch()
    video_features, session_features = _features()

    tools = MagicMock(spec=FFmpegTools)
    tools.probe_audio.return_value = AudioChunk(audio_path, 35, 48_000, 2, "pcm_f32le", None)
    tools.build_session_timeline.return_value = FeatureTimeline(
        "session-001", session_features, 0.05
    )
    tools.probe_video.return_value = VideoInfo(video_path, 5, 3840, 2160, True, "bt709")
    tools.extract_features.return_value = video_features
    selection_callback = MagicMock()
    progress_callback = MagicMock()

    bundle = RecorderSyncPipeline(tools=tools).analyze(
        video_dir,
        audio_dir,
        match_options=MatchOptions(min_confidence=0.7),
        selection_callback=selection_callback,
        progress_callback=progress_callback,
    )

    assert len(bundle.sessions) == 1
    assert bundle.matches[0].status is MatchStatus.MATCHED
    assert bundle.matches[0].external_start_seconds == 12.5
    assert selection_callback.call_args_list == [
        call("audio", (audio_path,)),
        call("video", (video_path,)),
    ]
    assert progress_callback.call_args_list == [
        call("audio", 0, 1, ""),
        call("audio", 1, 1, "session-001"),
        call("match", 0, 1, ""),
        call("match", 1, 1, "clip.mov"),
    ]


def test_pipeline_marks_video_without_camera_audio_as_error(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    video_dir = tmp_path / "video"
    audio_dir.mkdir()
    video_dir.mkdir()
    audio_path = audio_dir / "REC.wav"
    video_path = video_dir / "silent.mov"
    audio_path.touch()
    video_path.touch()
    tools = MagicMock(spec=FFmpegTools)
    tools.probe_audio.return_value = AudioChunk(audio_path, 30, 48_000, 2, "pcm_s24le", None)
    tools.build_session_timeline.return_value = FeatureTimeline(
        "session-001", np.ones((6, 200), dtype=np.float32), 0.05
    )
    tools.probe_video.return_value = VideoInfo(video_path, 5, 1920, 1080, False)

    bundle = RecorderSyncPipeline(tools=tools).analyze(video_dir, audio_dir)

    assert bundle.matches[0].status is MatchStatus.ERROR
    assert "Camera audio" in (bundle.matches[0].reason or "")
    tools.extract_features.assert_not_called()


def test_pipeline_process_renders_only_matched_videos(tmp_path: Path) -> None:
    video = VideoInfo(Path("clip.mov"), 5, 3840, 2160, True)
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("REC.wav"), 30, 48_000, 2, "pcm_s24le", None),),
    )
    bundle = AnalysisBundle(
        sessions=(session,),
        videos=(video,),
        matches=(
            AudioMatch(
                video.path,
                5,
                MatchStatus.MATCHED,
                session_id=session.id,
                external_start_seconds=3,
            ),
            AudioMatch(Path("other.mov"), 5, MatchStatus.AMBIGUOUS),
        ),
    )
    renderer = MagicMock(spec=FFmpegRenderer)
    expected = tmp_path / "final_clip_synced.mp4"
    renderer.render.return_value = expected
    progress_callback = MagicMock()

    report = RecorderSyncPipeline(renderer=renderer).process(
        bundle,
        tmp_path,
        mode=RenderMode.MIX,
        camera_audio_volume=0.08,
        external_audio_volume=0.7,
        output_prefix="final_",
        output_suffix="_synced",
        progress_callback=progress_callback,
    )

    assert renderer.render.call_count == 1
    plan = renderer.render.call_args.args[0]
    assert plan.mode is RenderMode.MIX
    assert plan.camera_audio_volume == 0.08
    assert plan.external_audio_volume == 0.7
    assert plan.output_path == expected
    assert report.matches[0].output_path == expected
    assert report.matches[1].status is MatchStatus.AMBIGUOUS
    assert progress_callback.call_args_list == [
        call("render", 0, 1, ""),
        call("render", 1, 1, "clip.mov"),
    ]
