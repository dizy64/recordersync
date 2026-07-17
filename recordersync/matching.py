"""다중 대역 특징 생성과 FFT 기반 오디오 구간 매칭."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from scipy.signal import correlate

from recordersync.models import AudioMatch, AudioMatchSegment, MatchStatus

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
    enable_partial: bool = False
    partial_window_seconds: float = 5.0
    min_partial_duration_seconds: float = 5.0
    partial_alignment_tolerance_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not 0 < self.min_confidence <= 1:
            raise ValueError("min_confidence must be in (0, 1]")
        if not 0 <= self.min_peak_margin <= 2:
            raise ValueError("min_peak_margin must be in [0, 2]")
        if self.peak_margin_full_scale <= 0:
            raise ValueError("peak_margin_full_scale must be > 0")
        if self.exclusion_seconds < 0:
            raise ValueError("exclusion_seconds must be >= 0")
        if self.partial_window_seconds <= 0:
            raise ValueError("partial_window_seconds must be > 0")
        if self.min_partial_duration_seconds <= 0:
            raise ValueError("min_partial_duration_seconds must be > 0")
        if self.partial_alignment_tolerance_seconds < 0:
            raise ValueError("partial_alignment_tolerance_seconds must be >= 0")


@dataclass(frozen=True, slots=True)
class _Candidate:
    session_id: str
    frame_index: int
    correlation: float
    hop_seconds: float


@dataclass(frozen=True, slots=True)
class _ReferenceScore:
    status: MatchStatus
    best: _Candidate | None
    correlation: float
    peak_margin: float
    confidence: float
    reason: str | None


@dataclass(frozen=True, slots=True)
class _WindowMatch:
    video_start_frame: int
    video_end_frame: int
    candidate: _Candidate
    correlation: float
    peak_margin: float
    confidence: float


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


def _score_reference(
    reference_features: FloatArray,
    sessions: list[FeatureTimeline],
    options: MatchOptions,
) -> _ReferenceScore:
    candidates: list[_Candidate] = []
    for timeline in sessions:
        candidates.extend(
            _top_candidates(
                timeline,
                reference_features,
                options.exclusion_seconds,
            )
        )
    if not candidates:
        return _ReferenceScore(
            MatchStatus.UNMATCHED,
            None,
            0.0,
            0.0,
            0.0,
            "All recording sessions are shorter than the video feature",
        )

    ordered = sorted(candidates, key=lambda item: item.correlation, reverse=True)
    best = ordered[0]
    second_correlation = ordered[1].correlation if len(ordered) > 1 else -1.0
    peak_margin = max(0.0, best.correlation - second_correlation)
    correlation_score = max(0.0, min(1.0, (best.correlation + 1.0) / 2.0))
    margin_score = max(
        0.0,
        min(1.0, peak_margin / options.peak_margin_full_scale),
    )
    confidence = 0.7 * correlation_score + 0.3 * margin_score

    if confidence < options.min_confidence and correlation_score < options.min_confidence:
        return _ReferenceScore(
            MatchStatus.UNMATCHED,
            best,
            best.correlation,
            peak_margin,
            confidence,
            "Match confidence is below the configured threshold",
        )
    if peak_margin < options.min_peak_margin:
        return _ReferenceScore(
            MatchStatus.AMBIGUOUS,
            best,
            best.correlation,
            peak_margin,
            confidence,
            "Best match is not sufficiently distinct from the runner-up",
        )
    if confidence < options.min_confidence:
        return _ReferenceScore(
            MatchStatus.UNMATCHED,
            best,
            best.correlation,
            peak_margin,
            confidence,
            "Match confidence is below the configured threshold",
        )
    return _ReferenceScore(
        MatchStatus.MATCHED,
        best,
        best.correlation,
        peak_margin,
        confidence,
        None,
    )


def _window_ranges(
    total_frames: int,
    hop_seconds: float,
    options: MatchOptions,
) -> list[tuple[int, int]]:
    minimum_frames = max(2, round(options.min_partial_duration_seconds / hop_seconds))
    if total_frames < minimum_frames:
        return []
    window_frames = max(
        minimum_frames,
        round(options.partial_window_seconds / hop_seconds),
    )
    ranges = [
        (start, min(total_frames, start + window_frames))
        for start in range(0, total_frames, window_frames)
    ]
    if ranges[-1][1] - ranges[-1][0] < minimum_frames:
        final_range = (total_frames - minimum_frames, total_frames)
        ranges[-1] = final_range
    return list(dict.fromkeys(ranges))


def _local_window_match(
    reference_features: FloatArray,
    *,
    video_start_frame: int,
    video_end_frame: int,
    timeline: FeatureTimeline,
    expected_start_frame: int,
    fallback_score: _ReferenceScore,
    options: MatchOptions,
) -> _WindowMatch | None:
    search_frames = max(
        1,
        round(options.partial_alignment_tolerance_seconds / timeline.hop_seconds),
    )
    found_start = _find_local_start(
        timeline.features,
        reference_features,
        expected_start=expected_start_frame,
        search_frames=search_frames,
    )
    found_end = found_start + reference_features.shape[1]
    if found_start < 0 or found_end > timeline.features.shape[1]:
        return None
    correlation = float(
        _correlation_curve(timeline.features[:, found_start:found_end], reference_features)[0]
    )
    correlation_score = max(0.0, min(1.0, (correlation + 1.0) / 2.0))
    if correlation_score < options.min_confidence:
        return None
    return _WindowMatch(
        video_start_frame,
        video_end_frame,
        _Candidate(timeline.session_id, found_start, correlation, timeline.hop_seconds),
        correlation,
        fallback_score.peak_margin,
        correlation_score,
    )


def _global_window_match(
    reference_features: FloatArray,
    *,
    video_start_frame: int,
    video_end_frame: int,
    sessions: list[FeatureTimeline],
    options: MatchOptions,
) -> _WindowMatch | None:
    score = _score_reference(reference_features, sessions, options)
    if score.status is not MatchStatus.MATCHED or score.best is None:
        return None
    return _WindowMatch(
        video_start_frame,
        video_end_frame,
        score.best,
        score.correlation,
        score.peak_margin,
        score.confidence,
    )


def _group_window_matches(
    windows: list[_WindowMatch],
    *,
    tolerance_seconds: float,
) -> list[list[_WindowMatch]]:
    groups: list[list[_WindowMatch]] = []
    for window in windows:
        if not groups:
            groups.append([window])
            continue
        previous = groups[-1][-1]
        frame_tolerance = round(tolerance_seconds / window.candidate.hop_seconds)
        expected_external_start = previous.candidate.frame_index + (
            window.video_start_frame - previous.video_start_frame
        )
        is_contiguous = window.video_start_frame <= previous.video_end_frame
        is_aligned = (
            window.candidate.session_id == previous.candidate.session_id
            and abs(window.candidate.frame_index - expected_external_start) <= frame_tolerance
        )
        if is_contiguous and is_aligned:
            groups[-1].append(window)
        else:
            groups.append([window])
    return groups


def _segment_from_group(
    group: list[_WindowMatch],
    *,
    video_features: FloatArray,
    duration_seconds: float,
    timelines: dict[str, FeatureTimeline],
    options: MatchOptions,
) -> AudioMatchSegment | None:
    first = group[0]
    last = group[-1]
    timeline = timelines[first.candidate.session_id]
    video_start = first.video_start_frame * timeline.hop_seconds
    video_end = (
        duration_seconds
        if last.video_end_frame >= video_features.shape[1]
        else last.video_end_frame * timeline.hop_seconds
    )
    segment_features = video_features[:, first.video_start_frame : last.video_end_frame]
    refined_start, tempo_ratio = refine_feature_alignment(
        timeline.features,
        segment_features,
        coarse_start_frame=first.candidate.frame_index,
        hop_seconds=timeline.hop_seconds,
    )
    available_seconds = (
        (timeline.features.shape[1] - refined_start) * timeline.hop_seconds / tempo_ratio
    )
    segment_duration = min(video_end - video_start, available_seconds)
    if segment_duration + 1e-6 < options.min_partial_duration_seconds:
        return None
    return AudioMatchSegment(
        session_id=first.candidate.session_id,
        video_start_seconds=video_start,
        external_start_seconds=refined_start * timeline.hop_seconds,
        duration_seconds=segment_duration,
        tempo_ratio=tempo_ratio,
        correlation=sum(window.correlation for window in group) / len(group),
        peak_margin=min(window.peak_margin for window in group),
        confidence=sum(window.confidence for window in group) / len(group),
    )


def _find_partial_segments(
    video_features: FloatArray,
    duration_seconds: float,
    sessions: list[FeatureTimeline],
    options: MatchOptions,
    *,
    full_score: _ReferenceScore,
    full_start_frame: int | None,
    full_tempo_ratio: float,
) -> tuple[AudioMatchSegment, ...]:
    if not sessions:
        return ()
    hop_seconds = sessions[0].hop_seconds
    if any(abs(timeline.hop_seconds - hop_seconds) > 1e-9 for timeline in sessions):
        raise ValueError("all session feature timelines must use the same hop_seconds")
    timelines = {timeline.session_id: timeline for timeline in sessions}
    full_timeline = (
        timelines.get(full_score.best.session_id)
        if full_score.best is not None and full_start_frame is not None
        else None
    )

    windows: list[_WindowMatch] = []
    for video_start, video_end in _window_ranges(video_features.shape[1], hop_seconds, options):
        reference = video_features[:, video_start:video_end]
        matched: _WindowMatch | None = None
        if full_timeline is not None and full_start_frame is not None:
            expected_start = full_start_frame + round(video_start * full_tempo_ratio)
            matched = _local_window_match(
                reference,
                video_start_frame=video_start,
                video_end_frame=video_end,
                timeline=full_timeline,
                expected_start_frame=expected_start,
                fallback_score=full_score,
                options=options,
            )
        if matched is None:
            matched = _global_window_match(
                reference,
                video_start_frame=video_start,
                video_end_frame=video_end,
                sessions=sessions,
                options=options,
            )
        if matched is not None:
            windows.append(matched)

    groups = _group_window_matches(
        windows,
        tolerance_seconds=options.partial_alignment_tolerance_seconds,
    )
    segments = [
        _segment_from_group(
            group,
            video_features=video_features,
            duration_seconds=duration_seconds,
            timelines=timelines,
            options=options,
        )
        for group in groups
    ]
    return tuple(segment for segment in segments if segment is not None)


def _failed_match(video_path: Path, duration_seconds: float, score: _ReferenceScore) -> AudioMatch:
    return AudioMatch(
        video_path=video_path,
        duration_seconds=duration_seconds,
        status=score.status,
        correlation=score.correlation,
        peak_margin=score.peak_margin,
        confidence=score.confidence,
        reason=score.reason,
    )


def match_video_features(
    video_path: Path,
    duration_seconds: float,
    video_features: FloatArray,
    sessions: list[FeatureTimeline],
    options: MatchOptions | None = None,
) -> AudioMatch:
    """전체 또는 승인된 부분 구간을 세션에서 찾아 보수적으로 반환한다."""

    resolved_options = options or MatchOptions()
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be > 0")
    if video_features.ndim != 2:
        raise ValueError("video_features must be a 2-D band x frame array")

    full_score = _score_reference(video_features, sessions, resolved_options)
    full_start: int | None = None
    full_tempo_ratio = 1.0
    full_match: AudioMatch | None = None
    if full_score.status is MatchStatus.MATCHED and full_score.best is not None:
        best_timeline = next(
            timeline for timeline in sessions if timeline.session_id == full_score.best.session_id
        )
        full_start, full_tempo_ratio = refine_feature_alignment(
            best_timeline.features,
            video_features,
            coarse_start_frame=full_score.best.frame_index,
            hop_seconds=full_score.best.hop_seconds,
        )
        full_segment = AudioMatchSegment(
            session_id=full_score.best.session_id,
            video_start_seconds=0.0,
            external_start_seconds=full_start * full_score.best.hop_seconds,
            duration_seconds=duration_seconds,
            tempo_ratio=full_tempo_ratio,
            correlation=full_score.correlation,
            peak_margin=full_score.peak_margin,
            confidence=full_score.confidence,
        )
        full_match = AudioMatch(
            video_path=video_path,
            duration_seconds=duration_seconds,
            status=MatchStatus.MATCHED,
            session_id=full_score.best.session_id,
            external_start_seconds=full_segment.external_start_seconds,
            tempo_ratio=full_tempo_ratio,
            correlation=full_score.correlation,
            peak_margin=full_score.peak_margin,
            confidence=full_score.confidence,
            segments=(full_segment,),
        )

    if not resolved_options.enable_partial:
        return full_match or _failed_match(video_path, duration_seconds, full_score)

    segments = _find_partial_segments(
        video_features,
        duration_seconds,
        sessions,
        resolved_options,
        full_score=full_score,
        full_start_frame=full_start,
        full_tempo_ratio=full_tempo_ratio,
    )
    if not segments:
        return full_match or _failed_match(video_path, duration_seconds, full_score)
    if (
        full_match is not None
        and len(segments) == 1
        and segments[0].video_start_seconds <= 1e-6
        and segments[0].video_end_seconds >= duration_seconds - 1e-6
    ):
        return full_match

    matched_duration = sum(segment.duration_seconds for segment in segments)
    status = (
        MatchStatus.MATCHED
        if len(segments) == 1
        and segments[0].video_start_seconds <= 1e-6
        and matched_duration >= duration_seconds - 1e-6
        else MatchStatus.PARTIAL
    )
    session_ids = {segment.session_id for segment in segments}
    return AudioMatch(
        video_path=video_path,
        duration_seconds=duration_seconds,
        status=status,
        session_id=segments[0].session_id if len(session_ids) == 1 else None,
        external_start_seconds=segments[0].external_start_seconds,
        tempo_ratio=segments[0].tempo_ratio,
        correlation=sum(segment.correlation * segment.duration_seconds for segment in segments)
        / matched_duration,
        peak_margin=min(segment.peak_margin for segment in segments),
        confidence=sum(segment.confidence * segment.duration_seconds for segment in segments)
        / matched_duration,
        reason=("Only part of the camera audio matched the external recording"),
        segments=segments,
    )
