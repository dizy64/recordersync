"""분할 녹음의 정렬과 세션 구성."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

from recordersync.models import AudioChunk, RecordingSession

_NATURAL_PARTS = re.compile(r"(\d+)")


def natural_sort_key(value: str) -> tuple[tuple[int, int | str], ...]:
    """숫자 부분을 정수로 비교하는 대소문자 비구분 정렬 키."""

    parts: list[tuple[int, int | str]] = []
    for part in _NATURAL_PARTS.split(value.casefold()):
        if part.isdigit():
            parts.append((0, int(part)))
        elif part:
            parts.append((1, part))
    return tuple(parts)


def _timestamp_key(value: datetime | None) -> tuple[int, float]:
    if value is None:
        return 1, 0.0
    return 0, value.timestamp()


def _starts_new_session(previous: AudioChunk, current: AudioChunk, gap_seconds: float) -> bool:
    if previous.stream_signature != current.stream_signature:
        return True
    if previous.started_at is None or current.started_at is None:
        return False
    expected_start = previous.started_at.timestamp() + previous.duration_seconds
    gap = current.started_at.timestamp() - expected_start
    # 복사 과정에서 모든 파일의 birthtime/mtime가 같아지는 경우에는 자연 파일명
    # 순서를 신뢰한다. 실제로 관측된 양수 공백만 새 세션 경계로 취급한다.
    return gap > gap_seconds


def group_recording_sessions(
    chunks: Iterable[AudioChunk],
    *,
    gap_seconds: float = 10.0,
) -> list[RecordingSession]:
    """녹음 조각을 시각·자연 파일명 순으로 정렬하고 연속 세션으로 묶는다."""

    if gap_seconds < 0:
        raise ValueError("gap_seconds must be >= 0")

    ordered = sorted(
        chunks,
        key=lambda chunk: (_timestamp_key(chunk.started_at), natural_sort_key(chunk.path.name)),
    )
    if not ordered:
        return []

    grouped: list[list[AudioChunk]] = [[ordered[0]]]
    for chunk in ordered[1:]:
        if _starts_new_session(grouped[-1][-1], chunk, gap_seconds):
            grouped.append([chunk])
        else:
            grouped[-1].append(chunk)

    return [
        RecordingSession(id=f"session-{index:03d}", chunks=tuple(group))
        for index, group in enumerate(grouped, start=1)
    ]
