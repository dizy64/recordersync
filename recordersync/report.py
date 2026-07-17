"""자동화 가능한 JSON 분석·처리 리포트."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from recordersync.models import AudioMatch, MatchStatus, RecordingSession

REPORT_VERSION = 1


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
                    "reason": _translate_reason(match.reason, language),
                    "output": str(match.output_path) if match.output_path else None,
                }
                for match in self.matches
            ],
        }

    def to_json(self, *, language: ReportLanguage = ReportLanguage.KO) -> str:
        return json.dumps(self.to_dict(language=language), ensure_ascii=False, indent=2)

    def write(self, path: Path, *, language: ReportLanguage = ReportLanguage.KO) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{self.to_json(language=language)}\n", encoding="utf-8")
