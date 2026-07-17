"""자동화 가능한 JSON 분석·처리 리포트."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from recordersync.models import AudioMatch, MatchStatus, RecordingSession

REPORT_VERSION = 1


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

    def to_dict(self) -> dict[str, object]:
        return {
            "version": REPORT_VERSION,
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
                    "reason": match.reason,
                    "output": str(match.output_path) if match.output_path else None,
                }
                for match in self.matches
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{self.to_json()}\n", encoding="utf-8")
