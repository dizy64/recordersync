"""분석·처리 결과 JSON 계약."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from recordersync.models import (
    AudioChunk,
    AudioMatch,
    AudioMatchSegment,
    MatchStatus,
    RecordingSession,
)
from recordersync.report import MatchReport, ReportLanguage


def test_매칭_리포트는_세션과_매칭과_요약을_직렬화한다() -> None:
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

    assert payload["version"] == 2
    assert payload["language"] == "ko"
    assert payload["summary"] == {
        "total": 2,
        "matched": 1,
        "partial": 0,
        "unmatched": 0,
        "ambiguous": 1,
        "error": 0,
    }
    assert payload["audio_sessions"][0]["chunks"] == ["a.wav"]
    assert payload["matches"][0]["external_start_seconds"] == 2.5
    assert payload["matches"][1]["reason"] == (
        "최상위 후보와 차순위 후보의 차이가 충분하지 않습니다."
    )


def test_매칭_리포트는_부분_구간과_레코더_사용률을_표시한다() -> None:
    match = AudioMatch(
        Path("partial.mov"),
        10,
        MatchStatus.PARTIAL,
        confidence=0.85,
        reason="Only part of the camera audio matched the external recording",
        segments=(
            AudioMatchSegment("session-001", 1, 3, 3, confidence=0.9),
            AudioMatchSegment("session-002", 7, 4, 2, confidence=0.8),
        ),
    )
    report = MatchReport(
        sessions=(),
        matches=(match,),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    payload = report.to_dict()

    assert payload["summary"] == {
        "total": 1,
        "matched": 0,
        "partial": 1,
        "unmatched": 0,
        "ambiguous": 0,
        "error": 0,
    }
    assert payload["matches"][0]["coverage_ratio"] == 0.5
    assert payload["matches"][0]["segments"][1] == {
        "session_id": "session-002",
        "video_start_seconds": 7,
        "external_start_seconds": 4,
        "duration_seconds": 2,
        "tempo_ratio": 1.0,
        "correlation": 0.0,
        "peak_margin": 0.0,
        "confidence": 0.8,
    }
    assert report.to_text().splitlines() == [
        "분석 결과: 0/1개 전체 매칭, 1개 부분 매칭",
        ("- partial.mov | 매칭 여부: 부분 | 매칭률: 85.0% | 레코더 사용: 50.0% | 구간: 2개"),
    ]


def test_매칭_리포트는_영문_사유를_렌더링할_수_있다() -> None:
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


def test_매칭_리포트는_알려진_접두사를_번역하고_알_수_없는_사유는_보존한다() -> None:
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


def test_매칭_리포트는_간결한_한국어_사람용_요약을_렌더링한다() -> None:
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


def test_매칭_리포트는_나눗셈_오류_없이_빈_사람용_요약을_렌더링한다() -> None:
    report = MatchReport(
        sessions=(),
        matches=(),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    assert report.to_text() == "분석 결과: 0/0개 매칭 (0.0%)"


def test_매칭_리포트는_실패한_사람용_매칭을_항상_설명한다() -> None:
    report = MatchReport(
        sessions=(),
        matches=(AudioMatch(Path("failed.mov"), 8, MatchStatus.ERROR),),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    assert report.to_text().endswith("사유: 사유를 확인할 수 없습니다.")


def test_매칭_리포트는_영문_사람용_요약을_렌더링한다() -> None:
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
