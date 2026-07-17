"""RecorderSync CLI 진입점."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from recordersync import __version__
from recordersync.matching import MatchOptions
from recordersync.models import MatchStatus
from recordersync.pipeline import RecorderSyncPipeline
from recordersync.render import RenderMode, resolve_output_path, validate_output_affix
from recordersync.report import MatchReport, ReportLanguage

if TYPE_CHECKING:
    from collections.abc import Sequence


def _unit_interval(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number in [0.0, 1.0]") from exc
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be in [0.0, 1.0]")
    return parsed


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("video_dir", type=Path, help="카메라 영상 파일 디렉터리")
    parser.add_argument(
        "--audio-dir",
        type=Path,
        default=None,
        help="분할된 보이스레코더 오디오 디렉터리(기본: VIDEO_DIR)",
    )
    parser.add_argument("--output-dir", type=Path, default=None, help="기본: VIDEO_DIR/replace")
    parser.add_argument("--report", type=Path, default=None, help="JSON 리포트 저장 경로")
    parser.add_argument(
        "--report-language",
        choices=[language.value for language in ReportLanguage],
        default=ReportLanguage.KO.value,
        help="리포트 사유 언어(기본: ko)",
    )
    parser.add_argument("--min-confidence", type=float, default=0.75)
    parser.add_argument("--min-peak-margin", type=float, default=0.05)
    parser.add_argument("--session-gap-seconds", type=float, default=10.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recordersync",
        description="보이스레코더 녹음과 영상 오디오를 비교해 영상별 음원을 교체합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""사용 예시:
  recordersync analyze VIDEO_DIR
  recordersync analyze VIDEO_DIR --json
  recordersync process VIDEO_DIR
  recordersync process VIDEO_DIR --audio-dir AUDIO_DIR --mode mix

세부 옵션은 `recordersync analyze --help` 또는 `recordersync process --help`로 확인합니다.""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="매칭만 분석하고 사람이 읽는 결과를 출력")
    _add_common_options(analyze)
    analyze.add_argument(
        "--json",
        action="store_true",
        help="기계 처리를 위한 전체 JSON을 표준 출력으로 내보냅니다.",
    )

    process = subparsers.add_parser("process", help="매칭 후 개별 표준화 영상을 생성")
    _add_common_options(process)
    process.add_argument("--mode", choices=[mode.value for mode in RenderMode], default="replace")
    process.add_argument(
        "--camera-audio-volume",
        type=_unit_interval,
        default=0.1,
        help="원본 영상 오디오 볼륨(0.0~1.0, mix 전용, 기본: 0.1)",
    )
    process.add_argument(
        "--external-audio-volume",
        type=_unit_interval,
        default=1.0,
        help="외부 보이스레코더 오디오 볼륨(0.0~1.0, 기본: 1.0)",
    )
    process.add_argument(
        "--output-prefix",
        type=validate_output_affix,
        default="",
        help="출력 파일명 앞에 붙일 문자열",
    )
    process.add_argument(
        "--output-suffix",
        type=validate_output_affix,
        default="",
        help="출력 파일명 뒤에 붙일 문자열",
    )
    process.add_argument("--overwrite", action="store_true")
    process.add_argument("--dry-run", action="store_true")
    return parser


def _match_options(args: argparse.Namespace) -> MatchOptions:
    return MatchOptions(
        min_confidence=args.min_confidence,
        min_peak_margin=args.min_peak_margin,
    )


def _exit_code(report: MatchReport) -> int:
    return 0 if all(match.status is MatchStatus.MATCHED for match in report.matches) else 2


def _print_selection(kind: str, paths: tuple[Path, ...]) -> None:
    labels = {"audio": "오디오", "video": "영상"}
    label = labels.get(kind, kind)
    print(f"선택된 {label} 파일 ({len(paths)}개)", file=sys.stderr)
    for path in paths:
        print(f"  - {path}", file=sys.stderr)


def _print_progress(stage: str, current: int, total: int, item: str) -> None:
    labels = {"audio": "오디오 분석", "match": "영상 매칭", "render": "영상 렌더"}
    label = labels.get(stage, stage)
    percent = 100 if total == 0 else round(current / total * 100)
    detail = f" {item}" if item else ""
    print(f"[{label}] {current}/{total} ({percent}%){detail}", file=sys.stderr)


def _dry_run_report(
    bundle: object,
    output_dir: Path,
    *,
    output_prefix: str,
    output_suffix: str,
) -> MatchReport:
    from recordersync.pipeline import AnalysisBundle

    if not isinstance(bundle, AnalysisBundle):
        raise TypeError("bundle must be AnalysisBundle")
    matches = tuple(
        replace(
            match,
            output_path=(
                resolve_output_path(
                    match.video_path,
                    output_dir,
                    prefix=output_prefix,
                    suffix=output_suffix,
                )
                if match.status is MatchStatus.MATCHED
                else None
            ),
        )
        for match in bundle.matches
    )
    return MatchReport(sessions=bundle.sessions, matches=matches)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if not arguments:
        parser.print_help()
        return 0
    args = parser.parse_args(arguments)
    pipeline = RecorderSyncPipeline()
    audio_dir = args.audio_dir or args.video_dir
    output_dir = args.output_dir or args.video_dir / "replace"
    report_language = ReportLanguage(args.report_language)

    try:
        bundle = pipeline.analyze(
            args.video_dir,
            audio_dir,
            output_dir=output_dir,
            match_options=_match_options(args),
            session_gap_seconds=args.session_gap_seconds,
            selection_callback=_print_selection,
            progress_callback=_print_progress,
        )
        if args.command == "analyze":
            report = bundle.report()
        elif args.dry_run:
            report = _dry_run_report(
                bundle,
                output_dir,
                output_prefix=args.output_prefix,
                output_suffix=args.output_suffix,
            )
        else:
            report = pipeline.process(
                bundle,
                output_dir,
                mode=RenderMode(args.mode),
                camera_audio_volume=args.camera_audio_volume,
                external_audio_volume=args.external_audio_volume,
                overwrite=args.overwrite,
                output_prefix=args.output_prefix,
                output_suffix=args.output_suffix,
                progress_callback=_print_progress,
            )

        report_path = args.report
        if report_path is None and args.command == "process" and not args.dry_run:
            report_path = output_dir / "recordersync-report.json"
        if report_path is not None:
            report.write(report_path, language=report_language)
        if args.command == "analyze" and not args.json:
            print(report.to_text(language=report_language))
        else:
            print(report.to_json(language=report_language))
        return _exit_code(report)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"recordersync: {exc}", file=sys.stderr)
        return 1
