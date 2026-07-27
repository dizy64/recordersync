"""렌더 전후 음량 측정값과 static gain 안전 정책."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum


class OutputChannelLayout(StrEnum):
    """음량 안전 모드에서 사용자가 승인한 출력 채널 정책."""

    PRESERVE = "preserve"
    MONO = "mono"
    STEREO = "stereo"


@dataclass(frozen=True, slots=True)
class AudioLevelPolicy:
    """동적 처리 없이 적용할 목표 음량과 최종 출력 제약."""

    target_lufs: float
    maximum_true_peak_dbtp: float
    output_channel_layout: OutputChannelLayout
    loudness_tolerance_lu: float

    def __post_init__(self) -> None:
        if not -70.0 <= self.target_lufs <= -5.0:
            raise ValueError("target_lufs must be in [-70, -5]")
        if not -99.0 <= self.maximum_true_peak_dbtp <= 0.0:
            raise ValueError("maximum_true_peak_dbtp must be in [-99, 0]")
        if not 0.0 < self.loudness_tolerance_lu <= 10.0:
            raise ValueError("loudness_tolerance_lu must be in (0, 10]")


@dataclass(frozen=True, slots=True)
class MeasuredLoudness:
    """FFmpeg EBU R128 요약에서 읽은 음량 수치."""

    integrated_loudness_lufs: float
    loudness_range_lu: float
    sample_peak_dbfs: float
    true_peak_dbtp: float

    def __post_init__(self) -> None:
        values = (
            self.integrated_loudness_lufs,
            self.loudness_range_lu,
            self.sample_peak_dbfs,
            self.true_peak_dbtp,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("EBU R128 measurements must be finite")


@dataclass(frozen=True, slots=True)
class AudioLevelMetrics:
    """한 오디오 신호의 스트림 규격과 음량 측정 결과."""

    channels: int
    sample_rate: int
    integrated_loudness_lufs: float
    loudness_range_lu: float
    sample_peak_dbfs: float
    true_peak_dbtp: float
    duration_seconds: float
    codec: str
    decoder_error: str | None = None

    def __post_init__(self) -> None:
        if self.channels <= 0:
            raise ValueError("channels must be > 0")
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be > 0")
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0")
        if not self.codec:
            raise ValueError("codec must not be empty")
        MeasuredLoudness(
            self.integrated_loudness_lufs,
            self.loudness_range_lu,
            self.sample_peak_dbfs,
            self.true_peak_dbtp,
        )


@dataclass(frozen=True, slots=True)
class StaticGainDecision:
    """목표 LUFS와 true-peak ceiling 사이의 static gain 판정."""

    requested_gain_db: float
    maximum_safe_gain_db: float
    applied_gain_db: float | None
    expected_true_peak_dbtp: float | None
    conflict_db: float
    limiter_free_lufs: float


@dataclass(frozen=True, slots=True)
class AudioLevelReport:
    """렌더 전 측정, gain 결정, 최종 AAC 검증을 묶은 영상별 결과."""

    policy: AudioLevelPolicy
    input_metrics: AudioLevelMetrics | None = None
    decision: StaticGainDecision | None = None
    output_metrics: AudioLevelMetrics | None = None
    validation_failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.decision is not None and self.input_metrics is None:
            raise ValueError("decision requires input_metrics")
        if self.output_metrics is not None and (self.decision is None or self.decision.applied_gain_db is None):
            raise ValueError("output_metrics requires an applied gain decision")

    @property
    def passed(self) -> bool:
        return (
            self.input_metrics is not None
            and self.decision is not None
            and self.decision.applied_gain_db is not None
            and self.output_metrics is not None
            and not self.validation_failures
        )


def decide_static_gain(
    metrics: AudioLevelMetrics,
    policy: AudioLevelPolicy,
) -> StaticGainDecision:
    """동적 처리 없이 목표 LUFS와 true peak를 함께 만족하는 gain을 계산한다."""

    requested_gain = policy.target_lufs - metrics.integrated_loudness_lufs
    maximum_safe_gain = policy.maximum_true_peak_dbtp - metrics.true_peak_dbtp
    conflict = max(0.0, requested_gain - maximum_safe_gain)
    can_apply = conflict <= 1e-9
    return StaticGainDecision(
        requested_gain_db=requested_gain,
        maximum_safe_gain_db=maximum_safe_gain,
        applied_gain_db=requested_gain if can_apply else None,
        expected_true_peak_dbtp=(metrics.true_peak_dbtp + requested_gain if can_apply else None),
        conflict_db=conflict,
        limiter_free_lufs=metrics.integrated_loudness_lufs + maximum_safe_gain,
    )


def validate_output_metrics(
    metrics: AudioLevelMetrics,
    policy: AudioLevelPolicy,
    *,
    expected_channels: int,
    expected_duration_seconds: float,
) -> tuple[str, ...]:
    """최종 AAC 재디코딩 결과가 명시된 출력 계약을 만족하는지 검사한다."""

    failures: list[str] = []
    if abs(metrics.integrated_loudness_lufs - policy.target_lufs) > policy.loudness_tolerance_lu:
        failures.append(
            f"integrated loudness {metrics.integrated_loudness_lufs:.1f} LUFS is outside "
            f"{policy.target_lufs:.1f}±{policy.loudness_tolerance_lu:.1f} LU"
        )
    if metrics.true_peak_dbtp > policy.maximum_true_peak_dbtp:
        failures.append(f"true peak {metrics.true_peak_dbtp:.1f} dBTP exceeds {policy.maximum_true_peak_dbtp:.1f} dBTP")
    if metrics.channels != expected_channels:
        failures.append(f"channel count {metrics.channels} does not match {expected_channels}")
    if metrics.sample_rate != 48_000:
        failures.append(f"sample rate {metrics.sample_rate} Hz does not match 48000 Hz")
    if abs(metrics.duration_seconds - expected_duration_seconds) > 0.1:
        failures.append(f"duration {metrics.duration_seconds:.1f}s differs from {expected_duration_seconds:.1f}s")
    if metrics.codec != "aac":
        failures.append(f"codec {metrics.codec} does not match aac")
    if metrics.decoder_error is not None:
        failures.append(f"decoder error: {metrics.decoder_error}")
    return tuple(failures)


_NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+|inf)"


def _summary_value(summary: str, section: str, label: str, unit: str) -> float:
    pattern = rf"{re.escape(section)}:\s+.*?{re.escape(label)}:\s*({_NUMBER})\s+{re.escape(unit)}"
    match = re.search(pattern, summary, flags=re.DOTALL)
    if match is None:
        raise ValueError(f"Missing EBU R128 {section} {label}")
    return float(match.group(1))


def parse_ebur128_summary(stderr: str) -> MeasuredLoudness:
    """FFmpeg `ebur128=peak=sample+true`의 마지막 Summary를 파싱한다."""

    summary_marker = "Summary:"
    if summary_marker not in stderr:
        raise ValueError("Missing EBU R128 summary")
    summary = stderr.rsplit(summary_marker, maxsplit=1)[1]
    return MeasuredLoudness(
        integrated_loudness_lufs=_summary_value(summary, "Integrated loudness", "I", "LUFS"),
        loudness_range_lu=_summary_value(summary, "Loudness range", "LRA", "LU"),
        sample_peak_dbfs=_summary_value(summary, "Sample peak", "Peak", "dBFS"),
        true_peak_dbtp=_summary_value(summary, "True peak", "Peak", "dBFS"),
    )
