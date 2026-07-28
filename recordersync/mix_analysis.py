"""원본 오디오 측정값으로 보수적인 mix 정책을 추천한다."""

from __future__ import annotations

import math
import subprocess
import tempfile
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from scipy.signal import welch

from recordersync.audio_levels import AudioLevelMetrics, parse_ebur128_summary
from recordersync.render import (
    DEFAULT_MIX_AUDIO_LEVEL_POLICY,
    MixPolicy,
    RenderMode,
    RenderPlan,
    build_concat_manifest,
    format_ffmpeg_number,
)

_ANALYSIS_SAMPLE_RATE = 8_000
_MINIMUM_SPECTRAL_SAMPLES = 256
_LOW_FREQUENCY_MAX_HZ = 160.0
_TARGET_EXTERNAL_BELOW_CAMERA_DB = 12.0
_TARGET_EXTERNAL_PEAK_BELOW_CAMERA_DB = 3.0
_BASS_EXCESS_RATIO = 0.08
_BASS_EXCESS_CENTROID_HZ = 150.0
_DEFAULT_HIGHPASS_HZ = 80.0
_BASS_EXCESS_HIGHPASS_HZ = 100.0
_ANALYSIS_TIMEOUT_SECONDS = 3_600.0
_SPECTRAL_WINDOW_SECONDS = 10.0
_SPECTRAL_WINDOW_COUNT = 12
_MAXIMUM_SPECTRAL_SECONDS = _SPECTRAL_WINDOW_SECONDS * _SPECTRAL_WINDOW_COUNT


class MixProfile(StrEnum):
    """사용자가 명시하는 mix 정책 선택 방식."""

    CONSERVATIVE = "conservative"
    AUTO = "auto"


class MixSource(StrEnum):
    """자동 mix에서 독립적으로 측정할 입력."""

    CAMERA = "camera"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class SpectralMetrics:
    """float PCM에서 계산한 스펙트럼·공간 측정값."""

    low_frequency_energy_ratio: float
    spectral_centroid_hz: float
    stereo_correlation: float | None
    stereo_side_to_mid_db: float | None

    def __post_init__(self) -> None:
        if not 0.0 <= self.low_frequency_energy_ratio <= 1.0:
            raise ValueError("low_frequency_energy_ratio must be in [0, 1]")
        if not math.isfinite(self.spectral_centroid_hz) or self.spectral_centroid_hz < 0:
            raise ValueError("spectral_centroid_hz must be finite and >= 0")
        if self.stereo_correlation is not None and not -1.0 <= self.stereo_correlation <= 1.0:
            raise ValueError("stereo_correlation must be in [-1, 1]")
        if self.stereo_side_to_mid_db is not None and not math.isfinite(self.stereo_side_to_mid_db):
            raise ValueError("stereo_side_to_mid_db must be finite")


@dataclass(frozen=True, slots=True)
class MixSourceMetrics:
    """한 mix 입력의 float 음량과 스펙트럼·공간 측정값."""

    audio_levels: AudioLevelMetrics
    low_frequency_energy_ratio: float
    spectral_centroid_hz: float
    stereo_correlation: float | None
    stereo_side_to_mid_db: float | None

    def __post_init__(self) -> None:
        if self.audio_levels.channels not in {1, 2}:
            raise ValueError("mix source metrics support mono or stereo")
        SpectralMetrics(
            self.low_frequency_energy_ratio,
            self.spectral_centroid_hz,
            self.stereo_correlation,
            self.stereo_side_to_mid_db,
        )
        if self.audio_levels.channels == 1:
            if self.stereo_correlation is not None or self.stereo_side_to_mid_db is not None:
                raise ValueError("mono source cannot have stereo metrics")
        elif self.audio_levels.channels == 2 and (
            self.stereo_correlation is None or self.stereo_side_to_mid_db is None
        ):
            raise ValueError("stereo source requires stereo metrics")


@dataclass(frozen=True, slots=True)
class MixRecommendation:
    """자동 분석 결과와 같은 렌더 경로에 전달할 정책."""

    camera: MixSourceMetrics | None = None
    external: MixSourceMetrics | None = None
    policy: MixPolicy | None = None
    external_gain_db: float | None = None
    reasons: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    applied: bool = False

    def __post_init__(self) -> None:
        if self.policy is None:
            if self.external_gain_db is not None or self.applied:
                raise ValueError("failed recommendation cannot have gain or be applied")
            if not self.failures:
                raise ValueError("recommendation without policy requires failures")
            return
        if self.camera is None or self.external is None:
            raise ValueError("successful recommendation requires both source metrics")
        if self.external_gain_db is None or not math.isfinite(self.external_gain_db):
            raise ValueError("successful recommendation requires finite external_gain_db")
        if self.external_gain_db > 0:
            raise ValueError("automatic mix recommendation cannot boost external audio")
        expected_volume = 10 ** (self.external_gain_db / 20.0)
        if not math.isclose(self.policy.external_audio_volume, expected_volume, rel_tol=1e-6, abs_tol=1e-9):
            raise ValueError("external gain and volume must describe the same attenuation")
        if not self.reasons:
            raise ValueError("successful recommendation requires reasons")
        if self.failures and self.applied:
            raise ValueError("failed application cannot be marked as applied")

    @property
    def passed(self) -> bool:
        return self.policy is not None and not self.failures

    @classmethod
    def failed(
        cls,
        failure: str,
        *,
        camera: MixSourceMetrics | None = None,
        external: MixSourceMetrics | None = None,
    ) -> MixRecommendation:
        if not failure:
            raise ValueError("failure must not be empty")
        return cls(camera=camera, external=external, failures=(failure,))

    def with_application_failure(self, failure: str) -> MixRecommendation:
        """분석은 성공했지만 추천 정책을 최종 출력에 적용하지 못한 상태를 만든다."""

        if self.policy is None or self.failures:
            raise ValueError("application failure requires a successful recommendation")
        if not failure:
            raise ValueError("failure must not be empty")
        return replace(self, failures=(failure,), applied=False)


def analyze_spectral_metrics(
    samples: NDArray[np.float32],
    *,
    channels: int,
    sample_rate: int,
) -> SpectralMetrics:
    """PCM의 스펙트럼과 stereo 공간 지표를 계산한다."""

    if samples.ndim != 1:
        raise ValueError("samples must be interleaved 1-D PCM")
    if channels not in {1, 2}:
        raise ValueError("spectral analysis supports mono or stereo")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be > 0")
    if samples.size % channels:
        raise ValueError("sample count must align with channels")
    frames = samples.reshape(-1, channels).astype(np.float64)
    if len(frames) < _MINIMUM_SPECTRAL_SAMPLES:
        raise ValueError("audio is too short for spectral analysis")
    if not np.isfinite(frames).all():
        raise ValueError("audio contains non-finite samples")

    segment_size = min(4_096, len(frames))
    frequencies, power = welch(
        frames,
        fs=sample_rate,
        axis=0,
        nperseg=segment_size,
        noverlap=segment_size // 2,
        detrend="constant",
        scaling="spectrum",
    )
    average_power = np.mean(power, axis=1)
    measured = (frequencies >= 20.0) & (frequencies <= sample_rate / 2)
    total_power = float(np.sum(average_power[measured]))
    if total_power <= np.finfo(np.float64).eps:
        raise ValueError("audio is too quiet for spectral analysis")
    low_frequency = measured & (frequencies <= _LOW_FREQUENCY_MAX_HZ)
    low_frequency_ratio = float(np.sum(average_power[low_frequency]) / total_power)
    spectral_centroid = float(np.sum(frequencies[measured] * average_power[measured]) / total_power)

    stereo_correlation: float | None = None
    stereo_side_to_mid_db: float | None = None
    if channels == 2:
        left = frames[:, 0] - np.mean(frames[:, 0])
        right = frames[:, 1] - np.mean(frames[:, 1])
        correlation_denominator = math.sqrt(float(np.dot(left, left) * np.dot(right, right)))
        stereo_correlation = (
            float(np.dot(left, right) / correlation_denominator) if correlation_denominator > 0 else 0.0
        )
        stereo_correlation = min(1.0, max(-1.0, stereo_correlation))
        mid = (left + right) * 0.5
        side = (left - right) * 0.5
        mid_energy = float(np.mean(np.square(mid)))
        side_energy = float(np.mean(np.square(side)))
        reference_energy = max(mid_energy, np.finfo(np.float64).eps)
        ratio = max(side_energy / reference_energy, 1e-12)
        stereo_side_to_mid_db = 10.0 * math.log10(ratio)

    return SpectralMetrics(
        low_frequency_energy_ratio=low_frequency_ratio,
        spectral_centroid_hz=spectral_centroid,
        stereo_correlation=stereo_correlation,
        stereo_side_to_mid_db=stereo_side_to_mid_db,
    )


def recommend_auto_mix(
    camera: MixSourceMetrics,
    external: MixSourceMetrics,
) -> MixRecommendation:
    """카메라를 주 음원으로 유지하는 static component gain과 HPF를 추천한다."""

    camera_levels = camera.audio_levels
    external_levels = external.audio_levels
    loudness_gain_db = (
        camera_levels.integrated_loudness_lufs
        - external_levels.integrated_loudness_lufs
        - _TARGET_EXTERNAL_BELOW_CAMERA_DB
    )
    peak_gain_db = camera_levels.true_peak_dbtp - external_levels.true_peak_dbtp - _TARGET_EXTERNAL_PEAK_BELOW_CAMERA_DB
    external_gain_db = min(0.0, loudness_gain_db, peak_gain_db)

    bass_excess = (
        external.low_frequency_energy_ratio - camera.low_frequency_energy_ratio >= _BASS_EXCESS_RATIO
        and camera.spectral_centroid_hz - external.spectral_centroid_hz >= _BASS_EXCESS_CENTROID_HZ
    )
    highpass_hz = _BASS_EXCESS_HIGHPASS_HZ if bass_excess else _DEFAULT_HIGHPASS_HZ
    reasons: list[str] = []
    if external_gain_db == 0.0:
        reasons.append("외부 음원은 이미 보수적인 상대 음량보다 낮아 자동으로 증폭하지 않습니다.")
    elif peak_gain_db < loudness_gain_db:
        reasons.append(f"외부 peak에 {_TARGET_EXTERNAL_PEAK_BELOW_CAMERA_DB:.0f} dB 여유를 두는 감쇠를 우선합니다.")
    else:
        reasons.append(
            f"외부 integrated loudness를 카메라보다 {_TARGET_EXTERNAL_BELOW_CAMERA_DB:.0f} LU 낮추는 감쇠를 적용합니다."
        )
    if bass_excess:
        reasons.append("외부 음원의 저역 비중이 더 높고 spectral centroid가 낮아 HP100을 제안합니다.")
    else:
        reasons.append("측정상 뚜렷한 저역 과다가 없어 보수 프리셋 HP80을 유지합니다.")
    if external_levels.channels == 1:
        reasons.append("외부 mono는 추가 gain 없이 dual-mono로 배치하고 카메라의 stereo 공간 정보를 유지합니다.")

    policy = MixPolicy(
        camera_audio_volume=1.0,
        external_audio_volume=10 ** (external_gain_db / 20.0),
        external_highpass_hz=highpass_hz,
        audio_level_policy=DEFAULT_MIX_AUDIO_LEVEL_POLICY,
    )
    return MixRecommendation(
        camera=camera,
        external=external,
        policy=policy,
        external_gain_db=external_gain_db,
        reasons=tuple(reasons),
    )


def _spectral_selection_filter(duration_seconds: float) -> str:
    """긴 입력의 스펙트럼 PCM을 시간축 전체의 고정 크기 대표 구간으로 제한한다."""

    if duration_seconds <= _MAXIMUM_SPECTRAL_SECONDS:
        return ""
    last_start = duration_seconds - _SPECTRAL_WINDOW_SECONDS
    step = last_start / (_SPECTRAL_WINDOW_COUNT - 1)
    windows = (
        "between(t,"
        f"{format_ffmpeg_number(index * step)},"
        f"{format_ffmpeg_number(index * step + _SPECTRAL_WINDOW_SECONDS)})"
        for index in range(_SPECTRAL_WINDOW_COUNT)
    )
    return f"aselect='{'+'.join(windows)}',asetpts=N/SR/TB,"


class FFmpegMixAnalyzer:
    """두 원본을 독립적인 float 신호로 디코딩해 자동 mix 정책을 계산한다."""

    def __init__(
        self,
        ffmpeg_path: str = "ffmpeg",
        *,
        timeout_seconds: float = _ANALYSIS_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        self.ffmpeg_path = ffmpeg_path
        self.timeout_seconds = timeout_seconds

    def build_command(
        self,
        plan: RenderPlan,
        manifest_path: Path,
        source: MixSource,
    ) -> list[str]:
        """선택한 원본을 EBU R128과 8kHz float 스펙트럼 입력으로 동시에 디코딩한다."""

        if plan.mode is not RenderMode.MIX:
            raise ValueError("automatic mix analysis requires mix mode")
        if source is MixSource.CAMERA:
            channels = plan.video.audio_channels
            if channels not in {1, 2}:
                raise ValueError("automatic mix analysis supports mono or stereo camera audio")
            input_arguments = ["-i", str(plan.video.path)]
            source_chain = ""
        else:
            channels = plan.session.chunks[0].channels
            if channels not in {1, 2}:
                raise ValueError("automatic mix analysis supports mono or stereo recorder audio")
            input_arguments = [
                "-ss",
                format_ffmpeg_number(plan.external_start_seconds),
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(manifest_path),
            ]
            source_chain = f"atempo={format_ffmpeg_number(plan.tempo_ratio)},"
        channel_layout = "mono" if channels == 1 else "stereo"
        meter_channel_filter = "pan=stereo|c0=c0|c1=c0," if channels == 1 else ""
        spectral_selection = _spectral_selection_filter(plan.video.duration_seconds)
        filters = (
            f"[0:a:0]{source_chain}aresample=48000,apad,atrim=duration={format_ffmpeg_number(plan.video.duration_seconds)},"
            f"asetpts=PTS-STARTPTS,aformat=channel_layouts={channel_layout},aformat=sample_fmts=fltp,"
            "asplit=2[meter_input][spectral_input];"
            f"[meter_input]{meter_channel_filter}ebur128=peak=sample+true:framelog=quiet[metered];"
            "[metered]anullsink;"
            f"[spectral_input]{spectral_selection}aresample={_ANALYSIS_SAMPLE_RATE}[spectral]"
        )
        return [
            self.ffmpeg_path,
            "-hide_banner",
            "-nostats",
            "-xerror",
            "-err_detect",
            "explode",
            *input_arguments,
            "-filter_complex",
            filters,
            "-map",
            "[spectral]",
            "-ac",
            str(channels),
            "-ar",
            str(_ANALYSIS_SAMPLE_RATE),
            "-f",
            "f32le",
            "pipe:1",
        ]

    def _measure(
        self,
        plan: RenderPlan,
        manifest_path: Path,
        source: MixSource,
    ) -> MixSourceMetrics:
        command = self.build_command(plan, manifest_path, source)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{source.value} analysis timed out") from exc
        stderr = result.stderr.decode("utf-8", errors="replace")
        if result.returncode != 0:
            lines = [line.strip() for line in stderr.splitlines() if line.strip()]
            diagnostic = " | ".join(lines[-3:]) if lines else f"FFmpeg exited with code {result.returncode}"
            raise RuntimeError(diagnostic)
        try:
            measured = parse_ebur128_summary(stderr)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

        channels = plan.video.audio_channels if source is MixSource.CAMERA else plan.session.chunks[0].channels
        if channels not in {1, 2}:
            raise ValueError("automatic mix analysis supports mono or stereo")
        samples = np.frombuffer(result.stdout, dtype="<f4")
        spectral = analyze_spectral_metrics(
            samples,
            channels=channels,
            sample_rate=_ANALYSIS_SAMPLE_RATE,
        )
        levels = AudioLevelMetrics(
            channels=channels,
            sample_rate=48_000,
            integrated_loudness_lufs=measured.integrated_loudness_lufs,
            loudness_range_lu=measured.loudness_range_lu,
            sample_peak_dbfs=measured.sample_peak_dbfs,
            true_peak_dbtp=measured.true_peak_dbtp,
            duration_seconds=plan.video.duration_seconds,
            codec="float_analysis",
        )
        return MixSourceMetrics(
            audio_levels=levels,
            low_frequency_energy_ratio=spectral.low_frequency_energy_ratio,
            spectral_centroid_hz=spectral.spectral_centroid_hz,
            stereo_correlation=spectral.stereo_correlation,
            stereo_side_to_mid_db=spectral.stereo_side_to_mid_db,
        )

    def recommend(self, plan: RenderPlan) -> MixRecommendation:
        """두 입력을 분석하고 실패도 영상별 리포트로 반환한다."""

        with tempfile.TemporaryDirectory(prefix="recordersync-mix-analysis-") as temp_dir:
            manifest_path = Path(temp_dir) / "audio-concat.txt"
            manifest_path.write_text(build_concat_manifest(plan.session), encoding="utf-8")
            try:
                camera = self._measure(plan, manifest_path, MixSource.CAMERA)
            except (RuntimeError, ValueError) as exc:
                return MixRecommendation.failed(f"camera analysis error: {exc}")
            try:
                external = self._measure(plan, manifest_path, MixSource.EXTERNAL)
            except (RuntimeError, ValueError) as exc:
                return MixRecommendation.failed(f"external analysis error: {exc}", camera=camera)
        try:
            return recommend_auto_mix(camera, external)
        except ValueError as exc:
            return MixRecommendation.failed(
                f"policy recommendation error: {exc}",
                camera=camera,
                external=external,
            )
