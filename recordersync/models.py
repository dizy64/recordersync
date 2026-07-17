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
    PARTIAL = "partial"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AudioMatchSegment:
    """영상 시간축과 외부 녹음 시간축이 신뢰 가능하게 일치하는 연속 구간."""

    session_id: str
    video_start_seconds: float
    external_start_seconds: float
    duration_seconds: float
    tempo_ratio: float = 1.0
    correlation: float = 0.0
    peak_margin: float = 0.0
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if self.video_start_seconds < 0:
            raise ValueError("video_start_seconds must be >= 0")
        if self.external_start_seconds < 0:
            raise ValueError("external_start_seconds must be >= 0")
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0")
        if not 0.5 <= self.tempo_ratio <= 2.0:
            raise ValueError("tempo_ratio must be in [0.5, 2.0]")
        if not -1.0 <= self.correlation <= 1.0:
            raise ValueError("correlation must be in [-1, 1]")
        if not 0.0 <= self.peak_margin <= 2.0:
            raise ValueError("peak_margin must be in [0, 2]")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")

    @property
    def video_end_seconds(self) -> float:
        return self.video_start_seconds + self.duration_seconds


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
    segments: tuple[AudioMatchSegment, ...] = ()

    def __post_init__(self) -> None:
        if self.duration_seconds < 0:
            raise ValueError("duration_seconds must be >= 0")
        if self.status is MatchStatus.PARTIAL and not self.segments:
            raise ValueError("partial match requires at least one segment")
        if self.segments and self.status not in {MatchStatus.MATCHED, MatchStatus.PARTIAL}:
            raise ValueError("segments require matched or partial status")

        previous_end = 0.0
        for index, segment in enumerate(self.segments):
            if index and segment.video_start_seconds < previous_end - 1e-6:
                raise ValueError("match segments must not overlap")
            if segment.video_end_seconds > self.duration_seconds + 1e-6:
                raise ValueError("match segment exceeds video duration")
            previous_end = segment.video_end_seconds

    @property
    def matched_duration_seconds(self) -> float:
        if self.segments:
            return sum(segment.duration_seconds for segment in self.segments)
        if self.status is MatchStatus.MATCHED:
            return self.duration_seconds
        return 0.0

    @property
    def coverage_ratio(self) -> float:
        if self.duration_seconds <= 0:
            return 0.0
        return min(1.0, self.matched_duration_seconds / self.duration_seconds)
