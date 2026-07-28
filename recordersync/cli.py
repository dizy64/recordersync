"""RecorderSync CLI 진입점."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from recordersync import __version__
from recordersync.analysis_plan import load_analysis_report, write_analysis_report
from recordersync.audio_levels import AudioLevelPolicy, OutputChannelLayout
from recordersync.matching import MatchOptions
from recordersync.mix_analysis import MixProfile
from recordersync.models import AudioMatch, MatchStatus
from recordersync.pipeline import AnalysisBundle, RecorderSyncPipeline, is_renderable_match
from recordersync.recommendation import (
    RecommendationMode,
    recommend_batch_mode,
)
from recordersync.render import RenderMode, resolve_output_path, validate_output_affix
from recordersync.report import (
    MatchReport,
    ReportLanguage,
    format_audio_level_summary,
    format_mix_recommendation_summary,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


_DEFAULT_MATCH_OPTIONS = MatchOptions()
_DEFAULT_SESSION_GAP_SECONDS = 10.0


def _unit_interval(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number in [0.0, 1.0]") from exc
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be in [0.0, 1.0]")
    return parsed


def _highpass_frequency(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be 0 or a number in [20, 20000]") from exc
    if parsed != 0 and not 20 <= parsed <= 20_000:
        raise argparse.ArgumentTypeError("must be 0 or in [20, 20000]")
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
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=_DEFAULT_MATCH_OPTIONS.min_confidence,
        help=f"매칭으로 승인할 최소 종합 신뢰도(0 초과~1.0, 기본: {_DEFAULT_MATCH_OPTIONS.min_confidence:g})",
    )
    parser.add_argument(
        "--min-peak-margin",
        type=float,
        default=_DEFAULT_MATCH_OPTIONS.min_peak_margin,
        help=f"최고·차순위 후보의 최소 상관 peak 차이(0.0~2.0, 기본: {_DEFAULT_MATCH_OPTIONS.min_peak_margin:g})",
    )
    parser.add_argument(
        "--min-partial-seconds",
        type=float,
        default=_DEFAULT_MATCH_OPTIONS.min_partial_duration_seconds,
        help="부분 매칭으로 승인할 최소 연속 구간 길이(기본: 5.0)",
    )
    parser.add_argument(
        "--session-gap-seconds",
        type=float,
        default=_DEFAULT_SESSION_GAP_SECONDS,
        help=f"새 녹음 세션으로 분리할 시간 공백(0 이상, 기본: {_DEFAULT_SESSION_GAP_SECONDS:g}초)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recordersync",
        description="보이스레코더 녹음과 영상 오디오를 비교해 영상별 음원을 교체합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""사용 예시:
  recordersync analyze VIDEO_DIR
  recordersync analyze VIDEO_DIR --json
  recordersync analyze VIDEO_DIR --full-only
  recordersync process VIDEO_DIR
  recordersync process VIDEO_DIR --audio-dir AUDIO_DIR --mode mix
  recordersync process VIDEO_DIR --mode fallback

세부 옵션은 `recordersync analyze --help` 또는 `recordersync process --help`로 확인합니다.""",
    )
    parser.set_defaults(json=False)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser(
        "analyze",
        help="매칭과 권장 처리 모드를 분석해 사람이 읽는 결과를 출력",
    )
    _add_common_options(analyze)
    analyze.add_argument(
        "--json",
        action="store_true",
        help="기계 처리를 위한 전체 JSON을 표준 출력으로 내보냅니다.",
    )
    partial_group = analyze.add_mutually_exclusive_group()
    partial_group.add_argument(
        "--partial",
        dest="partial",
        action="store_true",
        help="부분·다중 구간과 fallback 추천을 분석합니다(기본 동작).",
    )
    partial_group.add_argument(
        "--full-only",
        dest="partial",
        action="store_false",
        help="부분 탐색을 생략하고 전체 일치만 빠르게 분석합니다.",
    )
    analyze.set_defaults(partial=True)

    process = subparsers.add_parser("process", help="매칭 후 개별 표준화 영상을 생성")
    _add_common_options(process)
    process.add_argument(
        "--mode",
        choices=[mode.value for mode in RenderMode],
        default="replace",
        help="오디오 처리 방식: replace=전체 교체, mix=혼합, fallback=일치 구간만 교체(기본: replace)",
    )
    process.add_argument(
        "--mix-profile",
        choices=[profile.value for profile in MixProfile],
        default=MixProfile.CONSERVATIVE.value,
        help="mix 정책: conservative=검증된 고정값, auto=원본 측정 기반 추천·적용(기본: conservative)",
    )
    process.add_argument(
        "--camera-audio-volume",
        type=_unit_interval,
        default=None,
        help="원본 영상 오디오 볼륨(기본: mix/fallback 1.0)",
    )
    process.add_argument(
        "--external-audio-volume",
        type=_unit_interval,
        default=None,
        help="외부 보이스레코더 오디오 볼륨(기본: mix -12dB 상당, replace/fallback 1.0)",
    )
    process.add_argument(
        "--external-highpass-hz",
        type=_highpass_frequency,
        default=None,
        help="mix 외부 오디오 high-pass 주파수(기본: 80Hz, 0은 해제)",
    )
    process.add_argument(
        "--target-lufs",
        type=float,
        default=None,
        help="static gain 음량 안전 모드의 목표 integrated loudness([-70, -5] LUFS)",
    )
    process.add_argument(
        "--max-true-peak-dbtp",
        type=float,
        default=None,
        help="static gain 음량 안전 모드의 최종 최대 true peak([-99, 0] dBTP)",
    )
    process.add_argument(
        "--output-channel-layout",
        choices=[layout.value for layout in OutputChannelLayout],
        default=None,
        help="음량 안전 모드의 출력 채널 정책: preserve, mono, stereo",
    )
    process.add_argument(
        "--loudness-tolerance-lu",
        type=float,
        default=None,
        help="최종 AAC integrated loudness의 허용 오차((0, 10] LU)",
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
    process.add_argument(
        "--recommended-only",
        action="store_true",
        help="fallback에서 analyze가 추천한 안전한 부분 매칭만 렌더링",
    )
    process.add_argument(
        "--analysis-report",
        type=Path,
        default=None,
        help="analyze --report 결과를 검증해 재분석 없이 처리",
    )
    process.add_argument(
        "--overwrite",
        action="store_true",
        help="기존 출력 파일 덮어쓰기",
    )
    process.add_argument(
        "--dry-run",
        action="store_true",
        help="렌더하지 않고 예상 처리 결과와 출력 경로만 JSON으로 출력",
    )
    return parser


def _match_options(args: argparse.Namespace) -> MatchOptions:
    return MatchOptions(
        min_confidence=args.min_confidence,
        min_peak_margin=args.min_peak_margin,
        enable_partial=(
            bool(getattr(args, "partial", False)) or getattr(args, "mode", None) == RenderMode.FALLBACK.value
        ),
        min_partial_duration_seconds=args.min_partial_seconds,
    )


def _exit_code(report: MatchReport, *, accept_partial: bool = False) -> int:
    def is_successful(match: AudioMatch) -> bool:
        if match.status is MatchStatus.MATCHED:
            return True
        return accept_partial and match.status is MatchStatus.PARTIAL and match.output_path is not None

    return 0 if all(is_successful(match) for match in report.matches) else 2


def _format_cli_number(value: float) -> str:
    return format(value, ".15g")


def _recommended_process_command(
    args: argparse.Namespace,
    report: MatchReport,
) -> tuple[str, ...] | None:
    recommendation = recommend_batch_mode(report.matches)
    if recommendation.mode is None:
        return None

    command = ["recordersync", "process", str(args.video_dir)]
    if args.report is not None:
        command.extend(("--analysis-report", str(args.report.resolve())))
    elif args.audio_dir is not None:
        command.extend(("--audio-dir", str(args.audio_dir)))
    if args.output_dir is not None:
        command.extend(("--output-dir", str(args.output_dir)))
    if args.report is None and args.min_confidence != _DEFAULT_MATCH_OPTIONS.min_confidence:
        command.extend(("--min-confidence", _format_cli_number(args.min_confidence)))
    if args.report is None and args.min_peak_margin != _DEFAULT_MATCH_OPTIONS.min_peak_margin:
        command.extend(("--min-peak-margin", _format_cli_number(args.min_peak_margin)))
    if args.report is None and args.session_gap_seconds != _DEFAULT_SESSION_GAP_SECONDS:
        command.extend(("--session-gap-seconds", _format_cli_number(args.session_gap_seconds)))
    if recommendation.mode is RecommendationMode.FALLBACK:
        if recommendation.minimum_contiguous_seconds is None:
            raise ValueError("fallback recommendation requires a contiguous duration")
        minimum_partial_seconds = max(
            args.min_partial_seconds,
            recommendation.minimum_contiguous_seconds,
        )
        command.extend(
            (
                "--mode",
                "fallback",
                "--recommended-only",
                "--min-partial-seconds",
                _format_cli_number(minimum_partial_seconds),
            )
        )
    return tuple(command)


def _print_selection(kind: str, paths: tuple[Path, ...]) -> None:
    labels = {"audio": "오디오", "video": "영상"}
    label = labels.get(kind, kind)
    print(f"선택된 {label} 파일 ({len(paths)}개)", file=sys.stderr)
    for path in paths:
        print(f"  - {path}", file=sys.stderr)


def _print_progress(stage: str, current: int, total: int, item: str) -> None:
    labels = {"audio": "오디오 분석", "match": "영상 매칭", "mix": "믹스 분석", "render": "영상 렌더"}
    label = labels.get(stage, stage)
    percent = 100 if total == 0 else round(current / total * 100)
    detail = f" {item}" if item else ""
    print(f"[{label}] {current}/{total} ({percent}%){detail}", file=sys.stderr)


def _dry_run_report(
    bundle: object,
    output_dir: Path,
    *,
    mode: RenderMode,
    recommended_only: bool,
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
                if is_renderable_match(match, mode, recommended_only=recommended_only)
                else None
            ),
        )
        for match in bundle.matches
    )
    return MatchReport(sessions=bundle.sessions, matches=matches)


def _validate_process_options(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    render_mode: RenderMode,
) -> None:
    if args.command != "process":
        return
    if args.recommended_only and render_mode is not RenderMode.FALLBACK:
        parser.error("--recommended-only requires --mode fallback")
    _validate_auto_mix_options(parser, args, render_mode)
    if args.external_highpass_hz is not None and render_mode is not RenderMode.MIX:
        parser.error("--external-highpass-hz requires --mode mix")
    loudness_values = (
        args.target_lufs,
        args.max_true_peak_dbtp,
        args.output_channel_layout,
        args.loudness_tolerance_lu,
    )
    if any(value is not None for value in loudness_values):
        if not all(value is not None for value in loudness_values):
            parser.error("loudness safety options must be provided together")
        if render_mode not in {RenderMode.REPLACE, RenderMode.MIX}:
            parser.error("loudness safety requires --mode replace or mix")
        if (
            render_mode is RenderMode.REPLACE
            and args.external_audio_volume is not None
            and args.external_audio_volume != 1.0
        ):
            parser.error("--external-audio-volume cannot be combined with loudness safety")
        if render_mode is RenderMode.MIX and args.output_channel_layout != OutputChannelLayout.STEREO.value:
            parser.error("mix loudness safety requires --output-channel-layout stereo")
        if args.dry_run:
            parser.error("loudness safety cannot be combined with --dry-run")
    if args.analysis_report is None:
        return
    if (
        args.audio_dir is not None
        or args.min_confidence != _DEFAULT_MATCH_OPTIONS.min_confidence
        or args.min_peak_margin != _DEFAULT_MATCH_OPTIONS.min_peak_margin
        or args.min_partial_seconds != _DEFAULT_MATCH_OPTIONS.min_partial_duration_seconds
        or args.session_gap_seconds != _DEFAULT_SESSION_GAP_SECONDS
    ):
        parser.error("--analysis-report cannot be combined with analysis options")
    if args.report is not None and args.analysis_report.resolve() == args.report.resolve():
        parser.error("--analysis-report and --report must use different paths")


def _validate_auto_mix_options(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    render_mode: RenderMode,
) -> None:
    if args.mix_profile != MixProfile.AUTO.value:
        return
    if render_mode is not RenderMode.MIX:
        parser.error("--mix-profile auto requires --mode mix")
    if any(
        value is not None
        for value in (
            args.camera_audio_volume,
            args.external_audio_volume,
            args.external_highpass_hz,
            args.target_lufs,
            args.max_true_peak_dbtp,
            args.output_channel_layout,
            args.loudness_tolerance_lu,
        )
    ):
        parser.error("--mix-profile auto cannot be combined with manual mix options")


def _audio_level_policy(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> AudioLevelPolicy | None:
    if args.command != "process":
        return None
    if args.target_lufs is None:
        return None
    try:
        return AudioLevelPolicy(
            target_lufs=args.target_lufs,
            maximum_true_peak_dbtp=args.max_true_peak_dbtp,
            output_channel_layout=OutputChannelLayout(args.output_channel_layout),
            loudness_tolerance_lu=args.loudness_tolerance_lu,
        )
    except ValueError as exc:
        parser.error(str(exc))


def _analysis_bundle(
    args: argparse.Namespace,
    pipeline: RecorderSyncPipeline,
    *,
    audio_dir: Path,
    output_dir: Path,
) -> AnalysisBundle:
    analysis_report = getattr(args, "analysis_report", None)
    if analysis_report is not None:
        bundle = load_analysis_report(
            analysis_report,
            expected_video_dir=args.video_dir,
        )
        print(f"분석 리포트 재사용: {analysis_report}", file=sys.stderr)
        return bundle
    return pipeline.analyze(
        args.video_dir,
        audio_dir,
        output_dir=output_dir,
        match_options=_match_options(args),
        session_gap_seconds=args.session_gap_seconds,
        selection_callback=_print_selection,
        progress_callback=_print_progress,
    )


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
    render_mode = RenderMode(args.mode) if args.command == "process" else RenderMode.REPLACE
    mix_profile = MixProfile(args.mix_profile) if args.command == "process" else MixProfile.CONSERVATIVE
    _validate_process_options(parser, args, render_mode)
    audio_level_policy = _audio_level_policy(parser, args)

    try:
        bundle = _analysis_bundle(
            args,
            pipeline,
            audio_dir=audio_dir,
            output_dir=output_dir,
        )
        if args.command == "analyze":
            report = bundle.report()
            report = replace(
                report,
                recommended_command=_recommended_process_command(args, report),
                include_recommended_command=True,
            )
        elif args.dry_run and mix_profile is not MixProfile.AUTO:
            report = _dry_run_report(
                bundle,
                output_dir,
                mode=render_mode,
                recommended_only=args.recommended_only,
                output_prefix=args.output_prefix,
                output_suffix=args.output_suffix,
            )
        else:
            report = pipeline.process(
                bundle,
                output_dir,
                mode=render_mode,
                recommended_only=args.recommended_only,
                mix_profile=mix_profile,
                recommend_mix_only=args.dry_run and mix_profile is MixProfile.AUTO,
                camera_audio_volume=args.camera_audio_volume,
                external_audio_volume=args.external_audio_volume,
                external_highpass_hz=args.external_highpass_hz,
                overwrite=args.overwrite,
                output_prefix=args.output_prefix,
                output_suffix=args.output_suffix,
                audio_level_policy=audio_level_policy,
                progress_callback=_print_progress,
            )

        if args.command == "process" and report.mix_recommendations:
            for match, recommendation in zip(report.matches, report.mix_recommendations, strict=True):
                if recommendation is not None:
                    print(
                        f"[믹스 추천] {format_mix_recommendation_summary(match, recommendation)}",
                        file=sys.stderr,
                    )
        if args.command == "process" and report.audio_levels:
            for match, audio_levels in zip(report.matches, report.audio_levels, strict=True):
                if audio_levels is not None:
                    print(
                        f"[음량 검증] {format_audio_level_summary(match, audio_levels)}",
                        file=sys.stderr,
                    )

        report_path = args.report
        if report_path is None and args.command == "process" and not args.dry_run:
            report_path = output_dir / "recordersync-report.json"
        if report_path is not None:
            if args.command == "analyze":
                write_analysis_report(
                    report,
                    bundle,
                    report_path,
                    language=report_language,
                )
            else:
                report.write(report_path, language=report_language)
        if args.command == "analyze" and not args.json:
            print(report.to_text(language=report_language))
        else:
            print(report.to_json(language=report_language))
        return _exit_code(
            report,
            accept_partial=args.command == "process" and render_mode is RenderMode.FALLBACK,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"recordersync: {exc}", file=sys.stderr)
        return 1
