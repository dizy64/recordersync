"""분석·처리 결과 JSON 계약."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from recordersync.models import AudioChunk, AudioMatch, MatchStatus, RecordingSession
from recordersync.report import MatchReport


def test_match_report_serializes_sessions_matches_and_summary() -> None:
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("a.wav"), 60, 48_000, 2, "pcm_f32le", None),),
    )
    matches = (
        AudioMatch(
            Path("a.mov"),
            10,
            MatchStatus.MATCHED,
            session_id="session-001",
            external_start_seconds=2.5,
            confidence=0.9,
            output_path=Path("replace/a_replaced.mp4"),
        ),
        AudioMatch(Path("b.mov"), 8, MatchStatus.AMBIGUOUS, reason="duplicate"),
    )
    report = MatchReport(
        sessions=(session,),
        matches=matches,
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    payload = json.loads(report.to_json())

    assert payload["version"] == 1
    assert payload["summary"] == {
        "total": 2,
        "matched": 1,
        "unmatched": 0,
        "ambiguous": 1,
        "error": 0,
    }
    assert payload["audio_sessions"][0]["chunks"] == ["a.wav"]
    assert payload["matches"][0]["external_start_seconds"] == 2.5
