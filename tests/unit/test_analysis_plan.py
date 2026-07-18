"""검증 가능한 분석 리포트 저장과 재사용 계약."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from recordersync.analysis_plan import load_analysis_report, write_analysis_report
from recordersync.media import VideoInfo
from recordersync.models import AudioChunk, AudioMatch, MatchStatus, RecordingSession
from recordersync.pipeline import AnalysisBundle
from recordersync.report import MatchReport


def _bundle(tmp_path: Path) -> AnalysisBundle:
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
        confidence=0.9,
    )
    return AnalysisBundle((session,), (video,), (match,))


def test_분석_리포트는_입력_메타데이터를_검증해_번들을_복원한다(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    report_path = tmp_path / "analysis.json"

    write_analysis_report(bundle.report(), bundle, report_path)
    restored = load_analysis_report(report_path, expected_video_dir=tmp_path / "video")

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["analysis_inputs"]["version"] == 1
    assert payload["analysis_inputs"]["videos"][0]["width"] == 1080
    assert restored.sessions == bundle.sessions
    assert restored.videos == bundle.videos
    assert restored.matches == bundle.matches


def test_분석_리포트는_입력_파일이_바뀌면_재사용을_거부한다(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    report_path = tmp_path / "analysis.json"
    write_analysis_report(bundle.report(), bundle, report_path)
    bundle.videos[0].path.write_bytes(b"changed-video")

    with pytest.raises(ValueError, match=r"Analysis input changed: .*clip.mov"):
        load_analysis_report(report_path, expected_video_dir=tmp_path / "video")


def test_분석_리포트는_입력_파일에_접근할_수_없으면_재사용을_거부한다(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    report_path = tmp_path / "analysis.json"
    write_analysis_report(bundle.report(), bundle, report_path)

    with (
        patch.object(Path, "stat", side_effect=PermissionError("permission denied")),
        pytest.raises(ValueError, match="Analysis input is missing or inaccessible"),
    ):
        load_analysis_report(report_path, expected_video_dir=tmp_path / "video")


def test_분석_리포트_파일에_접근할_수_없으면_명확한_오류를_반환한다(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "analysis.json"

    with (
        patch.object(Path, "read_text", side_effect=PermissionError("permission denied")),
        pytest.raises(ValueError, match="Analysis report not found or inaccessible"),
    ):
        load_analysis_report(report_path, expected_video_dir=tmp_path)


def test_분석_리포트는_표시_언어와_무관하게_원본_사유를_복원한다(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    reason = "Match confidence is below the configured threshold"
    bundle = replace(
        bundle,
        matches=(
            AudioMatch(
                bundle.videos[0].path,
                bundle.videos[0].duration_seconds,
                MatchStatus.UNMATCHED,
                reason=reason,
            ),
        ),
    )
    report_path = tmp_path / "analysis.json"

    write_analysis_report(bundle.report(), bundle, report_path)
    restored = load_analysis_report(report_path, expected_video_dir=tmp_path / "video")

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["matches"][0]["reason"] == "매칭 신뢰도가 설정된 기준보다 낮습니다."
    assert restored.matches[0].reason == reason


def test_분석_리포트는_지원하지_않는_계획_버전을_거부한다(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    report_path = tmp_path / "analysis.json"
    write_analysis_report(bundle.report(), bundle, report_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["analysis_inputs"]["version"] = 99
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported analysis input version: 99"):
        load_analysis_report(report_path, expected_video_dir=tmp_path / "video")


def test_분석_리포트는_계획의_알_수_없는_필드를_거부한다(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    report_path = tmp_path / "analysis.json"
    write_analysis_report(bundle.report(), bundle, report_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["analysis_inputs"]["unexpected"] = True
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid analysis report schema at analysis_inputs"):
        load_analysis_report(report_path, expected_video_dir=tmp_path / "video")


def test_분석_리포트는_매칭의_알_수_없는_필드를_거부한다(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    report_path = tmp_path / "analysis.json"
    write_analysis_report(bundle.report(), bundle, report_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["analysis_inputs"]["matches"][0]["unexpected"] = True
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"Invalid analysis report schema at analysis_inputs\.matches\.0",
    ):
        load_analysis_report(report_path, expected_video_dir=tmp_path / "video")


def test_분석_리포트는_빈_세션_ID를_거부한다(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    report_path = tmp_path / "analysis.json"
    write_analysis_report(bundle.report(), bundle, report_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["analysis_inputs"]["audio_sessions"][0]["id"] = ""
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"Invalid analysis report schema at analysis_inputs\.audio_sessions\.0\.id",
    ):
        load_analysis_report(report_path, expected_video_dir=tmp_path / "video")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("external_start_seconds", -0.1),
        ("tempo_ratio", 2.1),
        ("correlation", -1.1),
        ("peak_margin", 2.1),
        ("confidence", 1.1),
    ],
)
def test_분석_리포트는_범위를_벗어난_매칭_수치를_거부한다(
    tmp_path: Path,
    field: str,
    value: float,
) -> None:
    bundle = _bundle(tmp_path)
    report_path = tmp_path / "analysis.json"
    write_analysis_report(bundle.report(), bundle, report_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["analysis_inputs"]["matches"][0][field] = value
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=rf"Invalid analysis report schema at analysis_inputs\.matches\.0\.{field}",
    ):
        load_analysis_report(report_path, expected_video_dir=tmp_path / "video")


def test_분석_리포트는_유한하지_않은_JSON_수치를_거부한다(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    report_path = tmp_path / "analysis.json"
    write_analysis_report(bundle.report(), bundle, report_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["analysis_inputs"]["matches"][0]["confidence"] = float("nan")
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid analysis report JSON"):
        load_analysis_report(report_path, expected_video_dir=tmp_path / "video")


def test_분석_리포트는_다른_영상_디렉터리에서의_실행을_거부한다(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    report_path = tmp_path / "analysis.json"
    write_analysis_report(bundle.report(), bundle, report_path)

    with pytest.raises(ValueError, match="Analysis report does not belong to video directory"):
        load_analysis_report(report_path, expected_video_dir=tmp_path / "other")


def test_일반_리포트는_실행_계획으로_오인되지_않는다(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    MatchReport(sessions=(), matches=()).write(report_path)

    with pytest.raises(ValueError, match="Analysis report does not contain reusable inputs"):
        load_analysis_report(report_path, expected_video_dir=tmp_path)
