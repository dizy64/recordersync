"""매칭 결과에 따른 보수적인 처리 모드 추천 정책."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from recordersync.models import AudioMatch, MatchStatus

MIN_RECOMMENDED_CONFIDENCE = 0.75
MIN_RECOMMENDED_PEAK_MARGIN = 0.05
MIN_RECOMMENDED_COVERAGE_RATIO = 0.10
MAX_REQUIRED_CONTIGUOUS_SECONDS = 30.0
REQUIRED_CONTIGUOUS_DURATION_RATIO = 0.25


class RecommendationMode(StrEnum):
    """분석 결과로 안전하게 권장할 수 있는 렌더 모드."""

    REPLACE = "replace"
    FALLBACK = "fallback"


class RecommendationReason(StrEnum):
    """번역 가능한 추천 판정 사유 코드."""

    FULL_MATCH = "full_match"
    RELIABLE_PARTIAL = "reliable_partial"
    LOW_CONFIDENCE = "low_confidence"
    LOW_PEAK_MARGIN = "low_peak_margin"
    LOW_COVERAGE = "low_coverage"
    SHORT_SEGMENTS = "short_segments"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ModeRecommendation:
    """추천 모드와 판단 근거."""

    mode: RecommendationMode | None
    reason: RecommendationReason
    minimum_contiguous_seconds: float | None = None


def _required_contiguous_seconds(duration_seconds: float) -> float:
    return min(
        MAX_REQUIRED_CONTIGUOUS_SECONDS,
        duration_seconds * REQUIRED_CONTIGUOUS_DURATION_RATIO,
    )


def recommend_mode(match: AudioMatch) -> ModeRecommendation:
    """오탐 위험을 우선해 매칭 결과의 다음 처리 모드를 추천한다."""

    if match.status is MatchStatus.MATCHED:
        return ModeRecommendation(RecommendationMode.REPLACE, RecommendationReason.FULL_MATCH)
    if match.status is MatchStatus.UNMATCHED:
        return ModeRecommendation(None, RecommendationReason.UNMATCHED)
    if match.status is MatchStatus.AMBIGUOUS:
        return ModeRecommendation(None, RecommendationReason.AMBIGUOUS)
    if match.status is MatchStatus.ERROR:
        return ModeRecommendation(None, RecommendationReason.ERROR)

    required_contiguous_seconds = _required_contiguous_seconds(match.duration_seconds)
    if match.confidence < MIN_RECOMMENDED_CONFIDENCE:
        return ModeRecommendation(None, RecommendationReason.LOW_CONFIDENCE)
    if match.peak_margin < MIN_RECOMMENDED_PEAK_MARGIN:
        return ModeRecommendation(None, RecommendationReason.LOW_PEAK_MARGIN)
    if match.coverage_ratio < MIN_RECOMMENDED_COVERAGE_RATIO:
        return ModeRecommendation(None, RecommendationReason.LOW_COVERAGE)
    if max(segment.duration_seconds for segment in match.segments) < required_contiguous_seconds:
        return ModeRecommendation(None, RecommendationReason.SHORT_SEGMENTS)
    return ModeRecommendation(
        RecommendationMode.FALLBACK,
        RecommendationReason.RELIABLE_PARTIAL,
        minimum_contiguous_seconds=required_contiguous_seconds,
    )


def recommend_batch_mode(matches: Iterable[AudioMatch]) -> ModeRecommendation:
    """배치 전체를 한 번에 안전하게 처리할 렌더 모드를 추천한다."""

    recommendations = tuple(recommend_mode(match) for match in matches)
    fallback_recommendations = tuple(
        recommendation
        for recommendation in recommendations
        if recommendation.mode is RecommendationMode.FALLBACK
    )
    if fallback_recommendations:
        if any(
            recommendation.minimum_contiguous_seconds is None
            for recommendation in fallback_recommendations
        ):
            raise ValueError("fallback recommendation requires a contiguous duration")
        minimum_contiguous_seconds = max(
            recommendation.minimum_contiguous_seconds
            for recommendation in fallback_recommendations
            if recommendation.minimum_contiguous_seconds is not None
        )
        return ModeRecommendation(
            RecommendationMode.FALLBACK,
            RecommendationReason.RELIABLE_PARTIAL,
            minimum_contiguous_seconds=minimum_contiguous_seconds,
        )
    if any(recommendation.mode is RecommendationMode.REPLACE for recommendation in recommendations):
        return ModeRecommendation(RecommendationMode.REPLACE, RecommendationReason.FULL_MATCH)
    return ModeRecommendation(None, RecommendationReason.UNMATCHED)
