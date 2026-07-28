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
from recordersync.audio_levels import (
    AudioLevelMetrics,
    AudioLevelPolicy,
    AudioLevelReport,
    OutputChannelLayout,
    decide_static_gain,
)
from recordersync.media import VideoInfo
from recordersync.mix_analysis import MixRecommendation, MixSourceMetrics
from recordersync.models import AudioChunk, AudioMatch, MatchStatus, RecordingSession
from recordersync.pipeline import AnalysisBundle
from recordersync.render import MixPolicy
from recordersync.report import MatchReport

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


def test_리포트_스키마는_음량_안전_처리_결과를_검증한다(
    schema: dict[str, object],
    bundle: AnalysisBundle,
) -> None:
    policy = AudioLevelPolicy(-16.0, -1.0, OutputChannelLayout.STEREO, 0.5)
    input_metrics = AudioLevelMetrics(2, 48_000, -20.0, 7.0, -8.1, -8.0, 10.0, "pcm_f32le")
    output_metrics = AudioLevelMetrics(2, 48_000, -16.1, 7.0, -1.2, -1.1, 10.0, "aac")
    report = MatchReport(
        sessions=bundle.sessions,
        matches=bundle.matches,
        audio_levels=(
            AudioLevelReport(
                policy=policy,
                input_metrics=input_metrics,
                decision=decide_static_gain(input_metrics, policy),
                output_metrics=output_metrics,
            ),
        ),
    )

    Draft202012Validator(schema).validate(report.to_dict())


def test_리포트_스키마는_자동_mix_추천과_분석_실패를_검증한다(
    schema: dict[str, object],
    bundle: AnalysisBundle,
) -> None:
    level_policy = AudioLevelPolicy(-16, -1, OutputChannelLayout.STEREO, 0.5)
    levels = AudioLevelMetrics(2, 48_000, -12, 7, -1.1, -1, 10, "float_analysis")
    source = MixSourceMetrics(levels, 0.2, 1_300, 0.8, -18)
    success = MixRecommendation(
        camera=source,
        external=source,
        policy=MixPolicy(1.0, 10 ** (-12 / 20), 80, level_policy),
        external_gain_db=-12,
        reasons=("측정 기반 보수 감쇠",),
        applied=True,
    )
    failure = MixRecommendation.failed("camera analysis error: invalid frame")
    application_failure = success.with_application_failure("final AAC validation failed")
    validator = Draft202012Validator(schema)

    for recommendation in (success, failure, application_failure):
        report = MatchReport(
            sessions=bundle.sessions,
            matches=bundle.matches,
            mix_recommendations=(recommendation,),
        )
        validator.validate(report.to_dict())


def test_리포트_스키마는_gain_충돌과_입력_분석_실패를_검증한다(
    schema: dict[str, object],
    bundle: AnalysisBundle,
) -> None:
    policy = AudioLevelPolicy(-7.3, -1.0, OutputChannelLayout.STEREO, 0.5)
    input_metrics = AudioLevelMetrics(2, 48_000, -11.1, 10.6, 7.6, 7.7, 10.0, "aac")
    conflict = AudioLevelReport(
        policy=policy,
        input_metrics=input_metrics,
        decision=decide_static_gain(input_metrics, policy),
        validation_failures=("loudness target conflicts with true-peak ceiling",),
    )
    analysis_failure = AudioLevelReport(
        policy=policy,
        validation_failures=("input analysis error: corrupt frame",),
    )
    validator = Draft202012Validator(schema)

    for audio_levels in (conflict, analysis_failure):
        report = MatchReport(
            sessions=bundle.sessions,
            matches=bundle.matches,
            audio_levels=(audio_levels,),
        )
        validator.validate(report.to_dict())


@pytest.mark.parametrize(
    ("output", "passed", "failures"),
    [
        (None, True, []),
        ("keep", False, []),
        ("keep", True, ["unexpected failure"]),
    ],
)
def test_리포트_스키마는_음량_검증_상태의_모순을_거부한다(
    schema: dict[str, object],
    bundle: AnalysisBundle,
    output: object,
    passed: bool,
    failures: list[str],
) -> None:
    policy = AudioLevelPolicy(-16.0, -1.0, OutputChannelLayout.STEREO, 0.5)
    input_metrics = AudioLevelMetrics(2, 48_000, -20.0, 7.0, -8.1, -8.0, 10.0, "pcm_f32le")
    output_metrics = AudioLevelMetrics(2, 48_000, -16.1, 7.0, -1.2, -1.1, 10.0, "aac")
    report = MatchReport(
        sessions=bundle.sessions,
        matches=bundle.matches,
        audio_levels=(
            AudioLevelReport(
                policy=policy,
                input_metrics=input_metrics,
                decision=decide_static_gain(input_metrics, policy),
                output_metrics=output_metrics,
            ),
        ),
    )
    payload = report.to_dict()
    levels = payload["matches"][0]["audio_levels"]
    levels["validation"] = {"passed": passed, "failures": failures}
    if output is None:
        levels["output"] = None

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(payload)


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


def test_리포트_스키마는_재사용_입력의_알_수_없는_필드를_거부한다(
    tmp_path: Path,
    schema: dict[str, object],
    bundle: AnalysisBundle,
) -> None:
    report_path = tmp_path / "analysis.json"
    write_analysis_report(bundle.report(), bundle, report_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    chunk = payload["analysis_inputs"]["audio_sessions"][0]["chunks"][0]
    chunk["unexpected"] = True

    with pytest.raises(ValidationError, match="not allowed"):
        Draft202012Validator(schema).validate(payload)
