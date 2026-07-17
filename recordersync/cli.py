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
from recordersync.render import RenderMode, resolve_output_path
from recordersync.report import MatchReport

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
        required=True,
        help="분할된 보이스레코더 오디오 디렉터리",
    )
    parser.add_argument("--output-dir", type=Path, default=None, help="기본: VIDEO_DIR/replace")
    parser.add_argument("--report", type=Path, default=None, help="JSON 리포트 저장 경로")
    parser.add_argument("--min-confidence", type=float, default=0.75)
    parser.add_argument("--min-peak-margin", type=float, default=0.05)
    parser.add_argument("--session-gap-seconds", type=float, default=10.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recordersync",
        description="보이스레코더 녹음과 영상 오디오를 비교해 영상별 음원을 교체합니다.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="매칭만 분석하고 JSON을 출력")
    _add_common_options(analyze)

    process = subparsers.add_parser("process", help="매칭 후 개별 표준화 영상을 생성")
    _add_common_options(process)
    process.add_argument("--mode", choices=[mode.value for mode in RenderMode], default="replace")
    process.add_argument("--camera-audio-volume", type=_unit_interval, default=0.1)
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


def _dry_run_report(bundle: object, output_dir: Path) -> MatchReport:
    from recordersync.pipeline import AnalysisBundle

    if not isinstance(bundle, AnalysisBundle):
        raise TypeError("bundle must be AnalysisBundle")
    matches = tuple(
        replace(
            match,
            output_path=(
                resolve_output_path(match.video_path, output_dir)
                if match.status is MatchStatus.MATCHED
                else None
            ),
        )
        for match in bundle.matches
    )
    return MatchReport(sessions=bundle.sessions, matches=matches)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pipeline = RecorderSyncPipeline()
    output_dir = args.output_dir or args.video_dir / "replace"

    try:
        bundle = pipeline.analyze(
            args.video_dir,
            args.audio_dir,
            output_dir=output_dir,
            match_options=_match_options(args),
            session_gap_seconds=args.session_gap_seconds,
        )
        if args.command == "analyze":
            report = bundle.report()
        elif args.dry_run:
            report = _dry_run_report(bundle, output_dir)
        else:
            report = pipeline.process(
                bundle,
                output_dir,
                mode=RenderMode(args.mode),
                camera_audio_volume=args.camera_audio_volume,
                overwrite=args.overwrite,
            )

        report_path = args.report
        if report_path is None and args.command == "process" and not args.dry_run:
            report_path = output_dir / "recordersync-report.json"
        if report_path is not None:
            report.write(report_path)
        print(report.to_json())
        return _exit_code(report)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"recordersync: {exc}", file=sys.stderr)
        return 1
