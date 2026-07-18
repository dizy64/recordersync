"""분석 리포트에 렌더 재사용용 입력 계획을 저장하고 검증한다."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from recordersync.media import VideoInfo
from recordersync.models import (
    AudioChunk,
    AudioMatch,
    AudioMatchSegment,
    MatchStatus,
    RecordingSession,
)
from recordersync.report import MatchReport, ReportLanguage

if TYPE_CHECKING:
    from recordersync.pipeline import AnalysisBundle


ANALYSIS_INPUT_VERSION = 1


def _fingerprint(path: Path) -> dict[str, object]:
    resolved = path.resolve()
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _chunk_payload(chunk: AudioChunk) -> dict[str, object]:
    return {
        **_fingerprint(chunk.path),
        "duration_seconds": chunk.duration_seconds,
        "sample_rate": chunk.sample_rate,
        "channels": chunk.channels,
        "codec": chunk.codec,
        "started_at": chunk.started_at.isoformat() if chunk.started_at is not None else None,
    }


def _video_payload(video: VideoInfo) -> dict[str, object]:
    return {
        **_fingerprint(video.path),
        "duration_seconds": video.duration_seconds,
        "width": video.width,
        "height": video.height,
        "has_audio": video.has_audio,
        "color_transfer": video.color_transfer,
    }


def _segment_payload(segment: AudioMatchSegment) -> dict[str, object]:
    return {
        "session_id": segment.session_id,
        "video_start_seconds": segment.video_start_seconds,
        "external_start_seconds": segment.external_start_seconds,
        "duration_seconds": segment.duration_seconds,
        "tempo_ratio": segment.tempo_ratio,
        "correlation": segment.correlation,
        "peak_margin": segment.peak_margin,
        "confidence": segment.confidence,
    }


def _match_input_payload(match: AudioMatch) -> dict[str, object]:
    return {
        "video": str(match.video_path.resolve()),
        "duration_seconds": match.duration_seconds,
        "status": match.status.value,
        "session_id": match.session_id,
        "external_start_seconds": match.external_start_seconds,
        "tempo_ratio": match.tempo_ratio,
        "correlation": match.correlation,
        "peak_margin": match.peak_margin,
        "confidence": match.confidence,
        "reason": match.reason,
        "segments": [_segment_payload(segment) for segment in match.segments],
    }


def _analysis_inputs(bundle: AnalysisBundle) -> dict[str, object]:
    return {
        "version": ANALYSIS_INPUT_VERSION,
        "audio_sessions": [
            {
                "id": session.id,
                "chunks": [_chunk_payload(chunk) for chunk in session.chunks],
            }
            for session in bundle.sessions
        ],
        "videos": [_video_payload(video) for video in bundle.videos],
        "matches": [_match_input_payload(match) for match in bundle.matches],
    }


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_analysis_report(
    report: MatchReport,
    bundle: AnalysisBundle,
    path: Path,
    *,
    language: ReportLanguage = ReportLanguage.KO,
) -> None:
    """분석 JSON과 입력 지문을 원자적으로 저장한다."""

    payload = report.to_dict(language=language)
    payload["analysis_inputs"] = _analysis_inputs(bundle)
    _write_json_atomic(path, payload)


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid analysis report field: {field}")
    return value


def _sequence(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"Invalid analysis report field: {field}")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid analysis report field: {field}")
    return value


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _string(value, field)


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid analysis report field: {field}")
    return float(value)


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Invalid analysis report field: {field}")
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"Invalid analysis report field: {field}")
    return value


def _validated_path(payload: dict[str, Any], field: str) -> Path:
    path = Path(_string(payload.get("path"), f"{field}.path")).resolve()
    expected_size = _integer(payload.get("size_bytes"), f"{field}.size_bytes")
    expected_mtime = _integer(payload.get("mtime_ns"), f"{field}.mtime_ns")
    try:
        stat = path.stat()
    except FileNotFoundError as exc:
        raise ValueError(f"Analysis input is missing: {path}") from exc
    if stat.st_size != expected_size or stat.st_mtime_ns != expected_mtime:
        raise ValueError(f"Analysis input changed: {path}")
    return path


def _load_session(value: object, index: int) -> RecordingSession:
    field = f"analysis_inputs.audio_sessions[{index}]"
    payload = _mapping(value, field)
    chunks: list[AudioChunk] = []
    for chunk_index, raw_chunk in enumerate(_sequence(payload.get("chunks"), f"{field}.chunks")):
        chunk_field = f"{field}.chunks[{chunk_index}]"
        chunk = _mapping(raw_chunk, chunk_field)
        raw_started_at = chunk.get("started_at")
        started_at = (
            datetime.fromisoformat(_string(raw_started_at, f"{chunk_field}.started_at"))
            if raw_started_at is not None
            else None
        )
        chunks.append(
            AudioChunk(
                path=_validated_path(chunk, chunk_field),
                duration_seconds=_number(
                    chunk.get("duration_seconds"), f"{chunk_field}.duration_seconds"
                ),
                sample_rate=_integer(chunk.get("sample_rate"), f"{chunk_field}.sample_rate"),
                channels=_integer(chunk.get("channels"), f"{chunk_field}.channels"),
                codec=_string(chunk.get("codec"), f"{chunk_field}.codec"),
                started_at=started_at,
            )
        )
    return RecordingSession(
        id=_string(payload.get("id"), f"{field}.id"),
        chunks=tuple(chunks),
    )


def _load_video(value: object, index: int) -> VideoInfo:
    field = f"analysis_inputs.videos[{index}]"
    payload = _mapping(value, field)
    return VideoInfo(
        path=_validated_path(payload, field),
        duration_seconds=_number(payload.get("duration_seconds"), f"{field}.duration_seconds"),
        width=_integer(payload.get("width"), f"{field}.width"),
        height=_integer(payload.get("height"), f"{field}.height"),
        has_audio=_boolean(payload.get("has_audio"), f"{field}.has_audio"),
        color_transfer=_optional_string(payload.get("color_transfer"), f"{field}.color_transfer"),
    )


def _load_segment(value: object, match_index: int, segment_index: int) -> AudioMatchSegment:
    field = f"matches[{match_index}].segments[{segment_index}]"
    payload = _mapping(value, field)
    return AudioMatchSegment(
        session_id=_string(payload.get("session_id"), f"{field}.session_id"),
        video_start_seconds=_number(
            payload.get("video_start_seconds"), f"{field}.video_start_seconds"
        ),
        external_start_seconds=_number(
            payload.get("external_start_seconds"), f"{field}.external_start_seconds"
        ),
        duration_seconds=_number(payload.get("duration_seconds"), f"{field}.duration_seconds"),
        tempo_ratio=_number(payload.get("tempo_ratio"), f"{field}.tempo_ratio"),
        correlation=_number(payload.get("correlation"), f"{field}.correlation"),
        peak_margin=_number(payload.get("peak_margin"), f"{field}.peak_margin"),
        confidence=_number(payload.get("confidence"), f"{field}.confidence"),
    )


def _load_match(value: object, index: int) -> AudioMatch:
    field = f"matches[{index}]"
    payload = _mapping(value, field)
    segments = tuple(
        _load_segment(segment, index, segment_index)
        for segment_index, segment in enumerate(
            _sequence(payload.get("segments"), f"{field}.segments")
        )
    )
    return AudioMatch(
        video_path=Path(_string(payload.get("video"), f"{field}.video")).resolve(),
        duration_seconds=_number(payload.get("duration_seconds"), f"{field}.duration_seconds"),
        status=MatchStatus(_string(payload.get("status"), f"{field}.status")),
        session_id=_optional_string(payload.get("session_id"), f"{field}.session_id"),
        external_start_seconds=(
            _number(payload.get("external_start_seconds"), f"{field}.external_start_seconds")
            if payload.get("external_start_seconds") is not None
            else None
        ),
        tempo_ratio=_number(payload.get("tempo_ratio"), f"{field}.tempo_ratio"),
        correlation=_number(payload.get("correlation"), f"{field}.correlation"),
        peak_margin=_number(payload.get("peak_margin"), f"{field}.peak_margin"),
        confidence=_number(payload.get("confidence"), f"{field}.confidence"),
        reason=_optional_string(payload.get("reason"), f"{field}.reason"),
        segments=segments,
    )


def load_analysis_report(path: Path, *, expected_video_dir: Path) -> AnalysisBundle:
    """입력 변경 여부를 검증하고 분석 번들을 복원한다."""

    try:
        raw_payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Analysis report not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid analysis report JSON: {path}") from exc
    payload = _mapping(raw_payload, "root")
    raw_inputs = payload.get("analysis_inputs")
    if raw_inputs is None:
        raise ValueError("Analysis report does not contain reusable inputs")
    inputs = _mapping(raw_inputs, "analysis_inputs")
    version = _integer(inputs.get("version"), "analysis_inputs.version")
    if version != ANALYSIS_INPUT_VERSION:
        raise ValueError(f"Unsupported analysis input version: {version}")

    sessions = tuple(
        _load_session(value, index)
        for index, value in enumerate(
            _sequence(inputs.get("audio_sessions"), "analysis_inputs.audio_sessions")
        )
    )
    videos = tuple(
        _load_video(value, index)
        for index, value in enumerate(_sequence(inputs.get("videos"), "analysis_inputs.videos"))
    )
    resolved_video_dir = expected_video_dir.resolve()
    if any(video.path.parent != resolved_video_dir for video in videos):
        raise ValueError(
            f"Analysis report does not belong to video directory: {resolved_video_dir}"
        )
    matches = tuple(
        _load_match(value, index)
        for index, value in enumerate(_sequence(inputs.get("matches"), "analysis_inputs.matches"))
    )
    video_paths = {video.path for video in videos}
    if any(
        match.status in {MatchStatus.MATCHED, MatchStatus.PARTIAL}
        and match.video_path not in video_paths
        for match in matches
    ):
        raise ValueError("Analysis report is missing render metadata for a matched video")

    from recordersync.pipeline import AnalysisBundle

    return AnalysisBundle(sessions=sessions, videos=videos, matches=matches)
