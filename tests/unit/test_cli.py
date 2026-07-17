"""공개 CLI 인자 계약."""

# ruff: noqa: N802 - 테스트 이름은 한국어 문장으로 작성한다.

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from recordersync.cli import build_parser, main
from recordersync.models import AudioMatch, MatchStatus
from recordersync.pipeline import AnalysisBundle


def test_인자_없는_메인은_도움말을_출력하고_성공을_반환한다(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0

    stdout = capsys.readouterr().out
    assert "usage: recordersync" in stdout
    assert "recordersync analyze VIDEO_DIR" in stdout
    assert "recordersync process VIDEO_DIR" in stdout


def test_처리_도움말은_두_오디오_볼륨_옵션을_안내한다(
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


def test_처리_CLI는_안전한_교체_정책을_기본값으로_사용한다() -> None:
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
    assert not args.json
    assert not args.overwrite


def test_처리_CLI는_오디오_디렉터리_생략을_허용한다() -> None:
    args = build_parser().parse_args(["process", "/media"])

    assert args.video_dir == Path("/media")
    assert args.audio_dir is None


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
    assert "- clip.mov | 매칭 여부: 성공 | 매칭률: 0.0%" in stdout
    assert '"matched"' not in stdout


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
    stdout = capsys.readouterr().out
    assert '"matched": 1' in stdout
    assert "분석 결과:" not in stdout


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


def test_메인은_치명적인_검증_오류를_보고한다(capsys: pytest.CaptureFixture[str]) -> None:
    pipeline = MagicMock()
    pipeline.analyze.side_effect = ValueError("bad input")

    with patch("recordersync.cli.RecorderSyncPipeline", return_value=pipeline):
        exit_code = main(["analyze", "/video", "--audio-dir", "/audio"])

    assert exit_code == 1
    assert "bad input" in capsys.readouterr().err
