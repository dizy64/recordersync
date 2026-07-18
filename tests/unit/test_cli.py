"""공개 CLI 인자 계약."""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from recordersync.cli import build_parser, main
from recordersync.models import AudioMatch, AudioMatchSegment, MatchStatus
from recordersync.pipeline import AnalysisBundle
from recordersync.report import MatchReport


def test_모든_공개_CLI_인자는_도움말을_제공한다() -> None:
    def is_missing_help(value: object) -> bool:
        return (
            value is None
            or value == argparse.SUPPRESS
            or (isinstance(value, str) and not value.strip())
        )

    pending = [("recordersync", build_parser())]
    missing: list[str] = []

    while pending:
        command, parser = pending.pop()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                missing.extend(
                    f"{command} {choice.dest}"
                    for choice in action._choices_actions
                    if is_missing_help(choice.help)
                )
                pending.extend(
                    (f"{command} {name}", child) for name, child in action.choices.items()
                )
                continue
            if is_missing_help(action.help):
                argument = action.option_strings[0] if action.option_strings else action.dest
                missing.append(f"{command} {argument}")

    assert sorted(missing) == []


def test_인자_없는_메인은_도움말을_출력하고_성공을_반환한다(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0

    stdout = capsys.readouterr().out
    assert "usage: recordersync" in stdout
    assert "recordersync analyze VIDEO_DIR" in stdout
    assert "recordersync process VIDEO_DIR" in stdout
    assert "--full-only" in stdout
    assert "--mode fallback" in stdout


def test_처리_도움말은_주요_처리_옵션을_안내한다(
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
    assert "--mode {replace,mix,fallback}" in stdout
    assert "--min-partial-seconds" in stdout
    assert "--recommended-only" in stdout
    assert "--analysis-report" in stdout
    assert "--overwrite" in stdout
    assert "기존 출력 파일 덮어쓰기" in stdout


def test_분석_도움말은_처리_모드_추천을_안내한다(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["analyze", "--help"])

    assert exit_info.value.code == 0
    stdout = capsys.readouterr().out
    assert "fallback 추천을 분석합니다(기본 동작)" in stdout
    assert "--full-only" in stdout


def test_처리_CLI는_안전한_교체_정책을_기본값으로_사용한다() -> None:
    args = build_parser().parse_args(["process", "/video", "--audio-dir", "/audio"])

    assert args.command == "process"
    assert args.video_dir == Path("/video")
    assert args.audio_dir == Path("/audio")
    assert args.output_dir is None
    assert args.mode == "replace"
    assert args.camera_audio_volume is None
    assert args.external_audio_volume == pytest.approx(1.0)
    assert args.min_confidence == pytest.approx(0.75)
    assert args.min_peak_margin == pytest.approx(0.05)
    assert args.session_gap_seconds == pytest.approx(10.0)
    assert args.report_language == "ko"
    assert args.output_prefix == ""
    assert args.output_suffix == ""
    assert args.analysis_report is None
    assert not args.recommended_only
    assert not args.json
    assert not args.overwrite


def test_처리_CLI는_overwrite를_명시하면_덮어쓰기를_활성화한다() -> None:
    args = build_parser().parse_args(["process", "/video", "--overwrite"])

    assert args.overwrite


def test_부분_분석과_폴백_처리는_구간_매칭을_활성화한다() -> None:
    default_analyze_args = build_parser().parse_args(["analyze", "/video"])
    analyze_args = build_parser().parse_args(["analyze", "/video", "--partial"])
    full_only_args = build_parser().parse_args(["analyze", "/video", "--full-only"])
    process_args = build_parser().parse_args(["process", "/video", "--mode", "fallback"])

    assert default_analyze_args.partial
    assert analyze_args.partial
    assert not full_only_args.partial
    assert process_args.mode == "fallback"
    assert process_args.camera_audio_volume is None


def test_추천_전용_처리는_폴백_모드에서만_허용한다(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["process", "/video", "--recommended-only"])

    assert exit_info.value.code == 2
    assert "--recommended-only requires --mode fallback" in capsys.readouterr().err


def test_처리_CLI는_오디오_디렉터리_생략을_허용한다() -> None:
    args = build_parser().parse_args(["process", "/media"])

    assert args.video_dir == Path("/media")
    assert args.audio_dir is None


def test_처리_CLI는_기존_분석_리포트를_입력으로_허용한다() -> None:
    args = build_parser().parse_args(
        ["process", "/media", "--analysis-report", "/tmp/analysis.json"]
    )

    assert args.analysis_report == Path("/tmp/analysis.json")


def test_처리_CLI는_출력_이름의_접두사와_접미사를_허용한다() -> None:
    args = build_parser().parse_args(
        ["process", "/media", "--output-prefix", "final_", "--output-suffix", "_synced"]
    )

    assert args.output_prefix == "final_"
    assert args.output_suffix == "_synced"


def test_분석_CLI는_JSON_리포트_경로를_허용한다() -> None:
    args = build_parser().parse_args(
        ["analyze", "/video", "--audio-dir", "/audio", "--report", "/tmp/report.json"]
    )

    assert args.command == "analyze"
    assert args.report == Path("/tmp/report.json")
    assert not args.json


def test_분석_CLI는_JSON_표준_출력_플래그를_허용한다() -> None:
    args = build_parser().parse_args(["analyze", "/video", "--json"])

    assert args.json


def test_분석_CLI는_지원하지_않는_리포트_언어를_거부한다() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["analyze", "/video", "--report-language", "ja"])


def test_처리_CLI는_단위_구간_밖의_카메라_볼륨을_거부한다() -> None:
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


def test_처리_CLI는_외부_오디오_볼륨을_허용한다() -> None:
    args = build_parser().parse_args(
        ["process", "/video", "--mode", "mix", "--external-audio-volume", "0.8"]
    )

    assert args.external_audio_volume == pytest.approx(0.8)


def test_메인_분석은_기본적으로_사람용_요약을_출력한다(
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
        exit_code = main(["analyze", "/video", "--audio-dir", "/audio"])

    assert exit_code == 0
    stdout = capsys.readouterr().out
    assert "분석 결과: 1/1개 매칭 (100.0%)" in stdout
    assert "- clip.mov | 매칭 여부: 성공 | 매칭률: 0.0% | 추천: replace" in stdout
    assert "추천 실행:\n  recordersync process /video --audio-dir /audio" in stdout
    assert '"matched"' not in stdout
    assert pipeline.analyze.call_args.kwargs["match_options"].enable_partial


def test_메인_분석은_요청할_때만_JSON을_출력한다(
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
        exit_code = main(["analyze", "/media", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["matched"] == 1
    assert payload["matches"][0]["recommended_mode"] == "replace"
    assert payload["recommended_command"] == ["recordersync", "process", "/media"]


def test_메인_부분_분석은_안전한_구간에_fallback을_추천한다(
    capsys: pytest.CaptureFixture[str],
) -> None:
    match = AudioMatch(
        Path("clip.mov"),
        100,
        MatchStatus.PARTIAL,
        confidence=0.9,
        peak_margin=0.1,
        segments=(
            AudioMatchSegment(
                "session-001",
                10,
                20,
                30,
                confidence=0.9,
                peak_margin=0.1,
            ),
        ),
    )
    pipeline = MagicMock()
    pipeline.analyze.return_value = AnalysisBundle((), (), (match,))

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        exit_code = main(["analyze", "/media"])

    assert exit_code == 2
    stdout = capsys.readouterr().out
    assert "매칭 여부: 부분" in stdout
    assert "추천: fallback" in stdout
    assert (
        "추천 실행:\n  recordersync process /media --mode fallback "
        "--recommended-only --min-partial-seconds 25" in stdout
    )
    assert pipeline.analyze.call_args.kwargs["match_options"].enable_partial


def test_메인_분석은_입력_옵션과_배치_상태에_맞는_JSON_명령을_추천한다(
    capsys: pytest.CaptureFixture[str],
) -> None:
    partial = AudioMatch(
        Path("partial.mov"),
        100,
        MatchStatus.PARTIAL,
        confidence=0.9,
        peak_margin=0.1,
        segments=(AudioMatchSegment("session-001", 10, 20, 30, confidence=0.9),),
    )
    pipeline = MagicMock()
    pipeline.analyze.return_value = AnalysisBundle(
        (),
        (),
        (AudioMatch(Path("full.mov"), 100, MatchStatus.MATCHED), partial),
    )

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        exit_code = main(
            [
                "analyze",
                "/video dir",
                "--audio-dir",
                "/audio dir",
                "--output-dir",
                "/output dir",
                "--min-confidence",
                "0.8",
                "--min-peak-margin",
                "0.1",
                "--session-gap-seconds",
                "20",
                "--json",
            ]
        )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["recommended_command"] == [
        "recordersync",
        "process",
        "/video dir",
        "--audio-dir",
        "/audio dir",
        "--output-dir",
        "/output dir",
        "--min-confidence",
        "0.8",
        "--min-peak-margin",
        "0.1",
        "--session-gap-seconds",
        "20",
        "--mode",
        "fallback",
        "--recommended-only",
        "--min-partial-seconds",
        "25",
    ]


def test_메인_분석은_처리할_매칭이_없으면_명령을_추천하지_않는다(
    capsys: pytest.CaptureFixture[str],
) -> None:
    pipeline = MagicMock()
    pipeline.analyze.return_value = AnalysisBundle(
        (),
        (),
        (AudioMatch(Path("clip.mov"), 100, MatchStatus.UNMATCHED),),
    )

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        exit_code = main(["analyze", "/media"])

    assert exit_code == 2
    assert "추천 실행 명령 없음" in capsys.readouterr().out


def test_메인_분석은_사람용_요약을_출력하면서_JSON_리포트를_작성한다(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = AnalysisBundle(
        sessions=(),
        videos=(),
        matches=(AudioMatch(Path("clip.mov"), 5, MatchStatus.MATCHED, confidence=0.9),),
    )
    pipeline = MagicMock()
    pipeline.analyze.return_value = bundle
    report_path = tmp_path / "analysis.json"

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        exit_code = main(["analyze", "/media", "--report", str(report_path)])

    assert exit_code == 0
    assert "분석 결과: 1/1개 매칭 (100.0%)" in capsys.readouterr().out
    assert json.loads(report_path.read_text(encoding="utf-8"))["summary"]["matched"] == 1


def test_메인_분석은_저장한_리포트를_재사용하는_명령을_추천한다(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    video_dir = tmp_path / "video"
    video_dir.mkdir()
    video_path = video_dir / "clip.mov"
    video_path.write_bytes(b"video")
    bundle = AnalysisBundle(
        sessions=(),
        videos=(),
        matches=(AudioMatch(video_path, 5, MatchStatus.MATCHED),),
    )
    pipeline = MagicMock()
    pipeline.analyze.return_value = bundle
    report_path = tmp_path / "analysis.json"

    with (
        patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline),
        patch("recordersync.cli.write_analysis_report") as write_report,
    ):
        exit_code = main(["analyze", str(video_dir), "--report", str(report_path)])

    assert exit_code == 0
    expected_command = shlex.join(
        (
            "recordersync",
            "process",
            str(video_dir),
            "--analysis-report",
            str(report_path.resolve()),
        )
    )
    assert expected_command in capsys.readouterr().out
    write_report.assert_called_once()


def test_메인_처리는_분석_리포트를_사용하면_재분석하지_않는다(
    tmp_path: Path,
) -> None:
    video_dir = tmp_path / "video"
    video_dir.mkdir()
    report_path = tmp_path / "analysis.json"
    bundle = AnalysisBundle((), (), ())
    pipeline = MagicMock()
    pipeline.process.return_value = MatchReport(sessions=(), matches=())

    with (
        patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline),
        patch("recordersync.cli.load_analysis_report", return_value=bundle) as load_report,
    ):
        exit_code = main(
            [
                "process",
                str(video_dir),
                "--analysis-report",
                str(report_path),
            ]
        )

    assert exit_code == 0
    load_report.assert_called_once_with(report_path, expected_video_dir=video_dir)
    pipeline.analyze.assert_not_called()
    pipeline.process.assert_called_once()


def test_메인_처리는_분석_리포트와_분석_옵션의_혼용을_거부한다(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "process",
                "/video",
                "--analysis-report",
                "/tmp/analysis.json",
                "--audio-dir",
                "/audio",
            ]
        )

    assert exit_info.value.code == 2
    assert "--analysis-report cannot be combined with analysis options" in capsys.readouterr().err


def test_메인_처리는_입력_분석_리포트_덮어쓰기를_거부한다(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "process",
                "/video",
                "--analysis-report",
                "/tmp/analysis.json",
                "--report",
                "/tmp/analysis.json",
            ]
        )

    assert exit_info.value.code == 2
    assert "--analysis-report and --report must use different paths" in capsys.readouterr().err


def test_메인은_오디오_디렉터리_생략_시_영상_디렉터리를_사용한다() -> None:
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


def test_메인은_선택된_파일과_진행률을_표준_오류에_출력한다(
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


def test_메인은_영문_리포트_사유를_출력할_수_있다(capsys: pytest.CaptureFixture[str]) -> None:
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


def test_메인_모의_실행은_렌더링_없이_부분_종료를_반환한다() -> None:
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


def test_메인_모의_실행은_출력_이름의_접두사와_접미사를_적용한다(
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


def test_메인_폴백_모의_실행은_부분_매칭을_성공으로_처리한다(
    capsys: pytest.CaptureFixture[str],
) -> None:
    match = AudioMatch(
        Path("clip.mov"),
        10,
        MatchStatus.PARTIAL,
        segments=(AudioMatchSegment("session-001", 2, 4, 5, confidence=0.9),),
    )
    pipeline = MagicMock()
    pipeline.analyze.return_value = AnalysisBundle((), (), (match,))

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        exit_code = main(["process", "/video", "--mode", "fallback", "--dry-run"])

    assert exit_code == 0
    stdout = capsys.readouterr().out
    assert '"status": "partial"' in stdout
    assert '"output": "/video/replace/clip.mp4"' in stdout
    assert pipeline.analyze.call_args.kwargs["match_options"].enable_partial
    pipeline.process.assert_not_called()


def test_메인_추천_전용_폴백은_보류된_부분_매칭을_처리하지_않는다(
    capsys: pytest.CaptureFixture[str],
) -> None:
    match = AudioMatch(
        Path("clip.mov"),
        100,
        MatchStatus.PARTIAL,
        confidence=0.9,
        peak_margin=0.1,
        segments=(AudioMatchSegment("session-001", 2, 4, 5, confidence=0.9),),
    )
    pipeline = MagicMock()
    pipeline.analyze.return_value = AnalysisBundle((), (), (match,))

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        exit_code = main(
            [
                "process",
                "/video",
                "--mode",
                "fallback",
                "--recommended-only",
                "--dry-run",
            ]
        )

    assert exit_code == 2
    assert '"output": null' in capsys.readouterr().out
    pipeline.process.assert_not_called()


def test_메인은_치명적인_검증_오류를_보고한다(capsys: pytest.CaptureFixture[str]) -> None:
    pipeline = MagicMock()
    pipeline.analyze.side_effect = ValueError("bad input")

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        exit_code = main(["analyze", "/video", "--audio-dir", "/audio"])

    assert exit_code == 1
    assert "bad input" in capsys.readouterr().err
