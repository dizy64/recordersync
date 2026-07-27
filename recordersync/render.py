"""TubeArchive 호환 FFmpeg 렌더 계획과 실행기."""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from recordersync.audio_levels import (
    AudioLevelMetrics,
    AudioLevelPolicy,
    AudioLevelReport,
    OutputChannelLayout,
    decide_static_gain,
    parse_ebur128_summary,
    validate_output_metrics,
)
from recordersync.media import VideoInfo
from recordersync.models import AudioMatch, MatchStatus, RecordingSession


class RenderError(RuntimeError):
    """하드웨어와 소프트웨어 렌더가 모두 실패한 경우."""


class AudioLevelRenderError(RenderError):
    """음량 정책 충돌 또는 최종 AAC 검증 실패."""

    def __init__(self, message: str, report: AudioLevelReport) -> None:
        super().__init__(message)
        self.report = report


class RenderMode(StrEnum):
    REPLACE = "replace"
    MIX = "mix"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class RenderSegment:
    """영상의 한 구간에 배치할 외부 녹음 입력."""

    session: RecordingSession
    video_start_seconds: float
    external_start_seconds: float
    duration_seconds: float
    tempo_ratio: float = 1.0

    def __post_init__(self) -> None:
        if self.video_start_seconds < 0:
            raise ValueError("video_start_seconds must be >= 0")
        if self.external_start_seconds < 0:
            raise ValueError("external_start_seconds must be >= 0")
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0")
        if not 0.5 <= self.tempo_ratio <= 2.0:
            raise ValueError("tempo_ratio must be in [0.5, 2.0]")
        external_end = self.external_start_seconds + self.duration_seconds * self.tempo_ratio
        if external_end > self.session.duration_seconds + 1e-6:
            raise ValueError("render segment exceeds recording session duration")

    @property
    def video_end_seconds(self) -> float:
        return self.video_start_seconds + self.duration_seconds


@dataclass(frozen=True, slots=True)
class RenderPlan:
    """한 영상에 승인된 외부 오디오 구간을 적용하는 불변 계획."""

    video: VideoInfo
    session: RecordingSession
    output_path: Path
    external_start_seconds: float
    tempo_ratio: float
    mode: RenderMode = RenderMode.REPLACE
    camera_audio_volume: float = 0.1
    external_audio_volume: float = 1.0
    overwrite: bool = False
    segments: tuple[RenderSegment, ...] = ()
    crossfade_seconds: float = 0.05
    audio_level_policy: AudioLevelPolicy | None = None
    external_audio_gain_db: float | None = None

    def __post_init__(self) -> None:
        if self.external_start_seconds < 0:
            raise ValueError("external_start_seconds must be >= 0")
        if not 0.5 <= self.tempo_ratio <= 2.0:
            raise ValueError("tempo_ratio must be in [0.5, 2.0]")
        if not 0 <= self.camera_audio_volume <= 1:
            raise ValueError("camera_audio_volume must be in [0, 1]")
        if not 0 <= self.external_audio_volume <= 1:
            raise ValueError("external_audio_volume must be in [0, 1]")
        if self.crossfade_seconds < 0:
            raise ValueError("crossfade_seconds must be >= 0")
        if self.mode in {RenderMode.MIX, RenderMode.FALLBACK} and not self.video.has_audio:
            raise ValueError(f"{self.mode.value} mode requires camera audio")
        if self.segments and self.mode is not RenderMode.FALLBACK:
            raise ValueError("explicit render segments require fallback mode")
        if self.audio_level_policy is not None and self.mode is not RenderMode.REPLACE:
            raise ValueError("loudness safety requires replace mode")
        if self.audio_level_policy is not None and self.external_audio_volume != 1.0:
            raise ValueError("loudness safety cannot be combined with external_audio_volume")
        if self.external_audio_gain_db is not None and self.audio_level_policy is None:
            raise ValueError("external_audio_gain_db requires audio_level_policy")

        previous_end = 0.0
        for index, segment in enumerate(self.segments):
            if index and segment.video_start_seconds < previous_end - 1e-6:
                raise ValueError("render segments must not overlap")
            if segment.video_end_seconds > self.video.duration_seconds + 1e-6:
                raise ValueError("render segment exceeds video duration")
            previous_end = segment.video_end_seconds

    @property
    def resolved_segments(self) -> tuple[RenderSegment, ...]:
        if self.segments:
            return self.segments
        return (
            RenderSegment(
                self.session,
                0.0,
                self.external_start_seconds,
                self.video.duration_seconds,
                self.tempo_ratio,
            ),
        )


def _escape_concat_path(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def build_concat_manifest(session: RecordingSession) -> str:
    """FFmpeg concat demuxer용 안전한 파일 목록 문자열."""

    return "\n".join(f"file '{_escape_concat_path(chunk.path.resolve())}'" for chunk in session.chunks) + "\n"


def validate_output_affix(value: str) -> str:
    """파일명 접두사·접미사가 출력 디렉터리를 벗어나지 않도록 검증한다."""

    if "/" in value or "\\" in value or "\x00" in value:
        raise ValueError("output prefix/suffix must not contain a path separator")
    return value


def resolve_output_path(
    video_path: Path,
    output_dir: Path,
    *,
    prefix: str = "",
    suffix: str = "",
) -> Path:
    safe_prefix = validate_output_affix(prefix)
    safe_suffix = validate_output_affix(suffix)
    return output_dir / f"{safe_prefix}{video_path.stem}{safe_suffix}.mp4"


def _resolve_render_segments(
    match: AudioMatch,
    session_by_id: Mapping[str, RecordingSession],
) -> tuple[RenderSegment, ...]:
    missing = {segment.session_id for segment in match.segments if segment.session_id not in session_by_id}
    if missing:
        raise ValueError("Match does not belong to the supplied recording sessions")
    return tuple(
        RenderSegment(
            session=_indexed_session(session_by_id, segment.session_id),
            video_start_seconds=segment.video_start_seconds,
            external_start_seconds=segment.external_start_seconds,
            duration_seconds=segment.duration_seconds,
            tempo_ratio=segment.tempo_ratio,
        )
        for segment in match.segments
    )


def _indexed_session(
    session_by_id: Mapping[str, RecordingSession],
    session_id: str,
) -> RecordingSession:
    session = session_by_id[session_id]
    if session.id != session_id:
        raise ValueError("Session mapping keys must match RecordingSession.id")
    return session


def build_render_plan(
    match: AudioMatch,
    video: VideoInfo,
    session: RecordingSession | Sequence[RecordingSession] | Mapping[str, RecordingSession],
    output_dir: Path,
    *,
    mode: RenderMode = RenderMode.REPLACE,
    camera_audio_volume: float | None = None,
    external_audio_volume: float = 1.0,
    overwrite: bool = False,
    output_prefix: str = "",
    output_suffix: str = "",
    audio_level_policy: AudioLevelPolicy | None = None,
) -> RenderPlan:
    """승인된 매칭과 미디어 메타데이터를 검증하고 렌더 계획으로 변환한다."""

    if match.status is MatchStatus.PARTIAL and mode is not RenderMode.FALLBACK:
        raise ValueError("Partial audio can only be rendered in fallback mode")
    if match.status not in {MatchStatus.MATCHED, MatchStatus.PARTIAL}:
        raise ValueError("Only matched or partial audio can be rendered")
    if match.video_path != video.path:
        raise ValueError("Match video path does not match supplied video")

    if isinstance(session, RecordingSession):
        session_by_id: Mapping[str, RecordingSession] = {session.id: session}
    elif isinstance(session, Mapping):
        session_by_id = session
    else:
        session_by_id = {item.id: item for item in session}
    render_segments = _resolve_render_segments(match, session_by_id)
    if render_segments:
        primary_session = render_segments[0].session
        external_start = render_segments[0].external_start_seconds
        tempo_ratio = render_segments[0].tempo_ratio
    else:
        session_id = match.session_id
        match_external_start = match.external_start_seconds
        if session_id is None or match_external_start is None or session_id not in session_by_id:
            raise ValueError("Match does not belong to the supplied recording session")
        primary_session = _indexed_session(session_by_id, session_id)
        external_start = match_external_start
        tempo_ratio = match.tempo_ratio

    resolved_camera_volume = (
        camera_audio_volume if camera_audio_volume is not None else (1.0 if mode is RenderMode.FALLBACK else 0.1)
    )
    return RenderPlan(
        video=video,
        session=primary_session,
        output_path=resolve_output_path(
            video.path,
            output_dir,
            prefix=output_prefix,
            suffix=output_suffix,
        ),
        external_start_seconds=external_start,
        tempo_ratio=tempo_ratio,
        mode=mode,
        camera_audio_volume=resolved_camera_volume,
        external_audio_volume=external_audio_volume,
        overwrite=overwrite,
        segments=render_segments if mode is RenderMode.FALLBACK else (),
        audio_level_policy=audio_level_policy,
    )


def _number(value: float) -> str:
    return f"{value:.9f}".rstrip("0").rstrip(".") or "0"


def _video_filter(video: VideoInfo) -> list[str]:
    if video.color_transfer in {"arib-std-b67", "smpte2084"}:
        return [
            ("[0:v:0]colorspace=all=bt709:iall=bt2020:dither=fsb,format=yuv420p10le,format=p010le[vout]"),
        ]
    return ["[0:v:0]format=p010le[vout]"]


class FFmpegCommandBuilder:
    """쉘을 사용하지 않는 FFmpeg 인자 목록 생성기."""

    def __init__(self, ffmpeg_path: str = "ffmpeg") -> None:
        self.ffmpeg_path = ffmpeg_path

    @staticmethod
    def expected_output_channels(plan: RenderPlan) -> int:
        policy = plan.audio_level_policy
        source_channels = plan.session.chunks[0].channels
        if policy is None or policy.output_channel_layout is OutputChannelLayout.PRESERVE:
            return source_channels
        if policy.output_channel_layout is OutputChannelLayout.MONO:
            return 1
        return 2

    @staticmethod
    def _approved_channel_filter(plan: RenderPlan) -> str | None:
        policy = plan.audio_level_policy
        if policy is None:
            return None
        source_channels = plan.session.chunks[0].channels
        if source_channels not in {1, 2}:
            raise ValueError("loudness safety supports mono or stereo recorder audio")
        layout = policy.output_channel_layout
        if layout is OutputChannelLayout.PRESERVE:
            return "aformat=channel_layouts=mono" if source_channels == 1 else "aformat=channel_layouts=stereo"
        if layout is OutputChannelLayout.MONO:
            return "aformat=channel_layouts=mono" if source_channels == 1 else "pan=mono|c0=0.5*c0+0.5*c1"
        return "pan=stereo|c0=c0|c1=c0" if source_channels == 1 else "aformat=channel_layouts=stereo"

    @classmethod
    def _replace_audio_chain(
        cls,
        plan: RenderPlan,
        *,
        include_gain: bool,
    ) -> str:
        filters: list[str] = []
        if include_gain:
            if plan.audio_level_policy is None:
                filters.append(f"volume={_number(plan.external_audio_volume)}")
            else:
                if plan.external_audio_gain_db is None:
                    raise ValueError("loudness-safe render requires measured static gain")
                filters.append(f"volume={_number(plan.external_audio_gain_db)}dB")
        filters.extend(
            [
                f"atempo={_number(plan.tempo_ratio)}",
                "apad",
                f"atrim=duration={_number(plan.video.duration_seconds)}",
                "asetpts=PTS-STARTPTS",
            ]
        )
        channel_filter = cls._approved_channel_filter(plan)
        if channel_filter is not None:
            filters.extend((channel_filter, "aformat=sample_fmts=fltp"))
        return ",".join(filters)

    def build(
        self,
        plan: RenderPlan,
        manifest_paths: Path | Mapping[str, Path],
        *,
        software_fallback: bool = False,
    ) -> list[str]:
        duration = _number(plan.video.duration_seconds)
        filters = _video_filter(plan.video)
        segments = plan.resolved_segments
        if plan.mode is RenderMode.FALLBACK:
            fallback_filters, audio_label = self._fallback_audio_filters(plan, segments)
            filters.extend(fallback_filters)
        else:
            filters.append(f"[1:a:0]{self._replace_audio_chain(plan, include_gain=True)}[external]")
            audio_label = "[external]"
            if plan.mode is RenderMode.MIX:
                filters.extend(
                    [
                        (
                            f"[0:a:0]volume={_number(plan.camera_audio_volume)},"
                            f"aresample=48000,apad,atrim=duration={duration},"
                            "asetpts=PTS-STARTPTS[camera]"
                        ),
                        ("[camera][external]amix=inputs=2:duration=first:dropout_transition=0:weights=1 1[aout]"),
                    ]
                )
                audio_label = "[aout]"

        video_codec = "libx265" if software_fallback else "hevc_videotoolbox"
        pixel_format = "yuv420p10le" if software_fallback else "p010le"
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-y" if plan.overwrite else "-n",
            "-fflags",
            "+genpts",
            "-i",
            str(plan.video.path),
        ]
        input_segments = segments if plan.mode is RenderMode.FALLBACK else segments[:1]
        for segment in input_segments:
            command.extend(
                [
                    "-ss",
                    _number(segment.external_start_seconds),
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(self._manifest_path(manifest_paths, segment.session.id)),
                ]
            )
        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[vout]",
                "-map",
                audio_label,
                "-c:v",
                video_codec,
                "-b:v",
                "50M",
                "-pix_fmt",
                pixel_format,
                "-fps_mode:v",
                "passthrough",
                "-color_primaries",
                "bt709",
                "-color_trc",
                "bt709",
                "-colorspace",
                "bt709",
                "-color_range",
                "tv",
                "-tag:v",
                "hvc1",
                "-c:a",
                "aac",
                "-b:a",
                "256k",
                "-ar",
                "48000",
            ]
        )
        if software_fallback:
            command.extend(["-preset", "medium"])
        command.extend(["-movflags", "+faststart", str(plan.output_path)])
        return command

    def build_audio_analysis(
        self,
        plan: RenderPlan,
        manifest_paths: Path | Mapping[str, Path],
    ) -> list[str]:
        """실제 replace 구간을 gain 적용 전 float 상태로 EBU R128 측정한다."""

        if plan.audio_level_policy is None:
            raise ValueError("audio analysis requires audio_level_policy")
        if plan.mode is not RenderMode.REPLACE:
            raise ValueError("audio analysis requires replace mode")
        segment = plan.resolved_segments[0]
        return [
            self.ffmpeg_path,
            "-hide_banner",
            "-nostats",
            "-xerror",
            "-err_detect",
            "explode",
            "-ss",
            _number(segment.external_start_seconds),
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(self._manifest_path(manifest_paths, segment.session.id)),
            "-filter_complex",
            (
                f"[0:a:0]{self._replace_audio_chain(plan, include_gain=False)},"
                "ebur128=peak=sample+true:framelog=quiet[measured]"
            ),
            "-map",
            "[measured]",
            "-f",
            "null",
            "-",
        ]

    def build_output_audio_analysis(self, output_path: Path) -> list[str]:
        """최종 AAC를 오류 즉시 중단 모드로 재디코딩해 EBU R128 측정한다."""

        return [
            self.ffmpeg_path,
            "-hide_banner",
            "-nostats",
            "-xerror",
            "-err_detect",
            "explode",
            "-i",
            str(output_path),
            "-map",
            "0:a:0",
            "-af",
            "aformat=sample_fmts=fltp,ebur128=peak=sample+true:framelog=quiet",
            "-f",
            "null",
            "-",
        ]

    @staticmethod
    def _manifest_path(manifest_paths: Path | Mapping[str, Path], session_id: str) -> Path:
        if isinstance(manifest_paths, Path):
            return manifest_paths
        try:
            return manifest_paths[session_id]
        except KeyError as exc:
            raise ValueError(f"Missing concat manifest for session: {session_id}") from exc

    @staticmethod
    def _fallback_audio_filters(
        plan: RenderPlan,
        segments: tuple[RenderSegment, ...],
    ) -> tuple[list[str], str]:
        if not segments:
            raise ValueError("fallback mode requires at least one render segment")
        fade = min(
            plan.crossfade_seconds,
            min(segment.duration_seconds / 2 for segment in segments),
        )
        filters: list[str] = []
        labels: list[str] = []
        cursor = 0.0
        part_index = 0

        def add_camera(start: float, end: float) -> None:
            nonlocal part_index
            if end <= start:
                return
            label = f"part{part_index}"
            filters.append(
                f"[0:a:0]atrim=start={_number(start)}:end={_number(end)},"
                "asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
                f"volume={_number(plan.camera_audio_volume)}[{label}]"
            )
            labels.append(f"[{label}]")
            part_index += 1

        for segment_index, segment in enumerate(segments, start=1):
            if segment.video_start_seconds > cursor or segment_index > 1:
                camera_start = max(0.0, cursor - (fade if segment_index > 1 else 0.0))
                camera_end = min(
                    plan.video.duration_seconds,
                    segment.video_start_seconds + fade,
                )
                add_camera(camera_start, camera_end)

            label = f"part{part_index}"
            filters.append(
                f"[{segment_index}:a:0]volume={_number(plan.external_audio_volume)},"
                "aresample=48000,aformat=channel_layouts=stereo,"
                f"atempo={_number(segment.tempo_ratio)},"
                f"atrim=duration={_number(segment.duration_seconds)},"
                f"asetpts=PTS-STARTPTS[{label}]"
            )
            labels.append(f"[{label}]")
            part_index += 1
            cursor = segment.video_end_seconds

        if cursor < plan.video.duration_seconds:
            add_camera(max(0.0, cursor - fade), plan.video.duration_seconds)

        current_label = labels[0]
        if len(labels) > 1 and fade > 0:
            for index, next_label in enumerate(labels[1:], start=1):
                output_label = f"fade{index}"
                filters.append(f"{current_label}{next_label}acrossfade=d={_number(fade)}:c1=tri:c2=tri[{output_label}]")
                current_label = f"[{output_label}]"
        elif len(labels) > 1:
            filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[concatenated]")
            current_label = "[concatenated]"

        filters.append(f"{current_label}apad,atrim=duration={_number(plan.video.duration_seconds)}[aout]")
        return filters, "[aout]"


@dataclass(frozen=True, slots=True)
class RenderedOutput:
    """최종 파일과 선택적으로 수행된 음량 검증 결과."""

    output_path: Path
    audio_levels: AudioLevelReport | None = None


class FFmpegAudioAnalyzer:
    """FFmpeg EBU R128과 ffprobe를 이용한 렌더 전후 오디오 측정기."""

    def __init__(
        self,
        command_builder: FFmpegCommandBuilder | None = None,
        *,
        ffprobe_path: str = "ffprobe",
    ) -> None:
        self.command_builder = command_builder or FFmpegCommandBuilder()
        self.ffprobe_path = ffprobe_path

    @staticmethod
    def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, check=False)

    @staticmethod
    def _decoder_error(result: subprocess.CompletedProcess[str]) -> str | None:
        if result.returncode == 0:
            return None
        lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
        return lines[-1] if lines else f"FFmpeg exited with code {result.returncode}"

    @classmethod
    def _metrics(
        cls,
        result: subprocess.CompletedProcess[str],
        *,
        channels: int,
        sample_rate: int,
        duration_seconds: float,
        codec: str,
    ) -> AudioLevelMetrics:
        try:
            measured = parse_ebur128_summary(result.stderr)
        except ValueError as exc:
            diagnostic = cls._decoder_error(result) or str(exc)
            raise RenderError(f"Failed to measure audio levels: {diagnostic}") from exc
        return AudioLevelMetrics(
            channels=channels,
            sample_rate=sample_rate,
            integrated_loudness_lufs=measured.integrated_loudness_lufs,
            loudness_range_lu=measured.loudness_range_lu,
            sample_peak_dbfs=measured.sample_peak_dbfs,
            true_peak_dbtp=measured.true_peak_dbtp,
            duration_seconds=duration_seconds,
            codec=codec,
            decoder_error=cls._decoder_error(result),
        )

    def measure_render_input(
        self,
        plan: RenderPlan,
        manifest_paths: Path | Mapping[str, Path],
    ) -> AudioLevelMetrics:
        chunk = plan.session.chunks[0]
        result = self._run(self.command_builder.build_audio_analysis(plan, manifest_paths))
        return self._metrics(
            result,
            channels=self.command_builder.expected_output_channels(plan),
            sample_rate=chunk.sample_rate,
            duration_seconds=plan.video.duration_seconds,
            codec=chunk.codec,
        )

    def measure_output(self, output_path: Path) -> AudioLevelMetrics:
        probe = self._run(
            [
                self.ffprobe_path,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(output_path),
            ]
        )
        if probe.returncode != 0:
            diagnostic = self._decoder_error(probe) or "ffprobe failed"
            raise RenderError(f"Failed to probe rendered audio: {diagnostic}")
        try:
            payload = json.loads(probe.stdout)
            streams = payload["streams"]
            stream = next(item for item in streams if item.get("codec_type") == "audio")
            raw_format = payload["format"]
            channels = int(stream["channels"])
            sample_rate = int(stream["sample_rate"])
            raw_duration = stream.get("duration") or raw_format.get("duration")
            if raw_duration is None:
                raise KeyError("duration")
            duration_seconds = float(raw_duration)
            codec = str(stream["codec_name"])
        except (KeyError, TypeError, ValueError, StopIteration, json.JSONDecodeError) as exc:
            raise RenderError("Invalid ffprobe payload for rendered audio") from exc
        result = self._run(self.command_builder.build_output_audio_analysis(output_path))
        return self._metrics(
            result,
            channels=channels,
            sample_rate=sample_rate,
            duration_seconds=duration_seconds,
            codec=codec,
        )


class FFmpegRenderer:
    """임시 출력 후 원자적 교체와 libx265 폴백을 수행한다."""

    def __init__(
        self,
        command_builder: FFmpegCommandBuilder | None = None,
        audio_analyzer: FFmpegAudioAnalyzer | None = None,
    ) -> None:
        self.command_builder = command_builder or FFmpegCommandBuilder()
        self.audio_analyzer = audio_analyzer or FFmpegAudioAnalyzer(self.command_builder)

    @staticmethod
    def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, check=False)

    def render(self, plan: RenderPlan) -> Path:
        return self.render_with_report(plan).output_path

    def render_with_report(self, plan: RenderPlan) -> RenderedOutput:
        if plan.output_path.resolve() == plan.video.path.resolve():
            raise ValueError("Output path must not overwrite the source video")
        if plan.output_path.exists() and not plan.overwrite:
            raise FileExistsError(f"Output already exists: {plan.output_path}")
        plan.output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_output = plan.output_path.with_name(f".{plan.output_path.stem}.{uuid4().hex}.tmp{plan.output_path.suffix}")
        temp_plan = replace(plan, output_path=temp_output, overwrite=True)
        audio_levels: AudioLevelReport | None = None

        try:
            with tempfile.TemporaryDirectory(prefix="recordersync-") as temp_dir:
                session_by_id = {segment.session.id: segment.session for segment in plan.resolved_segments}
                manifest_paths: dict[str, Path] = {}
                for index, (session_id, session) in enumerate(session_by_id.items(), start=1):
                    manifest_path = Path(temp_dir) / f"audio-concat-{index}.txt"
                    manifest_path.write_text(build_concat_manifest(session), encoding="utf-8")
                    manifest_paths[session_id] = manifest_path

                effective_plan = temp_plan
                if plan.audio_level_policy is not None:
                    input_metrics = self.audio_analyzer.measure_render_input(plan, manifest_paths)
                    decision = decide_static_gain(input_metrics, plan.audio_level_policy)
                    audio_levels = AudioLevelReport(plan.audio_level_policy, input_metrics, decision)
                    if input_metrics.decoder_error is not None:
                        audio_levels = replace(
                            audio_levels,
                            validation_failures=(f"decoder error: {input_metrics.decoder_error}",),
                        )
                        raise AudioLevelRenderError(
                            "Input audio analysis failed",
                            audio_levels,
                        )
                    if decision.applied_gain_db is None:
                        raise AudioLevelRenderError(
                            "Loudness target conflicts with true-peak ceiling",
                            audio_levels,
                        )
                    effective_plan = replace(temp_plan, external_audio_gain_db=decision.applied_gain_db)

                hardware = self._run(self.command_builder.build(effective_plan, manifest_paths))
                if hardware.returncode != 0:
                    temp_output.unlink(missing_ok=True)
                    software = self._run(
                        self.command_builder.build(
                            effective_plan,
                            manifest_paths,
                            software_fallback=True,
                        )
                    )
                    if software.returncode != 0:
                        raise RenderError(
                            f"FFmpeg render failed with VideoToolbox and libx265: {software.stderr.strip()}"
                        )
                if not temp_output.is_file():
                    raise RenderError("FFmpeg reported success but produced no output file")

                if audio_levels is not None:
                    try:
                        output_metrics = self.audio_analyzer.measure_output(temp_output)
                    except RenderError as exc:
                        failed_report = replace(
                            audio_levels,
                            validation_failures=(f"decoder error: {exc}",),
                        )
                        raise AudioLevelRenderError(
                            "Final AAC validation failed",
                            failed_report,
                        ) from exc
                    policy = audio_levels.policy
                    failures = validate_output_metrics(
                        output_metrics,
                        policy,
                        expected_channels=self.command_builder.expected_output_channels(plan),
                        expected_duration_seconds=plan.video.duration_seconds,
                    )
                    audio_levels = replace(
                        audio_levels,
                        output_metrics=output_metrics,
                        validation_failures=failures,
                    )
                    if failures:
                        raise AudioLevelRenderError(
                            "Final AAC validation failed",
                            audio_levels,
                        )
            temp_output.replace(plan.output_path)
            return RenderedOutput(plan.output_path, audio_levels)
        finally:
            temp_output.unlink(missing_ok=True)
