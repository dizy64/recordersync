"""부분 오디오 매칭 도메인 모델 계약."""

from pathlib import Path

import pytest

from recordersync.models import AudioMatch, AudioMatchSegment, MatchStatus


def test_부분_매칭은_정렬된_구간과_커버리지를_제공한다() -> None:
    segments = (
        AudioMatchSegment("session-001", 2.0, 10.0, 3.0, confidence=0.9),
        AudioMatchSegment("session-002", 7.0, 20.0, 2.0, confidence=0.8),
    )

    match = AudioMatch(
        Path("clip.mov"),
        10.0,
        MatchStatus.PARTIAL,
        segments=segments,
    )

    assert match.matched_duration_seconds == pytest.approx(5.0)
    assert match.coverage_ratio == pytest.approx(0.5)


def test_부분_매칭은_겹치거나_영상_밖으로_나간_구간을_거부한다() -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        AudioMatch(
            Path("clip.mov"),
            10.0,
            MatchStatus.PARTIAL,
            segments=(
                AudioMatchSegment("session-001", 2.0, 10.0, 4.0),
                AudioMatchSegment("session-001", 5.0, 13.0, 2.0),
            ),
        )

    with pytest.raises(ValueError, match="video duration"):
        AudioMatch(
            Path("clip.mov"),
            10.0,
            MatchStatus.PARTIAL,
            segments=(AudioMatchSegment("session-001", 8.0, 10.0, 3.0),),
        )
