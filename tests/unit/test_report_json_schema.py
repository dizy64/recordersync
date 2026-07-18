"""REPORT_VERSION 2 JSON Schema 계약."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from recordersync.analysis_plan import write_analysis_report
from recordersync.media import VideoInfo
from recordersync.models import AudioChunk, AudioMatch, MatchStatus, RecordingSession
from recordersync.pipeline import AnalysisBundle

SCHEMA_PATH = Path(__file__).parents[2] / "schemas" / "recordersync-report-v2.schema.json"
REPORT_DOCUMENT_PATH = Path(__file__).parents[2] / "docs" / "reference" / "report-schema.md"


@pytest.fixture
def schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def bundle(tmp_path: Path) -> AnalysisBundle:
    audio_path = tmp_path / "audio" / "REC_001.wav"
    video_path = tmp_path / "video" / "clip.mov"
    audio_path.parent.mkdir()
    video_path.parent.mkdir()
    audio_path.write_bytes(b"audio")
    video_path.write_bytes(b"video")
    session = RecordingSession(
        "session-001",
        (
            AudioChunk(
                audio_path,
                60,
                48_000,
                2,
                "pcm_s24le",
                datetime(2026, 7, 18, tzinfo=UTC),
            ),
        ),
    )
    video = VideoInfo(video_path, 10, 1080, 1920, True, "bt709")
    match = AudioMatch(
        video_path,
        10,
        MatchStatus.MATCHED,
        session_id=session.id,
        external_start_seconds=3,
        correlation=0.9,
        peak_margin=0.2,
        confidence=0.95,
    )
    return AnalysisBundle((session,), (video,), (match,))


def test_리포트_스키마는_Draft_2020_12_메타_스키마를_통과한다(
    schema: dict[str, object],
) -> None:
    Draft202012Validator.check_schema(schema)


def test_리포트_스키마는_일반과_재사용_분석_리포트를_검증한다(
    tmp_path: Path,
    schema: dict[str, object],
    bundle: AnalysisBundle,
) -> None:
    validator = Draft202012Validator(schema)
    validator.validate(bundle.report().to_dict())
    report_path = tmp_path / "analysis.json"
    write_analysis_report(bundle.report(), bundle, report_path)

    validator.validate(json.loads(report_path.read_text(encoding="utf-8")))


def test_리포트_스키마는_정의되지_않은_매칭_상태를_거부한다(
    schema: dict[str, object],
    bundle: AnalysisBundle,
) -> None:
    payload = deepcopy(bundle.report().to_dict())
    matches = payload["matches"]
    assert isinstance(matches, list)
    first_match = matches[0]
    assert isinstance(first_match, dict)
    first_match["status"] = "unknown"

    with pytest.raises(ValidationError, match="is not one of"):
        Draft202012Validator(schema).validate(payload)


def test_리포트_스키마는_문서의_공개_합성_예시를_검증한다(
    schema: dict[str, object],
) -> None:
    document = REPORT_DOCUMENT_PATH.read_text(encoding="utf-8")
    example = document.split("```json\n", maxsplit=1)[1].split("\n```", maxsplit=1)[0]

    Draft202012Validator(schema).validate(json.loads(example))
