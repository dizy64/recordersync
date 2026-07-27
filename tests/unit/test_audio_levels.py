"""렌더 전후 음량 측정과 static gain 정책."""

from __future__ import annotations

import pytest

from recordersync.audio_levels import (
    AudioLevelMetrics,
    AudioLevelPolicy,
    AudioLevelReport,
    OutputChannelLayout,
    decide_static_gain,
    parse_ebur128_summary,
    validate_output_metrics,
)


def _metrics(
    *,
    integrated_loudness_lufs: float = -20.0,
    true_peak_dbtp: float = -8.0,
    channels: int = 2,
    sample_rate: int = 48_000,
    duration_seconds: float = 30.0,
    codec: str = "pcm_f32le",
    decoder_error: str | None = None,
) -> AudioLevelMetrics:
    return AudioLevelMetrics(
        channels=channels,
        sample_rate=sample_rate,
        integrated_loudness_lufs=integrated_loudness_lufs,
        loudness_range_lu=7.5,
        sample_peak_dbfs=true_peak_dbtp - 0.1,
        true_peak_dbtp=true_peak_dbtp,
        duration_seconds=duration_seconds,
        codec=codec,
        decoder_error=decoder_error,
    )


def test_static_gain은_목표_음량과_true_peak를_모두_지키면_요청_gain을_선택한다() -> None:
    policy = AudioLevelPolicy(
        target_lufs=-16.0,
        maximum_true_peak_dbtp=-1.0,
        output_channel_layout=OutputChannelLayout.STEREO,
        loudness_tolerance_lu=0.5,
    )

    decision = decide_static_gain(_metrics(), policy)

    assert decision.requested_gain_db == pytest.approx(4.0)
    assert decision.maximum_safe_gain_db == pytest.approx(7.0)
    assert decision.applied_gain_db == pytest.approx(4.0)
    assert decision.expected_true_peak_dbtp == pytest.approx(-4.0)
    assert decision.limiter_free_lufs == pytest.approx(-13.0)
    assert decision.conflict_db == pytest.approx(0.0)


def test_static_gain은_목표_음량과_peak_제한이_충돌하면_적용하지_않고_수치를_보고한다() -> None:
    policy = AudioLevelPolicy(
        target_lufs=-7.3,
        maximum_true_peak_dbtp=-1.0,
        output_channel_layout=OutputChannelLayout.PRESERVE,
        loudness_tolerance_lu=0.5,
    )

    decision = decide_static_gain(
        _metrics(
            integrated_loudness_lufs=-11.1,
            true_peak_dbtp=7.7,
            channels=1,
        ),
        policy,
    )

    assert decision.requested_gain_db == pytest.approx(3.8)
    assert decision.maximum_safe_gain_db == pytest.approx(-8.7)
    assert decision.applied_gain_db is None
    assert decision.conflict_db == pytest.approx(12.5)
    assert decision.limiter_free_lufs == pytest.approx(-19.8)


def test_음량_보고서는_gain_충돌_결정에_출력_측정값을_허용하지_않는다() -> None:
    policy = AudioLevelPolicy(
        target_lufs=-7.3,
        maximum_true_peak_dbtp=-1.0,
        output_channel_layout=OutputChannelLayout.MONO,
        loudness_tolerance_lu=0.5,
    )
    input_metrics = _metrics(
        integrated_loudness_lufs=-11.1,
        true_peak_dbtp=7.7,
        channels=1,
    )
    decision = decide_static_gain(input_metrics, policy)

    with pytest.raises(ValueError, match="output_metrics requires an applied gain decision"):
        AudioLevelReport(
            policy=policy,
            input_metrics=input_metrics,
            decision=decision,
            output_metrics=_metrics(channels=1, codec="aac"),
        )


def test_출력_검증은_AAC_재디코딩_결과의_음량_peak_채널_rate_duration을_모두_확인한다() -> None:
    policy = AudioLevelPolicy(
        target_lufs=-16.0,
        maximum_true_peak_dbtp=-1.0,
        output_channel_layout=OutputChannelLayout.STEREO,
        loudness_tolerance_lu=0.5,
    )

    passed = validate_output_metrics(
        _metrics(
            integrated_loudness_lufs=-16.2,
            true_peak_dbtp=-1.1,
            channels=2,
            codec="aac",
        ),
        policy,
        expected_channels=2,
        expected_duration_seconds=30.0,
    )
    failed = validate_output_metrics(
        _metrics(
            integrated_loudness_lufs=-15.0,
            true_peak_dbtp=-0.4,
            channels=1,
            sample_rate=44_100,
            duration_seconds=29.5,
            codec="aac",
            decoder_error="corrupt frame",
        ),
        policy,
        expected_channels=2,
        expected_duration_seconds=30.0,
    )

    assert passed == ()
    assert failed == (
        "integrated loudness -15.0 LUFS is outside -16.0±0.5 LU",
        "true peak -0.4 dBTP exceeds -1.0 dBTP",
        "channel count 1 does not match 2",
        "sample rate 44100 Hz does not match 48000 Hz",
        "duration 29.5s differs from 30.0s",
        "decoder error: corrupt frame",
    )


def test_EBUR128_요약은_LUFS_LRA_sample_peak_true_peak를_구분해_읽는다() -> None:
    stderr = """
[Parsed_ebur128_1 @ 0x1] Summary:

  Integrated loudness:
    I:         -21.1 LUFS
    Threshold: -31.1 LUFS

  Loudness range:
    LRA:         7.5 LU

  Sample peak:
    Peak:       -0.4 dBFS

  True peak:
    Peak:        0.7 dBFS
"""

    measured = parse_ebur128_summary(stderr)

    assert measured.integrated_loudness_lufs == pytest.approx(-21.1)
    assert measured.loudness_range_lu == pytest.approx(7.5)
    assert measured.sample_peak_dbfs == pytest.approx(-0.4)
    assert measured.true_peak_dbtp == pytest.approx(0.7)


def test_EBUR128_요약의_무음_inf는_누락이_아닌_비유한_측정값으로_거부한다() -> None:
    stderr = """
[Parsed_ebur128_1 @ 0x1] Summary:

  Integrated loudness:
    I:          -inf LUFS

  Loudness range:
    LRA:         0.0 LU

  Sample peak:
    Peak:       -inf dBFS

  True peak:
    Peak:       -inf dBFS
"""

    with pytest.raises(ValueError, match="measurements must be finite"):
        parse_ebur128_summary(stderr)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"target_lufs": -71.0}, "target_lufs"),
        ({"maximum_true_peak_dbtp": 0.1}, "maximum_true_peak_dbtp"),
        ({"loudness_tolerance_lu": 0.0}, "loudness_tolerance_lu"),
    ],
)
def test_음량_정책은_잘못된_범위를_거부한다(
    kwargs: dict[str, float],
    message: str,
) -> None:
    values: dict[str, object] = {
        "target_lufs": -16.0,
        "maximum_true_peak_dbtp": -1.0,
        "output_channel_layout": OutputChannelLayout.PRESERVE,
        "loudness_tolerance_lu": 0.5,
    }
    values.update(kwargs)

    with pytest.raises(ValueError, match=message):
        AudioLevelPolicy(**values)  # type: ignore[arg-type]
