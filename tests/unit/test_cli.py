"""공개 CLI 인자 계약."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from recordersync.cli import build_parser, main
from recordersync.models import AudioMatch, MatchStatus
from recordersync.pipeline import AnalysisBundle


def test_process_cli_defaults_to_safe_replace_policy() -> None:
    args = build_parser().parse_args(["process", "/video", "--audio-dir", "/audio"])

    assert args.command == "process"
    assert args.video_dir == Path("/video")
    assert args.audio_dir == Path("/audio")
    assert args.output_dir is None
    assert args.mode == "replace"
    assert args.camera_audio_volume == pytest.approx(0.1)
    assert args.min_confidence == pytest.approx(0.75)
    assert args.min_peak_margin == pytest.approx(0.05)
    assert args.session_gap_seconds == pytest.approx(10.0)
    assert not args.overwrite


def test_process_cli_allows_audio_dir_to_be_omitted() -> None:
    args = build_parser().parse_args(["process", "/media"])

    assert args.video_dir == Path("/media")
    assert args.audio_dir is None


def test_analyze_cli_accepts_json_report_path() -> None:
    args = build_parser().parse_args(
        ["analyze", "/video", "--audio-dir", "/audio", "--report", "/tmp/report.json"]
    )

    assert args.command == "analyze"
    assert args.report == Path("/tmp/report.json")


def test_process_cli_rejects_camera_volume_outside_unit_interval() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "process",
                "/video",
                "--audio-dir",
                "/audio",
                "--camera-audio-volume",
                "1.1",
            ]
        )


def test_main_analyze_prints_report_and_returns_success(capsys: pytest.CaptureFixture[str]) -> None:
    bundle = AnalysisBundle(
        sessions=(),
        videos=(),
        matches=(AudioMatch(Path("clip.mov"), 5, MatchStatus.MATCHED),),
    )
    pipeline = MagicMock()
    pipeline.analyze.return_value = bundle

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        exit_code = main(["analyze", "/video", "--audio-dir", "/audio"])

    assert exit_code == 0
    assert '"matched": 1' in capsys.readouterr().out


def test_main_uses_video_dir_for_audio_when_audio_dir_is_omitted() -> None:
    bundle = AnalysisBundle(
        sessions=(),
        videos=(),
        matches=(AudioMatch(Path("clip.mov"), 5, MatchStatus.MATCHED),),
    )
    pipeline = MagicMock()
    pipeline.analyze.return_value = bundle

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        exit_code = main(["analyze", "/media"])

    assert exit_code == 0
    assert pipeline.analyze.call_args.args[:2] == (Path("/media"), Path("/media"))


def test_main_dry_run_returns_partial_exit_without_rendering() -> None:
    bundle = AnalysisBundle(
        sessions=(),
        videos=(),
        matches=(AudioMatch(Path("clip.mov"), 5, MatchStatus.AMBIGUOUS),),
    )
    pipeline = MagicMock()
    pipeline.analyze.return_value = bundle

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        exit_code = main(["process", "/video", "--audio-dir", "/audio", "--dry-run"])

    assert exit_code == 2
    pipeline.process.assert_not_called()


def test_main_reports_fatal_validation_error(capsys: pytest.CaptureFixture[str]) -> None:
    pipeline = MagicMock()
    pipeline.analyze.side_effect = ValueError("bad input")

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        exit_code = main(["analyze", "/video", "--audio-dir", "/audio"])

    assert exit_code == 1
    assert "bad input" in capsys.readouterr().err
