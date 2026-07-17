"""미디어 파일 탐색과 FFmpeg/ffprobe 어댑터."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from recordersync.matching import FeatureTimeline, FloatArray, build_multiband_features
from recordersync.models import AudioChunk, RecordingSession
from recordersync.sessions import natural_sort_key

SUPPORTED_AUDIO_EXTENSIONS = frozenset(
    {".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".wav", ".wave"}
)
SUPPORTED_VIDEO_EXTENSIONS = frozenset(
    {".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mts", ".webm"}
)

FFPROBE_TIMEOUT_SECONDS = 30.0
FEATURE_EXTRACTION_TIMEOUT_SECONDS = 3_600.0
FEATURE_SAMPLE_RATE = 8_000
FEATURE_HOP_SECONDS = 0.05


class MediaError(RuntimeError):
    """미디어 탐색·디코딩·메타데이터 조회 실패."""


@dataclass(frozen=True, slots=True)
class VideoInfo:
    """렌더와 매칭에 필요한 영상 메타데이터."""

    path: Path
    duration_seconds: float
    width: int
    height: int
    has_audio: bool
    color_transfer: str | None = None

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("video dimensions must be > 0")

    @property
    def is_portrait(self) -> bool:
        return self.height > self.width


def _discover_files(directory: Path, extensions: frozenset[str]) -> list[Path]:
    if not directory.is_dir():
        raise FileNotFoundError(f"Directory not found: {directory}")
    return sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.casefold() in extensions
        ),
        key=lambda path: natural_sort_key(path.name),
    )


def discover_audio_files(directory: Path) -> list[Path]:
    """입력 폴더 바로 아래에서 지원 오디오를 자연 파일명 순으로 찾는다."""

    return _discover_files(directory, SUPPORTED_AUDIO_EXTENSIONS)


def discover_video_files(
    directory: Path,
    *,
    excluded_dirs: set[Path] | None = None,
) -> list[Path]:
    """입력 폴더 바로 아래에서 지원 영상을 찾는다."""

    del excluded_dirs  # 비재귀 스캔이므로 하위 출력 디렉터리는 자연스럽게 제외된다.
    return _discover_files(directory, SUPPORTED_VIDEO_EXTENSIONS)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _first_stream(payload: dict[str, Any], stream_type: str) -> dict[str, Any] | None:
    streams = payload.get("streams", [])
    if not isinstance(streams, list):
        return None
    return next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == stream_type
        ),
        None,
    )


def _positive_float(value: object, field: str) -> float:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError) as exc:
        raise MediaError(f"Invalid {field}: {value!r}") from exc
    if parsed <= 0:
        raise MediaError(f"Invalid {field}: {parsed}")
    return parsed


class FFmpegTools:
    """외부 프로세스를 한 경계에 모은 FFmpeg/ffprobe 서비스."""

    def __init__(self, ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe") -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path

    def _probe(self, path: Path) -> dict[str, Any]:
        command = [
            self.ffprobe_path,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=FFPROBE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaError(f"Timed out probing media: {path}") from exc
        if result.returncode != 0:
            raise MediaError(f"Failed to probe {path}: {result.stderr.strip()}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MediaError(f"Invalid ffprobe JSON for {path}") from exc
        if not isinstance(payload, dict):
            raise MediaError(f"Invalid ffprobe payload for {path}")
        return payload

    def probe_audio(self, path: Path) -> AudioChunk:
        payload = self._probe(path)
        stream = _first_stream(payload, "audio")
        if stream is None:
            raise MediaError(f"No audio stream found: {path}")
        raw_format = payload.get("format", {})
        file_format = raw_format if isinstance(raw_format, dict) else {}
        duration = _positive_float(file_format.get("duration", stream.get("duration")), "duration")

        raw_tags = file_format.get("tags", {})
        format_tags = raw_tags if isinstance(raw_tags, dict) else {}
        raw_stream_tags = stream.get("tags", {})
        stream_tags = raw_stream_tags if isinstance(raw_stream_tags, dict) else {}
        started_at = _parse_datetime(
            format_tags.get("creation_time") or stream_tags.get("creation_time")
        )
        if started_at is None:
            stat = path.stat()
            started_at = datetime.fromtimestamp(
                float(getattr(stat, "st_birthtime", stat.st_mtime)),
                tz=UTC,
            )

        return AudioChunk(
            path=path,
            duration_seconds=duration,
            sample_rate=int(stream.get("sample_rate", 0)),
            channels=int(stream.get("channels", 0)),
            codec=str(stream.get("codec_name", "unknown")),
            started_at=started_at,
        )

    def probe_video(self, path: Path) -> VideoInfo:
        payload = self._probe(path)
        video_stream = _first_stream(payload, "video")
        if video_stream is None:
            raise MediaError(f"No video stream found: {path}")
        raw_format = payload.get("format", {})
        file_format = raw_format if isinstance(raw_format, dict) else {}
        duration = _positive_float(
            file_format.get("duration", video_stream.get("duration")),
            "duration",
        )
        return VideoInfo(
            path=path,
            duration_seconds=duration,
            width=int(video_stream.get("width", 0)),
            height=int(video_stream.get("height", 0)),
            has_audio=_first_stream(payload, "audio") is not None,
            color_transfer=(
                str(video_stream["color_transfer"]) if video_stream.get("color_transfer") else None
            ),
        )

    def extract_features(self, path: Path) -> FloatArray:
        command = [
            self.ffmpeg_path,
            "-v",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(FEATURE_SAMPLE_RATE),
            "-f",
            "f32le",
            "pipe:1",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=FEATURE_EXTRACTION_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaError(f"Timed out extracting audio features: {path}") from exc
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise MediaError(f"Failed to decode audio from {path}: {stderr}")
        samples = np.frombuffer(result.stdout, dtype="<f4")
        if samples.size == 0:
            raise MediaError(f"Decoded audio is empty: {path}")
        return build_multiband_features(samples, sample_rate=FEATURE_SAMPLE_RATE)

    def build_session_timeline(self, session: RecordingSession) -> FeatureTimeline:
        features: list[FloatArray] = []
        for chunk in session.chunks:
            extracted = self.extract_features(chunk.path)
            expected_frames = max(1, round(chunk.duration_seconds / FEATURE_HOP_SECONDS))
            if extracted.shape[1] < expected_frames:
                padding = np.repeat(
                    extracted[:, -1:],
                    expected_frames - extracted.shape[1],
                    axis=1,
                )
                extracted = np.concatenate((extracted, padding), axis=1)
            elif extracted.shape[1] > expected_frames:
                extracted = extracted[:, :expected_frames]
            features.append(extracted)
        return FeatureTimeline(
            session_id=session.id,
            features=np.concatenate(features, axis=1).astype(np.float32, copy=False),
            hop_seconds=FEATURE_HOP_SECONDS,
        )
