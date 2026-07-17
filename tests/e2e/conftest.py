"""실제 FFmpeg를 사용하는 공개 합성 E2E fixture."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

FFMPEG_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class SyntheticProject:
    video_dir: Path
    audio_dir: Path
    output_dir: Path
    video_path: Path


@dataclass(frozen=True, slots=True)
class PartialSyntheticProject:
    video_dir: Path
    audio_dir: Path
    output_dir: Path
    video_path: Path


def run_command(command: list[str], *, timeout: int = FFMPEG_TIMEOUT_SECONDS) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        pytest.fail(f"command failed ({result.returncode}): {' '.join(command)}\n{result.stderr}")
    return result.stdout


def probe_media(path: Path) -> dict[str, Any]:
    payload = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ]
    )
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise TypeError("ffprobe payload must be an object")
    return parsed


@pytest.fixture(scope="module")
def synthetic_project(tmp_path_factory: pytest.TempPathFactory) -> SyntheticProject:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")

    root = tmp_path_factory.mktemp("recordersync-e2e")
    video_dir = root / "video"
    audio_dir = root / "audio"
    output_dir = root / "output"
    video_dir.mkdir()
    audio_dir.mkdir()
    master_audio = root / "master.wav"

    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=color=pink:seed=42:duration=12:sample_rate=48000",
            "-c:a",
            "pcm_s16le",
            str(master_audio),
        ]
    )
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(master_audio),
            "-f",
            "segment",
            "-segment_time",
            "6",
            "-reset_timestamps",
            "1",
            "-c:a",
            "pcm_s16le",
            str(audio_dir / "REC_%03d.wav"),
        ]
    )

    video_path = video_dir / "clip.mov"
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "3",
            "-t",
            "4",
            "-i",
            str(master_audio),
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=180x320:rate=24:duration=4",
            "-map",
            "1:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(video_path),
        ]
    )
    return SyntheticProject(video_dir, audio_dir, output_dir, video_path)


@pytest.fixture(scope="module")
def partial_synthetic_project(
    tmp_path_factory: pytest.TempPathFactory,
) -> PartialSyntheticProject:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")

    root = tmp_path_factory.mktemp("recordersync-partial-e2e")
    video_dir = root / "video"
    audio_dir = root / "audio"
    output_dir = root / "output"
    parts_dir = root / "camera-parts"
    video_dir.mkdir()
    audio_dir.mkdir()
    parts_dir.mkdir()
    recorder = root / "recorder.wav"

    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=color=pink:seed=52:duration=24:sample_rate=48000",
            "-c:a",
            "pcm_s16le",
            str(recorder),
        ]
    )
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(recorder),
            "-f",
            "segment",
            "-segment_time",
            "12",
            "-reset_timestamps",
            "1",
            "-c:a",
            "pcm_s16le",
            str(audio_dir / "REC_%03d.wav"),
        ]
    )

    camera_parts = [
        ("fallback-head.wav", "anoisesrc=color=pink:seed=101:duration=5:sample_rate=48000"),
        ("fallback-middle.wav", "anoisesrc=color=pink:seed=102:duration=5:sample_rate=48000"),
    ]
    for filename, source in camera_parts:
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                source,
                "-c:a",
                "pcm_s16le",
                str(parts_dir / filename),
            ]
        )
    for filename, start in (("matched-first.wav", "3"), ("matched-second.wav", "15")):
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                start,
                "-t",
                "6",
                "-i",
                str(recorder),
                "-c:a",
                "pcm_s16le",
                str(parts_dir / filename),
            ]
        )

    manifest = root / "camera-concat.txt"
    ordered_parts = [
        parts_dir / "fallback-head.wav",
        parts_dir / "matched-first.wav",
        parts_dir / "fallback-middle.wav",
        parts_dir / "matched-second.wav",
    ]
    manifest.write_text(
        "".join(f"file '{path.resolve()}'\n" for path in ordered_parts),
        encoding="utf-8",
    )
    camera_audio = root / "camera.wav"
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest),
            "-c:a",
            "pcm_s16le",
            str(camera_audio),
        ]
    )

    video_path = video_dir / "partial.mov"
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(camera_audio),
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=180x320:rate=24:duration=22",
            "-map",
            "1:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(video_path),
        ]
    )
    return PartialSyntheticProject(video_dir, audio_dir, output_dir, video_path)
