"""사람용 분석 목록과 자동화 가능한 JSON 리포트."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from recordersync.models import AudioMatch, MatchStatus, RecordingSession
from recordersync.recommendation import (
    ModeRecommendation,
    RecommendationReason,
    recommend_mode,
)

REPORT_VERSION = 2


class ReportLanguage(StrEnum):
    """사람이 읽는 리포트 사유의 지원 언어."""

    KO = "ko"
    EN = "en"


_KOREAN_REASONS = {
    "All recording sessions are shorter than the video feature": "모든 녹음 세션이 영상 오디오 특징보다 짧습니다.",
    "Match confidence is below the configured threshold": "매칭 신뢰도가 설정된 기준보다 낮습니다.",
    "Best match is not sufficiently distinct from the runner-up": (
        "최상위 후보와 차순위 후보의 차이가 충분하지 않습니다."
    ),
    "Camera audio is required for automatic matching": "자동 매칭에는 카메라 오디오가 필요합니다.",
    "Matched result is missing render metadata": "매칭 결과에 렌더 메타데이터가 없습니다.",
    "Output path must not overwrite the source video": "출력 경로는 원본 영상을 덮어쓸 수 없습니다.",
    "FFmpeg reported success but produced no output file": (
        "FFmpeg가 성공을 보고했지만 출력 파일을 만들지 않았습니다."
    ),
    "mix mode requires camera audio": "mix 모드에는 카메라 오디오가 필요합니다.",
    "fallback mode requires camera audio": "fallback 모드에는 카메라 오디오가 필요합니다.",
    "Only part of the camera audio matched the external recording": "카메라 오디오의 일부만 외부 녹음과 일치합니다.",
}

_KOREAN_REASON_PREFIXES = {
    "Output already exists: ": "출력 파일이 이미 존재합니다: ",
    "No audio stream found: ": "오디오 스트림을 찾을 수 없습니다: ",
    "No video stream found: ": "비디오 스트림을 찾을 수 없습니다: ",
    "Timed out probing media: ": "미디어 정보를 읽는 중 시간 제한을 초과했습니다: ",
    "Failed to probe ": "미디어 정보를 읽지 못했습니다: ",
    "Invalid ffprobe JSON for ": "ffprobe JSON이 올바르지 않습니다: ",
    "Invalid ffprobe payload for ": "ffprobe 결과가 올바르지 않습니다: ",
    "Timed out extracting audio features: ": "오디오 특징을 추출하는 중 시간 제한을 초과했습니다: ",
    "Failed to decode audio from ": "오디오를 디코딩하지 못했습니다: ",
    "Decoded audio is empty: ": "디코딩한 오디오가 비어 있습니다: ",
    "FFmpeg render failed with VideoToolbox and libx265: ": (
        "VideoToolbox와 libx265 FFmpeg 렌더가 모두 실패했습니다: "
    ),
    "Invalid duration: ": "영상 길이가 올바르지 않습니다: ",
}

_RECOMMENDATION_REASONS = {
    ReportLanguage.KO: {
        RecommendationReason.FULL_MATCH: "카메라 오디오 전체가 외부 녹음과 일치합니다.",
        RecommendationReason.RELIABLE_PARTIAL: "충분히 길고 넓은 부분 매칭이 확인되었습니다.",
        RecommendationReason.LOW_CONFIDENCE: "부분 매칭 신뢰도가 안전 추천 기준보다 낮습니다.",
        RecommendationReason.LOW_PEAK_MARGIN: "부분 매칭 후보가 다른 후보와 충분히 구분되지 않습니다.",
        RecommendationReason.LOW_COVERAGE: "일치 구간이 영상의 10%보다 적어 오탐 가능성이 있습니다.",
        RecommendationReason.SHORT_SEGMENTS: "연속 일치 구간이 추천에 필요한 길이보다 짧습니다.",
        RecommendationReason.UNMATCHED: "신뢰할 수 있는 일치 구간이 없습니다.",
        RecommendationReason.AMBIGUOUS: "후보가 불분명해 자동 처리를 권장하지 않습니다.",
        RecommendationReason.ERROR: "분석 오류가 있어 처리를 권장하지 않습니다.",
    },
    ReportLanguage.EN: {
        RecommendationReason.FULL_MATCH: "The full camera audio matches the external recording.",
        RecommendationReason.RELIABLE_PARTIAL: "A sufficiently long and well-covered partial match is available.",
        RecommendationReason.LOW_CONFIDENCE: "Partial-match confidence is below the safe recommendation threshold.",
        RecommendationReason.LOW_PEAK_MARGIN: "The partial match is not sufficiently distinct from other candidates.",
        RecommendationReason.LOW_COVERAGE: (
            "Matched segments cover less than 10% of the video and may be false positives."
        ),
        RecommendationReason.SHORT_SEGMENTS: (
            "Contiguous matched segments are too short for an automatic recommendation."
        ),
        RecommendationReason.UNMATCHED: "No reliable matching segment is available.",
        RecommendationReason.AMBIGUOUS: "The candidates are ambiguous, so automatic processing is not recommended.",
        RecommendationReason.ERROR: "An analysis error prevents an automatic processing recommendation.",
    },
}


def _translate_reason(reason: str | None, language: ReportLanguage) -> str | None:
    if reason is None or language is ReportLanguage.EN:
        return reason
    translated = _KOREAN_REASONS.get(reason)
    if translated is not None:
        return translated
    for prefix, translated_prefix in _KOREAN_REASON_PREFIXES.items():
        if reason.startswith(prefix):
            return f"{translated_prefix}{reason.removeprefix(prefix)}"
    return reason


def _recommendation_reason(
    recommendation: ModeRecommendation,
    language: ReportLanguage,
) -> str:
    return _RECOMMENDATION_REASONS[language][recommendation.reason]


def _recommendation_options(recommendation: ModeRecommendation) -> dict[str, float]:
    if recommendation.minimum_contiguous_seconds is None:
        return {}
    return {"min_partial_seconds": recommendation.minimum_contiguous_seconds}


def _match_payload(match: AudioMatch, language: ReportLanguage) -> dict[str, object]:
    recommendation = recommend_mode(match)
    return {
        "video": str(match.video_path),
        "status": match.status.value,
        "session_id": match.session_id,
        "external_start_seconds": match.external_start_seconds,
        "duration_seconds": match.duration_seconds,
        "tempo_ratio": match.tempo_ratio,
        "correlation": match.correlation,
        "peak_margin": match.peak_margin,
        "confidence": match.confidence,
        "coverage_ratio": match.coverage_ratio,
        "segments": [
            {
                "session_id": segment.session_id,
                "video_start_seconds": segment.video_start_seconds,
                "external_start_seconds": segment.external_start_seconds,
                "duration_seconds": segment.duration_seconds,
                "tempo_ratio": segment.tempo_ratio,
                "correlation": segment.correlation,
                "peak_margin": segment.peak_margin,
                "confidence": segment.confidence,
            }
            for segment in match.segments
        ],
        "reason": _translate_reason(match.reason, language),
        "output": str(match.output_path) if match.output_path else None,
        "recommended_mode": recommendation.mode.value if recommendation.mode else None,
        "recommendation_reason": _recommendation_reason(recommendation, language),
        "recommended_options": _recommendation_options(recommendation),
    }


@dataclass(frozen=True, slots=True)
class MatchReport:
    sessions: tuple[RecordingSession, ...]
    matches: tuple[AudioMatch, ...]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    recommended_command: tuple[str, ...] | None = None
    include_recommended_command: bool = False

    def _summary(self) -> dict[str, int]:
        return {
            "total": len(self.matches),
            "matched": sum(match.status is MatchStatus.MATCHED for match in self.matches),
            "partial": sum(match.status is MatchStatus.PARTIAL for match in self.matches),
            "unmatched": sum(match.status is MatchStatus.UNMATCHED for match in self.matches),
            "ambiguous": sum(match.status is MatchStatus.AMBIGUOUS for match in self.matches),
            "error": sum(match.status is MatchStatus.ERROR for match in self.matches),
        }

    def to_dict(self, *, language: ReportLanguage = ReportLanguage.KO) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": REPORT_VERSION,
            "language": language.value,
            "created_at": self.created_at.isoformat(),
            "summary": self._summary(),
            "audio_sessions": [
                {
                    "id": session.id,
                    "duration_seconds": session.duration_seconds,
                    "chunks": [str(chunk.path) for chunk in session.chunks],
                }
                for session in self.sessions
            ],
            "matches": [_match_payload(match, language) for match in self.matches],
        }
        if self.include_recommended_command:
            payload["recommended_command"] = (
                list(self.recommended_command) if self.recommended_command is not None else None
            )
        return payload

    def to_json(self, *, language: ReportLanguage = ReportLanguage.KO) -> str:
        return json.dumps(self.to_dict(language=language), ensure_ascii=False, indent=2)

    def to_text(self, *, language: ReportLanguage = ReportLanguage.KO) -> str:
        """영상별 핵심 매칭 결과만 사람이 읽기 쉬운 목록으로 반환한다."""

        summary = self._summary()
        total = summary["total"]
        matched = summary["matched"]
        partial = summary["partial"]
        overall_rate = matched / total * 100 if total else 0.0

        if language is ReportLanguage.KO:
            summary_line = (
                f"분석 결과: {matched}/{total}개 전체 매칭, {partial}개 부분 매칭"
                if partial
                else f"분석 결과: {matched}/{total}개 매칭 ({overall_rate:.1f}%)"
            )
            matched_label, yes, partial_value, no = "매칭 여부", "성공", "부분", "실패"
            confidence_label, reason_label = "매칭률", "사유"
            coverage_label, segment_label = "레코더 사용", "구간"
            recommendation_label, hold = "추천", "처리 보류"
            recommendation_reason_label = "추천 사유"
            missing_reason = "사유를 확인할 수 없습니다."
        else:
            summary_line = (
                f"Analysis result: {matched}/{total} fully matched, {partial} partially matched"
                if partial
                else f"Analysis result: {matched}/{total} matched ({overall_rate:.1f}%)"
            )
            matched_label, yes, partial_value, no = "matched", "yes", "partial", "no"
            confidence_label, reason_label = "match confidence", "reason"
            coverage_label, segment_label = "recorder coverage", "segments"
            recommendation_label, hold = "recommendation", "hold"
            recommendation_reason_label = "recommendation reason"
            missing_reason = "reason unavailable"

        lines = [summary_line]
        for match in self.matches:
            recommendation = recommend_mode(match)
            is_matched = match.status is MatchStatus.MATCHED
            is_partial = match.status is MatchStatus.PARTIAL
            match_value = partial_value if is_partial else (yes if is_matched else no)
            line = (
                f"- {match.video_path.name} | {matched_label}: {match_value} | "
                f"{confidence_label}: {match.confidence * 100:.1f}%"
            )
            if is_partial:
                segment_count = (
                    f"{len(match.segments)}개" if language is ReportLanguage.KO else str(len(match.segments))
                )
                line = (
                    f"{line} | {coverage_label}: {match.coverage_ratio * 100:.1f}% | {segment_label}: {segment_count}"
                )
            elif not is_matched:
                reason = _translate_reason(match.reason, language) or missing_reason
                line = f"{line} | {reason_label}: {reason}"
            recommendation_value = recommendation.mode.value if recommendation.mode is not None else hold
            line = f"{line} | {recommendation_label}: {recommendation_value}"
            if is_partial:
                line = f"{line} | {recommendation_reason_label}: {_recommendation_reason(recommendation, language)}"
            lines.append(line)
        if self.include_recommended_command:
            lines.append("")
            if self.recommended_command is not None:
                command_label = "추천 실행" if language is ReportLanguage.KO else "Recommended command"
                lines.extend((f"{command_label}:", f"  {shlex.join(self.recommended_command)}"))
            elif language is ReportLanguage.KO:
                lines.append("추천 실행 명령 없음: 신뢰할 수 있는 매칭이 없습니다.")
            else:
                lines.append("No recommended command: no reliable match is available.")
        return "\n".join(lines)

    def write(self, path: Path, *, language: ReportLanguage = ReportLanguage.KO) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{self.to_json(language=language)}\n", encoding="utf-8")
