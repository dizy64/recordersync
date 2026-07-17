"""사람용 분석 목록과 자동화 가능한 JSON 리포트."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from recordersync.models import AudioMatch, MatchStatus, RecordingSession

REPORT_VERSION = 2


class ReportLanguage(StrEnum):
    """사람이 읽는 리포트 사유의 지원 언어."""

    KO = "ko"
    EN = "en"


_KOREAN_REASONS = {
    "All recording sessions are shorter than the video feature": (
        "모든 녹음 세션이 영상 오디오 특징보다 짧습니다."
    ),
    "Match confidence is below the configured threshold": (
        "매칭 신뢰도가 설정된 기준보다 낮습니다."
    ),
    "Best match is not sufficiently distinct from the runner-up": (
        "최상위 후보와 차순위 후보의 차이가 충분하지 않습니다."
    ),
    "Camera audio is required for automatic matching": (
        "자동 매칭에는 카메라 오디오가 필요합니다."
    ),
    "Matched result is missing render metadata": "매칭 결과에 렌더 메타데이터가 없습니다.",
    "Output path must not overwrite the source video": (
        "출력 경로는 원본 영상을 덮어쓸 수 없습니다."
    ),
    "FFmpeg reported success but produced no output file": (
        "FFmpeg가 성공을 보고했지만 출력 파일을 만들지 않았습니다."
    ),
    "mix mode requires camera audio": "mix 모드에는 카메라 오디오가 필요합니다.",
    "fallback mode requires camera audio": "fallback 모드에는 카메라 오디오가 필요합니다.",
    "Only part of the camera audio matched the external recording": (
        "카메라 오디오의 일부만 외부 녹음과 일치합니다."
    ),
}

_KOREAN_REASON_PREFIXES = {
    "Output already exists: ": "출력 파일이 이미 존재합니다: ",
    "No audio stream found: ": "오디오 스트림을 찾을 수 없습니다: ",
    "No video stream found: ": "비디오 스트림을 찾을 수 없습니다: ",
    "Timed out probing media: ": "미디어 정보를 읽는 중 시간 제한을 초과했습니다: ",
    "Failed to probe ": "미디어 정보를 읽지 못했습니다: ",
    "Invalid ffprobe JSON for ": "ffprobe JSON이 올바르지 않습니다: ",
    "Invalid ffprobe payload for ": "ffprobe 결과가 올바르지 않습니다: ",
    "Timed out extracting audio features: ": (
        "오디오 특징을 추출하는 중 시간 제한을 초과했습니다: "
    ),
    "Failed to decode audio from ": "오디오를 디코딩하지 못했습니다: ",
    "Decoded audio is empty: ": "디코딩한 오디오가 비어 있습니다: ",
    "FFmpeg render failed with VideoToolbox and libx265: ": (
        "VideoToolbox와 libx265 FFmpeg 렌더가 모두 실패했습니다: "
    ),
    "Invalid duration: ": "영상 길이가 올바르지 않습니다: ",
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


@dataclass(frozen=True, slots=True)
class MatchReport:
    sessions: tuple[RecordingSession, ...]
    matches: tuple[AudioMatch, ...]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

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
        return {
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
            "matches": [
                {
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
                }
                for match in self.matches
            ],
        }

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
            missing_reason = "reason unavailable"

        lines = [summary_line]
        for match in self.matches:
            is_matched = match.status is MatchStatus.MATCHED
            is_partial = match.status is MatchStatus.PARTIAL
            match_value = partial_value if is_partial else (yes if is_matched else no)
            line = (
                f"- {match.video_path.name} | {matched_label}: {match_value} | "
                f"{confidence_label}: {match.confidence * 100:.1f}%"
            )
            if is_partial:
                segment_count = (
                    f"{len(match.segments)}개"
                    if language is ReportLanguage.KO
                    else str(len(match.segments))
                )
                line = (
                    f"{line} | {coverage_label}: {match.coverage_ratio * 100:.1f}% | "
                    f"{segment_label}: {segment_count}"
                )
            elif not is_matched:
                reason = _translate_reason(match.reason, language) or missing_reason
                line = f"{line} | {reason_label}: {reason}"
            lines.append(line)
        return "\n".join(lines)

    def write(self, path: Path, *, language: ReportLanguage = ReportLanguage.KO) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{self.to_json(language=language)}\n", encoding="utf-8")
