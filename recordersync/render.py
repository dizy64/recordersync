"""TubeArchive 호환 FFmpeg 렌더 계획과 실행기."""

from __future__ import annotations

import subprocess
import tempfile
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
    overwrite: bool = False

    def __post_init__(self) -> None:
        if self.external_start_seconds < 0:
            raise ValueError("external_start_seconds must be >= 0")
        if not 0.5 <= self.tempo_ratio <= 2.0:
            raise ValueError("tempo_ratio must be in [0.5, 2.0]")
        if not 0 <= self.camera_audio_volume <= 1:
            raise ValueError("camera_audio_volume must be in [0, 1]")
        if self.mode is RenderMode.MIX and not self.video.has_audio:
            raise ValueError("mix mode requires camera audio")


def _escape_concat_path(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def build_concat_manifest(session: RecordingSession) -> str:
    """FFmpeg concat demuxer용 안전한 파일 목록 문자열."""

    return (
        "\n".join(f"file '{_escape_concat_path(chunk.path.resolve())}'" for chunk in session.chunks)
        + "\n"
    )


def resolve_output_path(video_path: Path, output_dir: Path) -> Path:
    return output_dir / f"{video_path.stem}_replaced.mp4"


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
        manifest_path: Path,
        *,
        software_fallback: bool = False,
    ) -> list[str]:
        duration = _number(plan.video.duration_seconds)
        tempo = _number(plan.tempo_ratio)
        filters = _video_filter(plan.video)
        filters.append(
            f"[1:a:0]atempo={tempo},apad,atrim=duration={duration},asetpts=PTS-STARTPTS[external]"
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
            "-ss",
            _number(plan.external_start_seconds),
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest_path),
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
            "-r",
            "30000/1001",
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
        if software_fallback:
            command.extend(["-preset", "medium"])
        command.extend(["-movflags", "+faststart", str(plan.output_path)])
        return command


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
        if plan.output_path.exists() and not plan.overwrite:
            raise FileExistsError(f"Output already exists: {plan.output_path}")
        plan.output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_output = plan.output_path.with_name(
            f".{plan.output_path.stem}.{uuid4().hex}.tmp{plan.output_path.suffix}"
        )
        temp_plan = replace(plan, output_path=temp_output, overwrite=True)

        try:
            with tempfile.TemporaryDirectory(prefix="recordersync-") as temp_dir:
                manifest_path = Path(temp_dir) / "audio-concat.txt"
                manifest_path.write_text(build_concat_manifest(plan.session), encoding="utf-8")
                hardware = self._run(self.command_builder.build(temp_plan, manifest_path))
                if hardware.returncode != 0:
                    temp_output.unlink(missing_ok=True)
                    software = self._run(
                        self.command_builder.build(
                            temp_plan,
                            manifest_path,
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
