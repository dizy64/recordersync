"""분석 결과의 보수적인 처리 모드 추천 정책."""

from pathlib import Path

import pytest

from recordersync.models import AudioMatch, AudioMatchSegment, MatchStatus
from recordersync.recommendation import (
    RecommendationMode,
    RecommendationReason,
    recommend_batch_mode,
    recommend_mode,
)


def _partial_match(
    *,
    duration_seconds: float,
    segment_durations: tuple[float, ...],
    confidence: float = 0.9,
    peak_margin: float = 0.1,
) -> AudioMatch:
    video_start = 0.0
    segments: list[AudioMatchSegment] = []
    for segment_duration in segment_durations:
        segments.append(
            AudioMatchSegment(
                session_id="session-001",
                video_start_seconds=video_start,
                external_start_seconds=100.0 + video_start,
                duration_seconds=segment_duration,
                confidence=confidence,
                peak_margin=peak_margin,
            )
        )
        video_start += segment_duration + 1.0
    return AudioMatch(
        Path("clip.mov"),
        duration_seconds,
        MatchStatus.PARTIAL,
        confidence=confidence,
        peak_margin=peak_margin,
        segments=tuple(segments),
    )


def test_전체_매칭은_replace를_추천한다() -> None:
    match = AudioMatch(Path("clip.mov"), 60, MatchStatus.MATCHED)

    recommendation = recommend_mode(match)

    assert recommendation.mode is RecommendationMode.REPLACE
    assert recommendation.reason is RecommendationReason.FULL_MATCH
    assert recommendation.minimum_contiguous_seconds is None


def test_긴_구간과_충분한_커버리지가_있는_부분_매칭은_fallback을_추천한다() -> None:
    match = _partial_match(duration_seconds=1000, segment_durations=(120, 360))

    recommendation = recommend_mode(match)

    assert recommendation.mode is RecommendationMode.FALLBACK
    assert recommendation.reason is RecommendationReason.RELIABLE_PARTIAL
    assert recommendation.minimum_contiguous_seconds == pytest.approx(30.0)


def test_짧은_영상은_영상_길이에_비례한_연속_구간으로_fallback을_추천한다() -> None:
    match = _partial_match(duration_seconds=20, segment_durations=(5,))

    recommendation = recommend_mode(match)

    assert recommendation.mode is RecommendationMode.FALLBACK
    assert recommendation.minimum_contiguous_seconds == pytest.approx(5.0)


def test_커버리지가_낮은_부분_매칭은_처리를_보류한다() -> None:
    match = _partial_match(duration_seconds=1000, segment_durations=(40,))

    recommendation = recommend_mode(match)

    assert recommendation.mode is None
    assert recommendation.reason is RecommendationReason.LOW_COVERAGE


def test_분산된_짧은_부분_매칭은_처리를_보류한다() -> None:
    match = _partial_match(duration_seconds=100, segment_durations=(10, 10))

    recommendation = recommend_mode(match)

    assert recommendation.mode is None
    assert recommendation.reason is RecommendationReason.SHORT_SEGMENTS


@pytest.mark.parametrize(
    ("confidence", "peak_margin", "reason"),
    [
        (0.74, 0.1, RecommendationReason.LOW_CONFIDENCE),
        (0.9, 0.049, RecommendationReason.LOW_PEAK_MARGIN),
    ],
)
def test_안전_임계값보다_낮은_부분_매칭은_처리를_보류한다(
    confidence: float,
    peak_margin: float,
    reason: RecommendationReason,
) -> None:
    match = _partial_match(
        duration_seconds=100,
        segment_durations=(30,),
        confidence=confidence,
        peak_margin=peak_margin,
    )

    recommendation = recommend_mode(match)

    assert recommendation.mode is None
    assert recommendation.reason is reason


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (MatchStatus.UNMATCHED, RecommendationReason.UNMATCHED),
        (MatchStatus.AMBIGUOUS, RecommendationReason.AMBIGUOUS),
        (MatchStatus.ERROR, RecommendationReason.ERROR),
    ],
)
def test_승인되지_않은_매칭은_처리를_보류한다(
    status: MatchStatus,
    reason: RecommendationReason,
) -> None:
    match = AudioMatch(Path("clip.mov"), 60, status)

    recommendation = recommend_mode(match)

    assert recommendation.mode is None
    assert recommendation.reason is reason


def test_배치에_안전한_부분_매칭이_있으면_fallback을_추천한다() -> None:
    matched = AudioMatch(Path("full.mov"), 60, MatchStatus.MATCHED)
    first_partial = _partial_match(duration_seconds=100, segment_durations=(30,))
    second_partial = _partial_match(duration_seconds=200, segment_durations=(60,))

    recommendation = recommend_batch_mode((matched, first_partial, second_partial))

    assert recommendation.mode is RecommendationMode.FALLBACK
    assert recommendation.minimum_contiguous_seconds == pytest.approx(30.0)


def test_배치에_전체_매칭만_있으면_replace를_추천한다() -> None:
    matches = (
        AudioMatch(Path("first.mov"), 60, MatchStatus.MATCHED),
        AudioMatch(Path("second.mov"), 60, MatchStatus.UNMATCHED),
    )

    recommendation = recommend_batch_mode(matches)

    assert recommendation.mode is RecommendationMode.REPLACE
    assert recommendation.minimum_contiguous_seconds is None


def test_배치에_처리할_매칭이_없으면_모드를_추천하지_않는다() -> None:
    matches = (
        AudioMatch(Path("first.mov"), 60, MatchStatus.UNMATCHED),
        AudioMatch(Path("second.mov"), 60, MatchStatus.AMBIGUOUS),
    )

    recommendation = recommend_batch_mode(matches)

    assert recommendation.mode is None
    assert recommendation.minimum_contiguous_seconds is None
