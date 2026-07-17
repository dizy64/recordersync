"""RecorderSync의 순수 도메인 모델."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """보이스레코더가 분할해 저장한 단일 오디오 파일."""

    path: Path
    duration_seconds: float
    sample_rate: int
    channels: int
    codec: str
    started_at: datetime | None

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0")
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be > 0")
        if self.channels <= 0:
            raise ValueError("channels must be > 0")

    @property
    def stream_signature(self) -> tuple[int, int, str]:
        """세션 내 gapless 결합 호환성을 판단하는 스트림 규격."""

        return self.sample_rate, self.channels, self.codec


@dataclass(frozen=True, slots=True)
class RecordingSession:
    """시간상 연속되고 스트림 규격이 같은 녹음 조각 묶음."""

    id: str
    chunks: tuple[AudioChunk, ...]

    def __post_init__(self) -> None:
        if not self.chunks:
            raise ValueError("RecordingSession requires at least one chunk")

    @property
    def duration_seconds(self) -> float:
        return sum(chunk.duration_seconds for chunk in self.chunks)


class MatchStatus(StrEnum):
    """영상별 매칭 결과 상태."""

    MATCHED = "matched"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AudioMatch:
    """한 영상과 외부 녹음 구간의 매칭 결과."""

    video_path: Path
    duration_seconds: float
    status: MatchStatus
    session_id: str | None = None
    external_start_seconds: float | None = None
    tempo_ratio: float = 1.0
    correlation: float = 0.0
    peak_margin: float = 0.0
    confidence: float = 0.0
    reason: str | None = None
    output_path: Path | None = None
