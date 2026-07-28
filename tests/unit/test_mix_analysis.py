"""측정 기반 자동 mix 정책 단위 테스트."""

from __future__ import annotations

import math
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

import numpy as np
import pytest

from recordersync.audio_levels import AudioLevelMetrics
from recordersync.media import VideoInfo
from recordersync.mix_analysis import (
    FFmpegMixAnalyzer,
    MixRecommendation,
    MixSource,
    MixSourceMetrics,
    analyze_spectral_metrics,
    recommend_auto_mix,
)
from recordersync.models import AudioChunk, RecordingSession
from recordersync.render import RenderMode, RenderPlan


def _source_metrics(
    *,
    integrated_loudness_lufs: float,
    true_peak_dbtp: float,
    low_frequency_energy_ratio: float,
    spectral_centroid_hz: float,
    channels: int = 2,
) -> MixSourceMetrics:
    return MixSourceMetrics(
        audio_levels=AudioLevelMetrics(
            channels=channels,
            sample_rate=48_000,
            integrated_loudness_lufs=integrated_loudness_lufs,
            loudness_range_lu=7.0,
            sample_peak_dbfs=true_peak_dbtp - 0.1,
            true_peak_dbtp=true_peak_dbtp,
            duration_seconds=30.0,
            codec="float_analysis",
        ),
        low_frequency_energy_ratio=low_frequency_energy_ratio,
        spectral_centroid_hz=spectral_centroid_hz,
        stereo_correlation=0.8 if channels == 2 else None,
        stereo_side_to_mid_db=-18.0 if channels == 2 else None,
    )


def _render_plan() -> RenderPlan:
    return RenderPlan(
        video=VideoInfo(Path("clip.mov"), 5.0, 1920, 1080, True, audio_channels=2),
        session=RecordingSession(
            "session-001",
            (AudioChunk(Path("REC.wav"), 30.0, 48_000, 1, "pcm_s24le", None),),
        ),
        output_path=Path("replace/clip.mp4"),
        external_start_seconds=3.0,
        tempo_ratio=1.001,
        mode=RenderMode.MIX,
        external_highpass_hz=80.0,
    )


def _ebur128_summary() -> bytes:
    return b"""
[Parsed_ebur128_0] Summary:

  Integrated loudness:
    I:         -16.1 LUFS

  Loudness range:
    LRA:         7.0 LU

  Sample peak:
    Peak:       -1.2 dBFS

  True peak:
    Peak:       -1.1 dBFS
"""


def _successful_analysis() -> CompletedProcess[bytes]:
    sample_rate = 8_000
    time = np.arange(sample_rate, dtype=np.float32) / sample_rate
    samples = np.sin(2 * np.pi * 440 * time).astype("<f4")
    return CompletedProcess(["ffmpeg"], 0, samples.tobytes(), _ebur128_summary())


def test_자동_mix는_loudness와_peak중_더_보수적인_감쇠를_선택한다() -> None:
    camera = _source_metrics(
        integrated_loudness_lufs=-12.0,
        true_peak_dbtp=-1.0,
        low_frequency_energy_ratio=0.2,
        spectral_centroid_hz=1_400.0,
    )
    external = _source_metrics(
        integrated_loudness_lufs=-20.0,
        true_peak_dbtp=7.7,
        low_frequency_energy_ratio=0.35,
        spectral_centroid_hz=1_100.0,
        channels=1,
    )

    recommendation = recommend_auto_mix(camera, external)

    assert recommendation.external_gain_db == pytest.approx(-11.7)
    assert recommendation.policy is not None
    assert recommendation.policy.camera_audio_volume == pytest.approx(1.0)
    assert recommendation.policy.external_audio_volume == pytest.approx(10 ** (-11.7 / 20))
    assert recommendation.policy.external_highpass_hz == pytest.approx(100.0)
    assert recommendation.policy.audio_level_policy.target_lufs == pytest.approx(-16.0)
    assert recommendation.policy.audio_level_policy.maximum_true_peak_dbtp == pytest.approx(-1.0)
    assert not recommendation.applied
    assert not recommendation.failures


def test_자동_mix는_조용한_외부_음원을_증폭하지_않고_기본_HP80을_유지한다() -> None:
    camera = _source_metrics(
        integrated_loudness_lufs=-12.0,
        true_peak_dbtp=-1.0,
        low_frequency_energy_ratio=0.2,
        spectral_centroid_hz=1_300.0,
    )
    external = _source_metrics(
        integrated_loudness_lufs=-30.0,
        true_peak_dbtp=-20.0,
        low_frequency_energy_ratio=0.23,
        spectral_centroid_hz=1_200.0,
        channels=1,
    )

    recommendation = recommend_auto_mix(camera, external)

    assert recommendation.external_gain_db == pytest.approx(0.0)
    assert recommendation.policy is not None
    assert recommendation.policy.external_audio_volume == pytest.approx(1.0)
    assert recommendation.policy.external_highpass_hz == pytest.approx(80.0)
    assert any("증폭하지" in reason for reason in recommendation.reasons)


def test_스펙트럼_분석은_저역_비중과_stereo_공간_지표를_계산한다() -> None:
    sample_rate = 8_000
    time = np.arange(sample_rate * 4, dtype=np.float32) / sample_rate
    bass = np.sin(2 * np.pi * 80 * time).astype(np.float32)
    treble = np.sin(2 * np.pi * 1_500 * time).astype(np.float32)
    stereo_bass = np.column_stack((bass, bass)).ravel()
    stereo_treble = np.column_stack((treble, treble)).ravel()

    bass_metrics = analyze_spectral_metrics(stereo_bass, channels=2, sample_rate=sample_rate)
    treble_metrics = analyze_spectral_metrics(stereo_treble, channels=2, sample_rate=sample_rate)

    assert bass_metrics.low_frequency_energy_ratio > 0.9
    assert treble_metrics.low_frequency_energy_ratio < 0.01
    assert bass_metrics.spectral_centroid_hz < treble_metrics.spectral_centroid_hz
    assert bass_metrics.stereo_correlation == pytest.approx(1.0)
    assert bass_metrics.stereo_side_to_mid_db is not None
    assert math.isfinite(bass_metrics.stereo_side_to_mid_db)
    assert bass_metrics.stereo_side_to_mid_db <= -100.0


def test_자동_mix_분석_명령은_두_입력을_float로_분리_측정하고_원본_gain을_바꾸지_않는다() -> None:
    plan = _render_plan()
    analyzer = FFmpegMixAnalyzer(ffmpeg_path="/opt/ffmpeg")

    camera = analyzer.build_command(plan, Path("concat.txt"), MixSource.CAMERA)
    external = analyzer.build_command(plan, Path("concat.txt"), MixSource.EXTERNAL)
    camera_joined = " ".join(camera)
    external_joined = " ".join(external)

    assert camera[0] == "/opt/ffmpeg"
    assert "-xerror" in camera
    assert "aformat=sample_fmts=fltp" in camera_joined
    assert "ebur128=peak=sample+true:framelog=quiet" in camera_joined
    assert camera[camera.index("-ac") + 1] == "2"
    assert external[external.index("-ac") + 1] == "1"
    assert external[external.index("-ss") + 1] == "3"
    assert external[external.index("-f") + 1] == "concat"
    assert "atempo=1.001" in external_joined
    assert "pan=stereo|c0=c0|c1=c0" in external_joined
    assert "pan=stereo|c0=c0|c1=c0" not in camera_joined
    assert "highpass" not in external_joined
    assert "volume=" not in external_joined
    assert camera[-1] == "pipe:1"
    assert external[-1] == "pipe:1"


def test_자동_mix_분석은_FFmpeg_진단의_마지막_세_줄을_실패로_보고한다() -> None:
    failed = CompletedProcess(["ffmpeg"], 1, b"", b"first\nsecond\nthird\nfourth\n")

    with patch("recordersync.mix_analysis.subprocess.run", return_value=failed):
        recommendation = FFmpegMixAnalyzer().recommend(_render_plan())

    assert recommendation.failures == ("camera analysis error: second | third | fourth",)


def test_자동_mix_분석은_camera_성공_후_external_실패를_구분한다() -> None:
    failed = CompletedProcess(["ffmpeg"], 1, b"", b"external decoder failure")

    with patch(
        "recordersync.mix_analysis.subprocess.run",
        side_effect=[_successful_analysis(), failed],
    ):
        recommendation = FFmpegMixAnalyzer().recommend(_render_plan())

    assert recommendation.camera is not None
    assert recommendation.external is None
    assert recommendation.failures == ("external analysis error: external decoder failure",)


def test_자동_mix_분석은_timeout을_영상별_실패로_격리한다() -> None:
    timeout = TimeoutExpired(["ffmpeg"], 1)

    with patch("recordersync.mix_analysis.subprocess.run", side_effect=timeout):
        recommendation = FFmpegMixAnalyzer().recommend(_render_plan())

    assert recommendation.failures == ("camera analysis error: camera analysis timed out",)


def test_자동_mix_추천은_gain과_linear_volume의_불일치를_거부한다() -> None:
    source = _source_metrics(
        integrated_loudness_lufs=-12,
        true_peak_dbtp=-1,
        low_frequency_energy_ratio=0.2,
        spectral_centroid_hz=1_300,
    )
    source_policy = recommend_auto_mix(source, source).policy
    assert source_policy is not None

    with pytest.raises(ValueError, match="same attenuation"):
        MixRecommendation(
            camera=source,
            external=source,
            policy=source_policy,
            external_gain_db=-6,
            reasons=("불일치 정책",),
        )


def test_자동_mix_추천은_렌더_실패를_분석_실패와_구분한다() -> None:
    source = _source_metrics(
        integrated_loudness_lufs=-12,
        true_peak_dbtp=-1,
        low_frequency_energy_ratio=0.2,
        spectral_centroid_hz=1_300,
    )
    recommendation = recommend_auto_mix(source, source)

    failed = recommendation.with_application_failure("final AAC validation failed")

    assert not failed.passed
    assert failed.policy is recommendation.policy
    assert failed.failures == ("final AAC validation failed",)
    assert not failed.applied


def test_자동_mix_원본_측정값은_암묵적인_다채널_downmix를_허용하지_않는다() -> None:
    levels = AudioLevelMetrics(3, 48_000, -12, 7, -1.1, -1, 30, "float_analysis")

    with pytest.raises(ValueError, match="mono or stereo"):
        MixSourceMetrics(levels, 0.2, 1_300, None, None)
