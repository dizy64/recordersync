"""공개 CLI 인자 계약."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from recordersync.cli import build_parser, main
from recordersync.models import AudioMatch, MatchStatus
from recordersync.pipeline import AnalysisBundle


def test_main_without_arguments_prints_help_and_returns_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0

    stdout = capsys.readouterr().out
    assert "usage: recordersync" in stdout
    assert "recordersync analyze VIDEO_DIR" in stdout
    assert "recordersync process VIDEO_DIR" in stdout


def test_process_help_documents_both_audio_volume_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["process", "--help"])

    assert exit_info.value.code == 0
    stdout = capsys.readouterr().out
    assert "--camera-audio-volume" in stdout
    assert "원본 영상 오디오 볼륨" in stdout
    assert "--external-audio-volume" in stdout
    assert "외부 보이스레코더 오디오 볼륨" in stdout


def test_process_cli_defaults_to_safe_replace_policy() -> None:
    args = build_parser().parse_args(["process", "/video", "--audio-dir", "/audio"])

    assert args.command == "process"
    assert args.video_dir == Path("/video")
    assert args.audio_dir == Path("/audio")
    assert args.output_dir is None
    assert args.mode == "replace"
    assert args.camera_audio_volume == pytest.approx(0.1)
    assert args.external_audio_volume == pytest.approx(1.0)
    assert args.min_confidence == pytest.approx(0.75)
    assert args.min_peak_margin == pytest.approx(0.05)
    assert args.session_gap_seconds == pytest.approx(10.0)
    assert args.report_language == "ko"
    assert args.output_prefix == ""
    assert args.output_suffix == ""
    assert not args.overwrite


def test_process_cli_allows_audio_dir_to_be_omitted() -> None:
    args = build_parser().parse_args(["process", "/media"])

    assert args.video_dir == Path("/media")
    assert args.audio_dir is None


def test_process_cli_accepts_output_name_affixes() -> None:
    args = build_parser().parse_args(
        ["process", "/media", "--output-prefix", "final_", "--output-suffix", "_synced"]
    )

    assert args.output_prefix == "final_"
    assert args.output_suffix == "_synced"


def test_analyze_cli_accepts_json_report_path() -> None:
    args = build_parser().parse_args(
        ["analyze", "/video", "--audio-dir", "/audio", "--report", "/tmp/report.json"]
    )

    assert args.command == "analyze"
    assert args.report == Path("/tmp/report.json")


def test_analyze_cli_rejects_unsupported_report_language() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["analyze", "/video", "--report-language", "ja"])


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


def test_process_cli_accepts_external_audio_volume() -> None:
    args = build_parser().parse_args(
        ["process", "/video", "--mode", "mix", "--external-audio-volume", "0.8"]
    )

    assert args.external_audio_volume == pytest.approx(0.8)


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


def test_main_prints_selected_files_and_progress_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = AnalysisBundle(
        sessions=(),
        videos=(),
        matches=(AudioMatch(Path("clip.mov"), 5, MatchStatus.MATCHED),),
    )
    pipeline = MagicMock()
    pipeline.analyze.return_value = bundle

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        assert main(["analyze", "/media"]) == 0

    selection_callback = pipeline.analyze.call_args.kwargs["selection_callback"]
    progress_callback = pipeline.analyze.call_args.kwargs["progress_callback"]
    selection_callback("audio", (Path("REC_001.wav"), Path("REC_002.wav")))
    selection_callback("video", (Path("clip.mov"),))
    progress_callback("audio", 1, 2, "session-001")
    progress_callback("match", 2, 2, "clip.mov")

    stderr = capsys.readouterr().err
    assert "선택된 오디오 파일 (2개)" in stderr
    assert "REC_001.wav" in stderr
    assert "선택된 영상 파일 (1개)" in stderr
    assert "[오디오 분석] 1/2 (50%) session-001" in stderr
    assert "[영상 매칭] 2/2 (100%) clip.mov" in stderr


def test_main_can_print_english_report_reasons(capsys: pytest.CaptureFixture[str]) -> None:
    bundle = AnalysisBundle(
        sessions=(),
        videos=(),
        matches=(
            AudioMatch(
                Path("clip.mov"),
                5,
                MatchStatus.UNMATCHED,
                reason="Match confidence is below the configured threshold",
            ),
        ),
    )
    pipeline = MagicMock()
    pipeline.analyze.return_value = bundle

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        exit_code = main(["analyze", "/media", "--report-language", "en"])

    assert exit_code == 2
    assert "Match confidence is below the configured threshold" in capsys.readouterr().out


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


def test_main_dry_run_applies_output_name_affixes(capsys: pytest.CaptureFixture[str]) -> None:
    bundle = AnalysisBundle(
        sessions=(),
        videos=(),
        matches=(AudioMatch(Path("clip.mov"), 5, MatchStatus.MATCHED),),
    )
    pipeline = MagicMock()
    pipeline.analyze.return_value = bundle

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        exit_code = main(
            [
                "process",
                "/video",
                "--dry-run",
                "--output-prefix",
                "final_",
                "--output-suffix",
                "_synced",
            ]
        )

    assert exit_code == 0
    assert '"output": "/video/replace/final_clip_synced.mp4"' in capsys.readouterr().out
    pipeline.process.assert_not_called()


def test_main_reports_fatal_validation_error(capsys: pytest.CaptureFixture[str]) -> None:
    pipeline = MagicMock()
    pipeline.analyze.side_effect = ValueError("bad input")

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        exit_code = main(["analyze", "/video", "--audio-dir", "/audio"])

    assert exit_code == 1
    assert "bad input" in capsys.readouterr().err
