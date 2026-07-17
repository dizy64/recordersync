"""분석·처리 결과 JSON 계약."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from recordersync.models import AudioChunk, AudioMatch, MatchStatus, RecordingSession
from recordersync.report import MatchReport, ReportLanguage


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
            output_path=Path("replace/a.mp4"),
        ),
        AudioMatch(
            Path("b.mov"),
            8,
            MatchStatus.AMBIGUOUS,
            reason="Best match is not sufficiently distinct from the runner-up",
        ),
    )
    report = MatchReport(
        sessions=(session,),
        matches=matches,
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    payload = json.loads(report.to_json())

    assert payload["version"] == 1
    assert payload["language"] == "ko"
    assert payload["summary"] == {
        "total": 2,
        "matched": 1,
        "unmatched": 0,
        "ambiguous": 1,
        "error": 0,
    }
    assert payload["audio_sessions"][0]["chunks"] == ["a.wav"]
    assert payload["matches"][0]["external_start_seconds"] == 2.5
    assert payload["matches"][1]["reason"] == (
        "최상위 후보와 차순위 후보의 차이가 충분하지 않습니다."
    )


def test_match_report_can_render_english_reasons() -> None:
    report = MatchReport(
        sessions=(),
        matches=(
            AudioMatch(
                Path("clip.mov"),
                8,
                MatchStatus.UNMATCHED,
                reason="Match confidence is below the configured threshold",
            ),
        ),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    payload = json.loads(report.to_json(language=ReportLanguage.EN))

    assert payload["language"] == "en"
    assert payload["matches"][0]["reason"] == ("Match confidence is below the configured threshold")


def test_match_report_translates_known_prefix_and_preserves_unknown_reason() -> None:
    report = MatchReport(
        sessions=(),
        matches=(
            AudioMatch(
                Path("existing.mov"),
                8,
                MatchStatus.ERROR,
                reason="Output already exists: result.mp4",
            ),
            AudioMatch(
                Path("unknown.mov"),
                8,
                MatchStatus.ERROR,
                reason="codec-specific diagnostic",
            ),
        ),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    payload = report.to_dict()

    assert payload["matches"][0]["reason"] == "출력 파일이 이미 존재합니다: result.mp4"
    assert payload["matches"][1]["reason"] == "codec-specific diagnostic"


def test_match_report_renders_compact_korean_human_summary() -> None:
    report = MatchReport(
        sessions=(),
        matches=(
            AudioMatch(Path("matched.mov"), 8, MatchStatus.MATCHED, confidence=0.9),
            AudioMatch(
                Path("unmatched.mov"),
                8,
                MatchStatus.UNMATCHED,
                confidence=0.42,
                reason="Match confidence is below the configured threshold",
            ),
        ),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    rendered = report.to_text()

    assert rendered.splitlines() == [
        "분석 결과: 1/2개 매칭 (50.0%)",
        "- matched.mov | 매칭 여부: 성공 | 매칭률: 90.0%",
        (
            "- unmatched.mov | 매칭 여부: 실패 | 매칭률: 42.0% | "
            "사유: 매칭 신뢰도가 설정된 기준보다 낮습니다."
        ),
    ]
    assert "session_id" not in rendered
    assert "correlation" not in rendered


def test_match_report_renders_empty_human_summary_without_division_error() -> None:
    report = MatchReport(
        sessions=(),
        matches=(),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    assert report.to_text() == "분석 결과: 0/0개 매칭 (0.0%)"


def test_match_report_always_explains_failed_human_match() -> None:
    report = MatchReport(
        sessions=(),
        matches=(AudioMatch(Path("failed.mov"), 8, MatchStatus.ERROR),),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    assert report.to_text().endswith("사유: 사유를 확인할 수 없습니다.")


def test_match_report_renders_english_human_summary() -> None:
    report = MatchReport(
        sessions=(),
        matches=(
            AudioMatch(
                Path("clip.mov"),
                8,
                MatchStatus.UNMATCHED,
                confidence=0.3,
                reason="Match confidence is below the configured threshold",
            ),
        ),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    assert report.to_text(language=ReportLanguage.EN).splitlines() == [
        "Analysis result: 0/1 matched (0.0%)",
        (
            "- clip.mov | matched: no | match confidence: 30.0% | "
            "reason: Match confidence is below the configured threshold"
        ),
    ]
