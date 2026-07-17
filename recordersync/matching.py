"""다중 대역 특징 생성과 FFT 기반 오디오 구간 매칭."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from scipy.signal import correlate

from recordersync.models import AudioMatch, MatchStatus

FloatArray = NDArray[np.float32]

_BAND_EDGES_HZ = (80.0, 200.0, 400.0, 800.0, 1_600.0, 3_200.0, 4_000.0)


@dataclass(frozen=True, slots=True)
class FeatureTimeline:
    """한 녹음 세션의 시간축 오디오 특징."""

    session_id: str
    features: FloatArray
    hop_seconds: float

    def __post_init__(self) -> None:
        if self.features.ndim != 2:
            raise ValueError("features must be a 2-D band x frame array")
        if self.hop_seconds <= 0:
            raise ValueError("hop_seconds must be > 0")


@dataclass(frozen=True, slots=True)
class MatchOptions:
    """자동 매칭의 보수적 승인 기준."""

    min_confidence: float = 0.75
    min_peak_margin: float = 0.05
    peak_margin_full_scale: float = 0.15
    exclusion_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not 0 < self.min_confidence <= 1:
            raise ValueError("min_confidence must be in (0, 1]")
        if not 0 <= self.min_peak_margin <= 2:
            raise ValueError("min_peak_margin must be in [0, 2]")
        if self.peak_margin_full_scale <= 0:
            raise ValueError("peak_margin_full_scale must be > 0")
        if self.exclusion_seconds < 0:
            raise ValueError("exclusion_seconds must be >= 0")


@dataclass(frozen=True, slots=True)
class _Candidate:
    session_id: str
    frame_index: int
    correlation: float
    hop_seconds: float


def _normalize_bands(features: NDArray[np.floating]) -> FloatArray:
    stable_features = features.astype(np.float64)
    means = stable_features.mean(axis=1, keepdims=True)
    stddevs = stable_features.std(axis=1, keepdims=True)
    normalized = np.zeros_like(stable_features)
    np.divide(stable_features - means, stddevs, out=normalized, where=stddevs > 1e-5)
    return normalized.astype(np.float32)


def build_multiband_features(
    samples: FloatArray,
    *,
    sample_rate: int = 8_000,
    frame_seconds: float = 0.1,
    hop_seconds: float = 0.05,
    block_frames: int = 4_096,
) -> FloatArray:
    """PCM을 서로 다른 마이크에도 비교 가능한 6대역 log-energy 특징으로 바꾼다."""

    if sample_rate <= 0:
        raise ValueError("sample_rate must be > 0")
    if frame_seconds <= 0 or hop_seconds <= 0:
        raise ValueError("frame_seconds and hop_seconds must be > 0")
    if samples.ndim != 1:
        raise ValueError("samples must be mono")

    frame_size = max(2, round(sample_rate * frame_seconds))
    hop_size = max(1, round(sample_rate * hop_seconds))
    if samples.size < frame_size:
        raise ValueError("audio is too short to extract features")

    frame_count = 1 + (samples.size - frame_size) // hop_size
    frequencies = np.fft.rfftfreq(frame_size, d=1.0 / sample_rate)
    band_masks = [
        (frequencies >= low) & (frequencies < high) for low, high in pairwise(_BAND_EDGES_HZ)
    ]
    if any(not mask.any() for mask in band_masks):
        raise ValueError("sample_rate is too low for configured frequency bands")

    window = np.hanning(frame_size).astype(np.float32)
    output = np.empty((len(band_masks), frame_count), dtype=np.float32)
    all_windows = np.lib.stride_tricks.sliding_window_view(samples, frame_size)

    for block_start in range(0, frame_count, block_frames):
        block_end = min(frame_count, block_start + block_frames)
        sample_starts = np.arange(block_start, block_end) * hop_size
        frames = all_windows[sample_starts] * window
        spectrum = np.fft.rfft(frames, axis=1)
        power = np.square(np.abs(spectrum))
        for band_index, mask in enumerate(band_masks):
            output[band_index, block_start:block_end] = np.log1p(power[:, mask].mean(axis=1))

    return _normalize_bands(output)


def _correlation_curve(session: FloatArray, video: FloatArray) -> FloatArray:
    _, video_frames = video.shape
    combined = np.zeros(session.shape[1] - video_frames + 1, dtype=np.float64)
    active_bands = 0
    for band_index in range(video.shape[0]):
        reference = video[band_index].astype(np.float64)
        reference -= reference.mean()
        reference_energy = float(np.dot(reference, reference))
        if reference_energy <= 1e-8:
            continue

        signal = session[band_index].astype(np.float64)
        numerator = correlate(
            signal,
            reference,
            mode="valid",
            method="fft",
        )
        prefix_sum = np.concatenate(([0.0], np.cumsum(signal)))
        prefix_square = np.concatenate(([0.0], np.cumsum(np.square(signal))))
        local_sum = prefix_sum[video_frames:] - prefix_sum[:-video_frames]
        local_square = prefix_square[video_frames:] - prefix_square[:-video_frames]
        local_energy = np.maximum(
            0.0,
            local_square - np.square(local_sum) / video_frames,
        )
        denominator = np.sqrt(reference_energy * local_energy)
        band_curve = np.zeros_like(numerator)
        np.divide(numerator, denominator, out=band_curve, where=denominator > 1e-8)
        combined += np.clip(band_curve, -1.0, 1.0)
        active_bands += 1
    if active_bands == 0:
        return np.zeros_like(combined, dtype=np.float32)
    combined /= active_bands
    return combined.astype(np.float32)


def _top_candidates(
    timeline: FeatureTimeline,
    video_features: FloatArray,
    exclusion_seconds: float,
) -> list[_Candidate]:
    if timeline.features.shape[0] != video_features.shape[0]:
        raise ValueError("session and video feature band counts differ")
    if timeline.features.shape[1] < video_features.shape[1]:
        return []

    curve = _correlation_curve(timeline.features, video_features)
    best_index = int(np.argmax(curve))
    candidates = [
        _Candidate(timeline.session_id, best_index, float(curve[best_index]), timeline.hop_seconds)
    ]

    exclusion_frames = max(1, round(exclusion_seconds / timeline.hop_seconds))
    masked = curve.copy()
    left = max(0, best_index - exclusion_frames)
    right = min(masked.size, best_index + exclusion_frames + 1)
    masked[left:right] = -np.inf
    if np.isfinite(masked).any():
        second_index = int(np.argmax(masked))
        candidates.append(
            _Candidate(
                timeline.session_id,
                second_index,
                float(masked[second_index]),
                timeline.hop_seconds,
            )
        )
    return candidates


def _find_local_start(
    session_features: FloatArray,
    reference_features: FloatArray,
    *,
    expected_start: int,
    search_frames: int,
) -> int:
    region_start = max(0, expected_start - search_frames)
    region_end = min(
        session_features.shape[1],
        expected_start + search_frames + reference_features.shape[1],
    )
    region = session_features[:, region_start:region_end]
    if region.shape[1] < reference_features.shape[1]:
        return expected_start
    curve = _correlation_curve(region, reference_features)
    return region_start + int(np.argmax(curve))


def refine_feature_alignment(
    session_features: FloatArray,
    video_features: FloatArray,
    *,
    coarse_start_frame: int,
    hop_seconds: float,
    window_seconds: float = 60.0,
    search_seconds: float = 5.0,
) -> tuple[int, float]:
    """클립 시작·끝 특징을 다시 찾아 정밀 시작점과 recorder clock 비율을 구한다."""

    if hop_seconds <= 0:
        raise ValueError("hop_seconds must be > 0")
    video_frames = video_features.shape[1]
    if video_frames * hop_seconds < 30.0:
        return coarse_start_frame, 1.0

    requested_window = max(2, round(window_seconds / hop_seconds))
    window_frames = min(requested_window, max(2, video_frames // 4))
    reference_span = video_frames - window_frames
    if reference_span <= 0:
        return coarse_start_frame, 1.0

    search_frames = max(1, round(search_seconds / hop_seconds))
    head_start = _find_local_start(
        session_features,
        video_features[:, :window_frames],
        expected_start=coarse_start_frame,
        search_frames=search_frames,
    )
    expected_tail = coarse_start_frame + reference_span
    tail_start = _find_local_start(
        session_features,
        video_features[:, -window_frames:],
        expected_start=expected_tail,
        search_frames=search_frames,
    )
    observed_span = tail_start - head_start
    if observed_span <= 0:
        return coarse_start_frame, 1.0
    tempo_ratio = observed_span / reference_span
    if not 0.5 <= tempo_ratio <= 2.0:
        return coarse_start_frame, 1.0
    return head_start, tempo_ratio


def match_video_features(
    video_path: Path,
    duration_seconds: float,
    video_features: FloatArray,
    sessions: list[FeatureTimeline],
    options: MatchOptions | None = None,
) -> AudioMatch:
    """모든 세션을 독립 탐색하고 유일하며 신뢰도 높은 최상위 구간만 승인한다."""

    resolved_options = options or MatchOptions()
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be > 0")
    if video_features.ndim != 2:
        raise ValueError("video_features must be a 2-D band x frame array")

    candidates: list[_Candidate] = []
    for timeline in sessions:
        candidates.extend(
            _top_candidates(
                timeline,
                video_features,
                resolved_options.exclusion_seconds,
            )
        )
    if not candidates:
        return AudioMatch(
            video_path=video_path,
            duration_seconds=duration_seconds,
            status=MatchStatus.UNMATCHED,
            reason="All recording sessions are shorter than the video feature",
        )

    ordered = sorted(candidates, key=lambda item: item.correlation, reverse=True)
    best = ordered[0]
    second_correlation = ordered[1].correlation if len(ordered) > 1 else -1.0
    peak_margin = max(0.0, best.correlation - second_correlation)
    correlation_score = max(0.0, min(1.0, (best.correlation + 1.0) / 2.0))
    margin_score = max(
        0.0,
        min(1.0, peak_margin / resolved_options.peak_margin_full_scale),
    )
    confidence = 0.7 * correlation_score + 0.3 * margin_score

    if confidence < resolved_options.min_confidence and correlation_score < (
        resolved_options.min_confidence
    ):
        return AudioMatch(
            video_path=video_path,
            duration_seconds=duration_seconds,
            status=MatchStatus.UNMATCHED,
            correlation=best.correlation,
            peak_margin=peak_margin,
            confidence=confidence,
            reason="Match confidence is below the configured threshold",
        )
    if peak_margin < resolved_options.min_peak_margin:
        return AudioMatch(
            video_path=video_path,
            duration_seconds=duration_seconds,
            status=MatchStatus.AMBIGUOUS,
            correlation=best.correlation,
            peak_margin=peak_margin,
            confidence=confidence,
            reason="Best match is not sufficiently distinct from the runner-up",
        )
    if confidence < resolved_options.min_confidence:
        return AudioMatch(
            video_path=video_path,
            duration_seconds=duration_seconds,
            status=MatchStatus.UNMATCHED,
            correlation=best.correlation,
            peak_margin=peak_margin,
            confidence=confidence,
            reason="Match confidence is below the configured threshold",
        )

    best_timeline = next(
        timeline for timeline in sessions if timeline.session_id == best.session_id
    )
    refined_start, tempo_ratio = refine_feature_alignment(
        best_timeline.features,
        video_features,
        coarse_start_frame=best.frame_index,
        hop_seconds=best.hop_seconds,
    )
    return AudioMatch(
        video_path=video_path,
        duration_seconds=duration_seconds,
        status=MatchStatus.MATCHED,
        session_id=best.session_id,
        external_start_seconds=refined_start * best.hop_seconds,
        tempo_ratio=tempo_ratio,
        correlation=best.correlation,
        peak_margin=peak_margin,
        confidence=confidence,
    )
