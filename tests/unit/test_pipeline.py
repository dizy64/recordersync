"""배치 분석·렌더 오케스트레이션 단위 테스트."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

import numpy as np
import pytest

from recordersync.matching import FeatureTimeline, MatchOptions
from recordersync.media import FFmpegTools, VideoInfo
from recordersync.models import (
    AudioChunk,
    AudioMatch,
    AudioMatchSegment,
    MatchStatus,
    RecordingSession,
)
from recordersync.pipeline import AnalysisBundle, RecorderSyncPipeline, is_renderable_match
from recordersync.render import FFmpegRenderer, RenderMode


def _features() -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(21)
    video = rng.normal(size=(6, 100)).astype(np.float32)
    video = (video - video.mean(axis=1, keepdims=True)) / video.std(axis=1, keepdims=True)
    session = rng.normal(scale=0.02, size=(6, 700)).astype(np.float32)
    session[:, 250:350] += video
    session = (session - session.mean(axis=1, keepdims=True)) / session.std(axis=1, keepdims=True)
    return video, session


def test_렌더_대상_정책은_상태와_모드와_추천_기준을_함께_판단한다() -> None:
    matched = AudioMatch(Path("full.mov"), 100, MatchStatus.MATCHED)
    safe_partial = AudioMatch(
        Path("safe.mov"),
        100,
        MatchStatus.PARTIAL,
        confidence=0.9,
        peak_margin=0.1,
        segments=(AudioMatchSegment("session-001", 10, 20, 30, confidence=0.9),),
    )
    held_partial = AudioMatch(
        Path("held.mov"),
        100,
        MatchStatus.PARTIAL,
        confidence=0.9,
        peak_margin=0.1,
        segments=(AudioMatchSegment("session-001", 10, 20, 5, confidence=0.9),),
    )
    unmatched = AudioMatch(Path("none.mov"), 100, MatchStatus.UNMATCHED)
    ambiguous = AudioMatch(Path("ambiguous.mov"), 100, MatchStatus.AMBIGUOUS)
    error_match = AudioMatch(Path("error.mov"), 100, MatchStatus.ERROR, reason="Test error")

    cases = (
        ("전체 일치", matched, RenderMode.REPLACE, True, True),
        ("일반 폴백 부분 일치", safe_partial, RenderMode.FALLBACK, False, True),
        ("추천된 부분 일치", safe_partial, RenderMode.FALLBACK, True, True),
        ("보류된 부분 일치", held_partial, RenderMode.FALLBACK, True, False),
        ("폴백이 아닌 부분 일치", safe_partial, RenderMode.MIX, False, False),
        ("불일치", unmatched, RenderMode.FALLBACK, False, False),
        ("모호한 일치", ambiguous, RenderMode.FALLBACK, False, False),
        ("오류 상태", error_match, RenderMode.FALLBACK, False, False),
    )

    for label, match, mode, recommended_only, expected in cases:
        actual = is_renderable_match(match, mode, recommended_only=recommended_only)
        assert actual is expected, label


def test_파이프라인_분석은_세션을_찾고_영상과_매칭한다(tmp_path: Path) -> None:
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


def test_파이프라인은_카메라_오디오가_없는_영상을_오류로_표시한다(tmp_path: Path) -> None:
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


def test_파이프라인_처리는_매칭된_영상만_렌더링한다(tmp_path: Path) -> None:
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


def test_파이프라인_폴백은_부분_매칭의_다중_구간을_렌더링한다(tmp_path: Path) -> None:
    video = VideoInfo(Path("clip.mov"), 10, 1920, 1080, True)
    first_session = RecordingSession(
        "session-001",
        (AudioChunk(Path("first.wav"), 20, 48_000, 2, "pcm_s24le", None),),
    )
    second_session = RecordingSession(
        "session-002",
        (AudioChunk(Path("second.wav"), 20, 48_000, 2, "pcm_s24le", None),),
    )
    match = AudioMatch(
        video.path,
        10,
        MatchStatus.PARTIAL,
        segments=(
            AudioMatchSegment(first_session.id, 1, 3, 3, confidence=0.9),
            AudioMatchSegment(second_session.id, 7, 4, 2, confidence=0.85),
        ),
    )
    bundle = AnalysisBundle((first_session, second_session), (video,), (match,))
    renderer = MagicMock(spec=FFmpegRenderer)
    renderer.render.return_value = tmp_path / "clip.mp4"

    report = RecorderSyncPipeline(renderer=renderer).process(
        bundle,
        tmp_path,
        mode=RenderMode.FALLBACK,
        camera_audio_volume=None,
    )

    plan = renderer.render.call_args.args[0]
    assert plan.camera_audio_volume == pytest.approx(1.0)
    assert [segment.session.id for segment in plan.segments] == ["session-001", "session-002"]
    assert report.matches[0].status is MatchStatus.PARTIAL
    assert report.matches[0].output_path == tmp_path / "clip.mp4"


def test_파이프라인은_전체와_추천된_부분_매칭만_폴백으로_렌더링한다(
    tmp_path: Path,
) -> None:
    full_video = VideoInfo(Path("full.mov"), 100, 1920, 1080, True)
    safe_video = VideoInfo(Path("safe.mov"), 100, 1920, 1080, True)
    held_video = VideoInfo(Path("held.mov"), 1_000, 1920, 1080, True)
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("recording.wav"), 1_200, 48_000, 2, "pcm_s24le", None),),
    )
    full_match = AudioMatch(
        full_video.path,
        full_video.duration_seconds,
        MatchStatus.MATCHED,
        session_id=session.id,
        external_start_seconds=100,
    )
    safe_match = AudioMatch(
        safe_video.path,
        safe_video.duration_seconds,
        MatchStatus.PARTIAL,
        confidence=0.9,
        peak_margin=0.1,
        segments=(AudioMatchSegment(session.id, 10, 20, 30, confidence=0.9),),
    )
    held_match = AudioMatch(
        held_video.path,
        held_video.duration_seconds,
        MatchStatus.PARTIAL,
        confidence=0.9,
        peak_margin=0.1,
        segments=(AudioMatchSegment(session.id, 10, 20, 40, confidence=0.9),),
    )
    renderer = MagicMock(spec=FFmpegRenderer)
    renderer.render.side_effect = (tmp_path / "full.mp4", tmp_path / "safe.mp4")

    report = RecorderSyncPipeline(renderer=renderer).process(
        AnalysisBundle(
            (session,),
            (full_video, safe_video, held_video),
            (full_match, safe_match, held_match),
        ),
        tmp_path,
        mode=RenderMode.FALLBACK,
        recommended_only=True,
    )

    assert [call.args[0].video.path for call in renderer.render.call_args_list] == [
        full_video.path,
        safe_video.path,
    ]
    assert report.matches[0].output_path == tmp_path / "full.mp4"
    assert report.matches[1].output_path == tmp_path / "safe.mp4"
    assert report.matches[2].output_path is None


def test_파이프라인은_폴백_모드가_아니면_부분_매칭을_렌더링하지_않는다(
    tmp_path: Path,
) -> None:
    video = VideoInfo(Path("clip.mov"), 10, 1920, 1080, True)
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("first.wav"), 20, 48_000, 2, "pcm_s24le", None),),
    )
    match = AudioMatch(
        video.path,
        10,
        MatchStatus.PARTIAL,
        segments=(AudioMatchSegment(session.id, 1, 3, 3, confidence=0.9),),
    )
    renderer = MagicMock(spec=FFmpegRenderer)

    report = RecorderSyncPipeline(renderer=renderer).process(
        AnalysisBundle((session,), (video,), (match,)),
        tmp_path,
        mode=RenderMode.REPLACE,
    )

    renderer.render.assert_not_called()
    assert report.matches[0].output_path is None


def test_파이프라인은_세션_범위를_넘는_부분_구간을_영상별_오류로_기록한다(
    tmp_path: Path,
) -> None:
    video = VideoInfo(Path("clip.mov"), 10, 1920, 1080, True)
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("first.wav"), 20, 48_000, 2, "pcm_s24le", None),),
    )
    match = AudioMatch(
        video.path,
        10,
        MatchStatus.PARTIAL,
        segments=(AudioMatchSegment(session.id, 1, 18, 5, confidence=0.9),),
    )
    renderer = MagicMock(spec=FFmpegRenderer)

    report = RecorderSyncPipeline(renderer=renderer).process(
        AnalysisBundle((session,), (video,), (match,)),
        tmp_path,
        mode=RenderMode.FALLBACK,
    )

    renderer.render.assert_not_called()
    assert report.matches[0].status is MatchStatus.ERROR
    assert report.matches[0].reason == "render segment exceeds recording session duration"
