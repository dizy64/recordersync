"""TubeArchive 호환 FFmpeg 렌더 계획과 실행기."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from recordersync.media import VideoInfo
from recordersync.models import RecordingSession


class RenderError(RuntimeError):
    """하드웨어와 소프트웨어 렌더가 모두 실패한 경우."""


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

    return (
        "\n".join(f"file '{_escape_concat_path(chunk.path.resolve())}'" for chunk in session.chunks)
        + "\n"
    )


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


def _number(value: float) -> str:
    return f"{value:.9f}".rstrip("0").rstrip(".") or "0"


def _video_filter(video: VideoInfo) -> list[str]:
    if video.color_transfer in {"arib-std-b67", "smpte2084"}:
        return [
            (
                "[0:v:0]colorspace=all=bt709:iall=bt2020:dither=fsb,"
                "format=yuv420p10le,format=p010le[vout]"
            ),
        ]
    return ["[0:v:0]format=p010le[vout]"]


class FFmpegCommandBuilder:
    """쉘을 사용하지 않는 FFmpeg 인자 목록 생성기."""

    def __init__(self, ffmpeg_path: str = "ffmpeg") -> None:
        self.ffmpeg_path = ffmpeg_path

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
            tempo = _number(plan.tempo_ratio)
            filters.append(
                f"[1:a:0]volume={_number(plan.external_audio_volume)},atempo={tempo},"
                f"apad,atrim=duration={duration},asetpts=PTS-STARTPTS[external]"
            )
            audio_label = "[external]"
            if plan.mode is RenderMode.MIX:
                filters.extend(
                    [
                        (
                            f"[0:a:0]volume={_number(plan.camera_audio_volume)},"
                            f"aresample=48000,apad,atrim=duration={duration},"
                            "asetpts=PTS-STARTPTS[camera]"
                        ),
                        (
                            "[camera][external]amix=inputs=2:duration=first:"
                            "dropout_transition=0:weights=1 1[aout]"
                        ),
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
                filters.append(
                    f"{current_label}{next_label}acrossfade=d={_number(fade)}:"
                    f"c1=tri:c2=tri[{output_label}]"
                )
                current_label = f"[{output_label}]"
        elif len(labels) > 1:
            filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[concatenated]")
            current_label = "[concatenated]"

        filters.append(
            f"{current_label}apad,atrim=duration={_number(plan.video.duration_seconds)}[aout]"
        )
        return filters, "[aout]"


class FFmpegRenderer:
    """임시 출력 후 원자적 교체와 libx265 폴백을 수행한다."""

    def __init__(
        self,
        command_builder: FFmpegCommandBuilder | None = None,
    ) -> None:
        self.command_builder = command_builder or FFmpegCommandBuilder()

    @staticmethod
    def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, check=False)

    def render(self, plan: RenderPlan) -> Path:
        if plan.output_path.resolve() == plan.video.path.resolve():
            raise ValueError("Output path must not overwrite the source video")
        if plan.output_path.exists() and not plan.overwrite:
            raise FileExistsError(f"Output already exists: {plan.output_path}")
        plan.output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_output = plan.output_path.with_name(
            f".{plan.output_path.stem}.{uuid4().hex}.tmp{plan.output_path.suffix}"
        )
        temp_plan = replace(plan, output_path=temp_output, overwrite=True)

        try:
            with tempfile.TemporaryDirectory(prefix="recordersync-") as temp_dir:
                session_by_id = {
                    segment.session.id: segment.session for segment in plan.resolved_segments
                }
                manifest_paths: dict[str, Path] = {}
                for index, (session_id, session) in enumerate(session_by_id.items(), start=1):
                    manifest_path = Path(temp_dir) / f"audio-concat-{index}.txt"
                    manifest_path.write_text(build_concat_manifest(session), encoding="utf-8")
                    manifest_paths[session_id] = manifest_path
                hardware = self._run(self.command_builder.build(temp_plan, manifest_paths))
                if hardware.returncode != 0:
                    temp_output.unlink(missing_ok=True)
                    software = self._run(
                        self.command_builder.build(
                            temp_plan,
                            manifest_paths,
                            software_fallback=True,
                        )
                    )
                    if software.returncode != 0:
                        raise RenderError(
                            "FFmpeg render failed with VideoToolbox and libx265: "
                            f"{software.stderr.strip()}"
                        )
            if not temp_output.is_file():
                raise RenderError("FFmpeg reported success but produced no output file")
            temp_output.replace(plan.output_path)
            return plan.output_path
        finally:
            temp_output.unlink(missing_ok=True)
