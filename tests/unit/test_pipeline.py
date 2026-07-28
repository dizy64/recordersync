"""배치 분석·렌더 오케스트레이션 단위 테스트."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

import numpy as np
import pytest

from recordersync.audio_levels import (
    AudioLevelMetrics,
    AudioLevelPolicy,
    AudioLevelReport,
    OutputChannelLayout,
    decide_static_gain,
)
from recordersync.matching import FeatureTimeline, MatchOptions
from recordersync.media import FFmpegTools, VideoInfo
from recordersync.mix_analysis import (
    FFmpegMixAnalyzer,
    MixProfile,
    MixRecommendation,
    MixSourceMetrics,
)
from recordersync.models import (
    AudioChunk,
    AudioMatch,
    AudioMatchSegment,
    MatchStatus,
    RecordingSession,
)
from recordersync.pipeline import AnalysisBundle, RecorderSyncPipeline, is_renderable_match
from recordersync.render import (
    DEFAULT_MIX_POLICY,
    AudioLevelRenderError,
    FFmpegRenderer,
    MixPolicy,
    RenderedOutput,
    RenderMode,
)


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
    tools.build_session_timeline.return_value = FeatureTimeline("session-001", session_features, 0.05)
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
    video = VideoInfo(Path("clip.mov"), 5, 3840, 2160, True, audio_channels=2)
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
    renderer.render_with_report.return_value = RenderedOutput(expected)
    progress_callback = MagicMock()

    report = RecorderSyncPipeline(renderer=renderer).process(
        bundle,
        tmp_path,
        mode=RenderMode.MIX,
        camera_audio_volume=0.08,
        external_audio_volume=0.7,
        external_highpass_hz=0,
        output_prefix="final_",
        output_suffix="_synced",
        progress_callback=progress_callback,
    )

    assert renderer.render_with_report.call_count == 1
    plan = renderer.render_with_report.call_args.args[0]
    assert plan.mode is RenderMode.MIX
    assert plan.camera_audio_volume == 0.08
    assert plan.external_audio_volume == 0.7
    assert plan.external_highpass_hz is None
    assert plan.audio_level_policy == AudioLevelPolicy(-16, -1, OutputChannelLayout.STEREO, 0.5)
    assert plan.output_path == expected
    assert report.matches[0].output_path == expected
    assert report.matches[1].status is MatchStatus.AMBIGUOUS
    assert progress_callback.call_args_list == [
        call("render", 0, 1, ""),
        call("render", 1, 1, "clip.mov"),
    ]


def test_파이프라인_믹스는_카메라를_주음원으로_두는_보수적인_기본값을_사용한다(
    tmp_path: Path,
) -> None:
    video = VideoInfo(Path("clip.mov"), 5, 3840, 2160, True, audio_channels=2)
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("REC.wav"), 30, 48_000, 1, "pcm_f32le", None),),
    )
    match = AudioMatch(
        video.path,
        5,
        MatchStatus.MATCHED,
        session_id=session.id,
        external_start_seconds=3,
    )
    renderer = MagicMock(spec=FFmpegRenderer)
    renderer.render_with_report.return_value = RenderedOutput(tmp_path / "clip.mp4")

    RecorderSyncPipeline(renderer=renderer).process(
        AnalysisBundle((session,), (video,), (match,)),
        tmp_path,
        mode=RenderMode.MIX,
    )

    plan = renderer.render_with_report.call_args.args[0]
    assert plan.camera_audio_volume == pytest.approx(1.0)
    assert plan.external_audio_volume == pytest.approx(10 ** (-12 / 20))
    assert plan.external_highpass_hz == pytest.approx(80)
    assert plan.audio_level_policy == AudioLevelPolicy(-16, -1, OutputChannelLayout.STEREO, 0.5)


def test_파이프라인_믹스는_분석기가_제안한_정책을_같은_렌더_경로에_전달한다(
    tmp_path: Path,
) -> None:
    video = VideoInfo(Path("clip.mov"), 5, 3840, 2160, True, audio_channels=2)
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("REC.wav"), 30, 48_000, 1, "pcm_f32le", None),),
    )
    match = AudioMatch(
        video.path,
        5,
        MatchStatus.MATCHED,
        session_id=session.id,
        external_start_seconds=3,
    )
    renderer = MagicMock(spec=FFmpegRenderer)
    renderer.render_with_report.return_value = RenderedOutput(tmp_path / "clip.mp4")
    suggested_policy = MixPolicy(
        camera_audio_volume=1.0,
        external_audio_volume=0.2,
        external_highpass_hz=100,
        audio_level_policy=AudioLevelPolicy(
            target_lufs=-18,
            maximum_true_peak_dbtp=-2,
            output_channel_layout=OutputChannelLayout.STEREO,
            loudness_tolerance_lu=0.5,
        ),
    )

    RecorderSyncPipeline(renderer=renderer).process(
        AnalysisBundle((session,), (video,), (match,)),
        tmp_path,
        mode=RenderMode.MIX,
        mix_policy=suggested_policy,
    )

    plan = renderer.render_with_report.call_args.args[0]
    assert plan.camera_audio_volume == pytest.approx(1.0)
    assert plan.external_audio_volume == pytest.approx(0.2)
    assert plan.external_highpass_hz == pytest.approx(100)
    assert plan.audio_level_policy == suggested_policy.audio_level_policy


def test_파이프라인_자동_mix는_영상별_추천_정책을_렌더하고_리포트에_연결한다(
    tmp_path: Path,
) -> None:
    video = VideoInfo(Path("clip.mov"), 5, 3840, 2160, True, audio_channels=2)
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("REC.wav"), 30, 48_000, 1, "pcm_f32le", None),),
    )
    match = AudioMatch(
        video.path,
        5,
        MatchStatus.MATCHED,
        session_id=session.id,
        external_start_seconds=3,
    )
    source_levels = AudioLevelMetrics(2, 48_000, -12, 7, -1.1, -1, 5, "float_analysis")
    source = MixSourceMetrics(source_levels, 0.2, 1_300, 0.8, -18)
    policy = MixPolicy(
        camera_audio_volume=1.0,
        external_audio_volume=0.2,
        external_highpass_hz=100,
        audio_level_policy=AudioLevelPolicy(-16, -1, OutputChannelLayout.STEREO, 0.5),
    )
    recommendation = MixRecommendation(
        camera=source,
        external=source,
        policy=policy,
        external_gain_db=-13.9794,
        reasons=("측정 기반 보수 감쇠",),
    )
    mix_analyzer = MagicMock(spec=FFmpegMixAnalyzer)
    mix_analyzer.recommend.return_value = recommendation
    renderer = MagicMock(spec=FFmpegRenderer)
    output = tmp_path / "clip.mp4"
    renderer.render_with_report.return_value = RenderedOutput(output)

    report = RecorderSyncPipeline(renderer=renderer, mix_analyzer=mix_analyzer).process(
        AnalysisBundle((session,), (video,), (match,)),
        tmp_path,
        mode=RenderMode.MIX,
        mix_profile=MixProfile.AUTO,
    )

    analysis_plan = mix_analyzer.recommend.call_args.args[0]
    assert analysis_plan.mode is RenderMode.MIX
    rendered_plan = renderer.render_with_report.call_args.args[0]
    assert rendered_plan.camera_audio_volume == pytest.approx(1.0)
    assert rendered_plan.external_audio_volume == pytest.approx(0.2)
    assert rendered_plan.external_highpass_hz == pytest.approx(100)
    assert report.matches[0].output_path == output
    assert report.mix_recommendations[0] is not None
    assert report.mix_recommendations[0].applied


def test_파이프라인_자동_mix_추천_전용은_분석만_하고_렌더하지_않는다(
    tmp_path: Path,
) -> None:
    video = VideoInfo(Path("clip.mov"), 5, 3840, 2160, True, audio_channels=2)
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("REC.wav"), 30, 48_000, 1, "pcm_f32le", None),),
    )
    match = AudioMatch(
        video.path,
        5,
        MatchStatus.MATCHED,
        session_id=session.id,
        external_start_seconds=3,
    )
    source_levels = AudioLevelMetrics(2, 48_000, -12, 7, -1.1, -1, 5, "float_analysis")
    source = MixSourceMetrics(source_levels, 0.2, 1_300, 0.8, -18)
    recommendation = MixRecommendation(
        camera=source,
        external=source,
        policy=MixPolicy(
            camera_audio_volume=1.0,
            external_audio_volume=10 ** (-12 / 20),
            external_highpass_hz=80,
            audio_level_policy=AudioLevelPolicy(-16, -1, OutputChannelLayout.STEREO, 0.5),
        ),
        external_gain_db=-12,
        reasons=("측정 기반 보수 감쇠",),
    )
    mix_analyzer = MagicMock(spec=FFmpegMixAnalyzer)
    mix_analyzer.recommend.return_value = recommendation
    renderer = MagicMock(spec=FFmpegRenderer)

    report = RecorderSyncPipeline(renderer=renderer, mix_analyzer=mix_analyzer).process(
        AnalysisBundle((session,), (video,), (match,)),
        tmp_path,
        mode=RenderMode.MIX,
        mix_profile=MixProfile.AUTO,
        recommend_mix_only=True,
    )

    mix_analyzer.recommend.assert_called_once()
    renderer.render.assert_not_called()
    renderer.render_with_report.assert_not_called()
    assert report.matches[0].status is MatchStatus.MATCHED
    assert report.matches[0].output_path == tmp_path / "clip.mp4"
    assert report.mix_recommendations == (recommendation,)


def test_파이프라인_자동_mix_분석_실패는_영상별_오류로_격리한다(tmp_path: Path) -> None:
    video = VideoInfo(Path("clip.mov"), 5, 3840, 2160, True, audio_channels=2)
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("REC.wav"), 30, 48_000, 1, "pcm_f32le", None),),
    )
    match = AudioMatch(
        video.path,
        5,
        MatchStatus.MATCHED,
        session_id=session.id,
        external_start_seconds=3,
    )
    failure = MixRecommendation.failed("external analysis error: invalid frame")
    mix_analyzer = MagicMock(spec=FFmpegMixAnalyzer)
    mix_analyzer.recommend.return_value = failure
    renderer = MagicMock(spec=FFmpegRenderer)

    report = RecorderSyncPipeline(renderer=renderer, mix_analyzer=mix_analyzer).process(
        AnalysisBundle((session,), (video,), (match,)),
        tmp_path,
        mode=RenderMode.MIX,
        mix_profile=MixProfile.AUTO,
    )

    renderer.render_with_report.assert_not_called()
    assert report.matches[0].status is MatchStatus.ERROR
    assert report.matches[0].reason == "Automatic mix analysis failed"
    assert report.mix_recommendations == (failure,)


@pytest.mark.parametrize(
    ("video_channels", "external_channels", "failure"),
    (
        (3, 1, "analysis setup error: mix mode supports mono or stereo camera audio"),
        (2, 3, "analysis setup error: mix mode supports mono or stereo recorder audio"),
    ),
)
def test_파이프라인_자동_mix는_다채널_입력을_영상별_실패로_격리한다(
    tmp_path: Path,
    video_channels: int,
    external_channels: int,
    failure: str,
) -> None:
    video = VideoInfo(Path("clip.mov"), 5, 3840, 2160, True, audio_channels=video_channels)
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("REC.wav"), 30, 48_000, external_channels, "pcm_f32le", None),),
    )
    match = AudioMatch(
        video.path,
        5,
        MatchStatus.MATCHED,
        session_id=session.id,
        external_start_seconds=3,
    )
    mix_analyzer = MagicMock(spec=FFmpegMixAnalyzer)
    renderer = MagicMock(spec=FFmpegRenderer)

    report = RecorderSyncPipeline(renderer=renderer, mix_analyzer=mix_analyzer).process(
        AnalysisBundle((session,), (video,), (match,)),
        tmp_path,
        mode=RenderMode.MIX,
        mix_profile=MixProfile.AUTO,
    )

    mix_analyzer.recommend.assert_not_called()
    renderer.render_with_report.assert_not_called()
    assert report.matches[0].status is MatchStatus.ERROR
    recommendation = report.mix_recommendations[0]
    assert recommendation is not None
    assert recommendation.failures == (failure,)


def test_파이프라인_자동_mix_렌더_실패는_추천_상태와_구분한다(tmp_path: Path) -> None:
    video = VideoInfo(Path("clip.mov"), 5, 3840, 2160, True, audio_channels=2)
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("REC.wav"), 30, 48_000, 1, "pcm_f32le", None),),
    )
    match = AudioMatch(
        video.path,
        5,
        MatchStatus.MATCHED,
        session_id=session.id,
        external_start_seconds=3,
    )
    source_levels = AudioLevelMetrics(2, 48_000, -12, 7, -1.1, -1, 5, "float_analysis")
    source = MixSourceMetrics(source_levels, 0.2, 1_300, 0.8, -18)
    recommendation = MixRecommendation(
        camera=source,
        external=source,
        policy=MixPolicy(
            camera_audio_volume=1.0,
            external_audio_volume=10 ** (-12 / 20),
            external_highpass_hz=80,
            audio_level_policy=AudioLevelPolicy(-16, -1, OutputChannelLayout.STEREO, 0.5),
        ),
        external_gain_db=-12,
        reasons=("측정 기반 보수 감쇠",),
    )
    mix_analyzer = MagicMock(spec=FFmpegMixAnalyzer)
    mix_analyzer.recommend.return_value = recommendation
    renderer = MagicMock(spec=FFmpegRenderer)
    renderer.render_with_report.side_effect = RuntimeError("final AAC validation failed")

    report = RecorderSyncPipeline(renderer=renderer, mix_analyzer=mix_analyzer).process(
        AnalysisBundle((session,), (video,), (match,)),
        tmp_path,
        mode=RenderMode.MIX,
        mix_profile=MixProfile.AUTO,
    )

    assert report.matches[0].status is MatchStatus.ERROR
    failed_recommendation = report.mix_recommendations[0]
    assert failed_recommendation is not None
    assert failed_recommendation.failures == ("final AAC validation failed",)
    assert report.to_dict()["matches"][0]["mix_recommendation"]["status"] == "application_error"


@pytest.mark.parametrize(
    ("mode", "mix_profile", "recommend_mix_only", "mix_policy", "error"),
    (
        (
            RenderMode.REPLACE,
            MixProfile.AUTO,
            False,
            None,
            "automatic mix analysis requires mix mode",
        ),
        (
            RenderMode.MIX,
            MixProfile.AUTO,
            False,
            DEFAULT_MIX_POLICY,
            "automatic mix analysis cannot be combined with manual mix options",
        ),
        (
            RenderMode.MIX,
            MixProfile.CONSERVATIVE,
            True,
            None,
            "mix recommendation-only processing requires the auto profile",
        ),
    ),
)
def test_파이프라인은_직접_호출에서도_자동_mix_옵션_조합을_검증한다(
    tmp_path: Path,
    mode: RenderMode,
    mix_profile: MixProfile,
    recommend_mix_only: bool,
    mix_policy: MixPolicy | None,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        RecorderSyncPipeline().process(
            AnalysisBundle((), (), ()),
            tmp_path,
            mode=mode,
            mix_profile=mix_profile,
            recommend_mix_only=recommend_mix_only,
            mix_policy=mix_policy,
        )


def test_파이프라인은_음량_안전_결과를_영상별_리포트에_연결한다(tmp_path: Path) -> None:
    video = VideoInfo(Path("clip.mov"), 5, 3840, 2160, True)
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("REC.wav"), 30, 48_000, 2, "pcm_f32le", None),),
    )
    match = AudioMatch(
        video.path,
        5,
        MatchStatus.MATCHED,
        session_id=session.id,
        external_start_seconds=3,
    )
    policy = AudioLevelPolicy(-16, -1, OutputChannelLayout.STEREO, 0.5)
    input_metrics = AudioLevelMetrics(2, 48_000, -20, 7, -8.1, -8, 5, "pcm_f32le")
    output_metrics = AudioLevelMetrics(2, 48_000, -16.1, 7, -1.2, -1.1, 5, "aac")
    audio_levels = AudioLevelReport(
        policy,
        input_metrics,
        decide_static_gain(input_metrics, policy),
        output_metrics,
    )
    renderer = MagicMock(spec=FFmpegRenderer)
    output = tmp_path / "clip.mp4"
    renderer.render_with_report.return_value = RenderedOutput(output, audio_levels)

    report = RecorderSyncPipeline(renderer=renderer).process(
        AnalysisBundle((session,), (video,), (match,)),
        tmp_path,
        audio_level_policy=policy,
    )

    renderer.render.assert_not_called()
    plan = renderer.render_with_report.call_args.args[0]
    assert plan.audio_level_policy is policy
    assert report.matches[0].output_path == output
    assert report.audio_levels == (audio_levels,)
    assert report.to_dict()["matches"][0]["audio_levels"]["validation"]["passed"]


def test_파이프라인은_음량_검증_실패를_오류로_기록하고_보고서를_남긴다(
    tmp_path: Path,
) -> None:
    video = VideoInfo(Path("clip.mov"), 5, 3840, 2160, True)
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("REC.wav"), 30, 48_000, 1, "pcm_f32le", None),),
    )
    match = AudioMatch(
        video.path,
        5,
        MatchStatus.MATCHED,
        session_id=session.id,
        external_start_seconds=3,
    )
    policy = AudioLevelPolicy(-16, -1, OutputChannelLayout.MONO, 0.5)
    input_metrics = AudioLevelMetrics(1, 48_000, -20, 7, -0.1, 0, 5, "pcm_f32le")
    audio_levels = AudioLevelReport(
        policy,
        input_metrics,
        decide_static_gain(input_metrics, policy),
        validation_failures=("loudness target conflicts with true-peak ceiling",),
    )
    renderer = MagicMock(spec=FFmpegRenderer)
    renderer.render_with_report.side_effect = AudioLevelRenderError(
        "Loudness target conflicts with true-peak ceiling",
        audio_levels,
    )

    report = RecorderSyncPipeline(renderer=renderer).process(
        AnalysisBundle((session,), (video,), (match,)),
        tmp_path,
        audio_level_policy=policy,
    )

    assert report.matches[0].status is MatchStatus.ERROR
    assert report.matches[0].output_path is None
    assert report.audio_levels == (audio_levels,)


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


def test_파이프라인은_없는_세션_오류들을_한국어_리포트로_제공한다(tmp_path: Path) -> None:
    matched_video = VideoInfo(Path("matched.mov"), 10, 1920, 1080, True)
    partial_video = VideoInfo(Path("partial.mov"), 10, 1920, 1080, True)
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("first.wav"), 20, 48_000, 2, "pcm_s24le", None),),
    )
    matched = AudioMatch(
        matched_video.path,
        10,
        MatchStatus.MATCHED,
        session_id="missing-session",
        external_start_seconds=3,
    )
    partial = AudioMatch(
        partial_video.path,
        10,
        MatchStatus.PARTIAL,
        segments=(AudioMatchSegment("missing-session", 1, 3, 3, confidence=0.9),),
    )
    renderer = MagicMock(spec=FFmpegRenderer)

    report = RecorderSyncPipeline(renderer=renderer).process(
        AnalysisBundle(
            (session,),
            (matched_video, partial_video),
            (matched, partial),
        ),
        tmp_path,
        mode=RenderMode.FALLBACK,
    )

    renderer.render.assert_not_called()
    assert [match.status for match in report.matches] == [MatchStatus.ERROR, MatchStatus.ERROR]
    assert [match["reason"] for match in report.to_dict()["matches"]] == [
        "매칭이 제공된 녹음 세션에 속하지 않습니다.",
        "매칭이 제공된 녹음 세션들에 속하지 않습니다.",
    ]


def test_파이프라인은_영상_메타데이터_누락을_한국어_오류로_기록한다(tmp_path: Path) -> None:
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("first.wav"), 20, 48_000, 2, "pcm_s24le", None),),
    )
    match = AudioMatch(
        Path("missing-video.mov"),
        10,
        MatchStatus.MATCHED,
        session_id=session.id,
        external_start_seconds=3,
    )
    renderer = MagicMock(spec=FFmpegRenderer)

    report = RecorderSyncPipeline(renderer=renderer).process(
        AnalysisBundle((session,), (), (match,)),
        tmp_path,
    )

    renderer.render.assert_not_called()
    assert report.matches[0].status is MatchStatus.ERROR
    assert report.to_dict()["matches"][0]["reason"] == "매칭 결과에 렌더 메타데이터가 없습니다."
