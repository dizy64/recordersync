"""녹음 조각의 정렬과 세션 분리 규칙."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from recordersync.models import AudioChunk
from recordersync.sessions import group_recording_sessions, natural_sort_key


def _chunk(
    name: str,
    *,
    started_at: datetime | None,
    duration: float = 60.0,
    sample_rate: int = 48_000,
    channels: int = 2,
    codec: str = "pcm_f32le",
) -> AudioChunk:
    return AudioChunk(
        path=Path(name),
        duration_seconds=duration,
        sample_rate=sample_rate,
        channels=channels,
        codec=codec,
        started_at=started_at,
    )


def test_natural_sort_key_orders_numeric_suffixes() -> None:
    names = ["REC_10.WAV", "REC_2.WAV", "REC_1.WAV"]

    assert sorted(names, key=natural_sort_key) == ["REC_1.WAV", "REC_2.WAV", "REC_10.WAV"]


def test_group_recording_sessions_joins_contiguous_chunks() -> None:
    start = datetime(2026, 7, 17, tzinfo=UTC)
    chunks = [
        _chunk("REC_002.WAV", started_at=start + timedelta(seconds=60.5)),
        _chunk("REC_001.WAV", started_at=start),
    ]

    sessions = group_recording_sessions(chunks, gap_seconds=10.0)

    assert len(sessions) == 1
    assert [chunk.path.name for chunk in sessions[0].chunks] == ["REC_001.WAV", "REC_002.WAV"]
    assert sessions[0].duration_seconds == pytest.approx(120.0)


def test_group_recording_sessions_splits_on_large_time_gap() -> None:
    start = datetime(2026, 7, 17, tzinfo=UTC)
    chunks = [
        _chunk("REC_001.WAV", started_at=start),
        _chunk("REC_002.WAV", started_at=start + timedelta(minutes=5)),
    ]

    sessions = group_recording_sessions(chunks, gap_seconds=10.0)

    assert [session.id for session in sessions] == ["session-001", "session-002"]


def test_group_recording_sessions_splits_on_stream_mismatch() -> None:
    start = datetime(2026, 7, 17, tzinfo=UTC)
    chunks = [
        _chunk("REC_001.WAV", started_at=start),
        _chunk("REC_002.WAV", started_at=start + timedelta(seconds=60), sample_rate=96_000),
    ]

    sessions = group_recording_sessions(chunks)

    assert len(sessions) == 2


def test_group_recording_sessions_uses_filename_when_timestamps_missing() -> None:
    chunks = [
        _chunk("take_10.wav", started_at=None),
        _chunk("take_2.wav", started_at=None),
    ]

    sessions = group_recording_sessions(chunks)

    assert [chunk.path.name for chunk in sessions[0].chunks] == ["take_2.wav", "take_10.wav"]


def test_group_recording_sessions_rejects_invalid_gap() -> None:
    with pytest.raises(ValueError, match="gap_seconds"):
        group_recording_sessions([], gap_seconds=-1)
