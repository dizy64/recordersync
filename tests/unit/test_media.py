"""FFprobe/FFmpeg 경계와 미디어 탐색 단위 테스트."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import numpy as np
import pytest

from recordersync.media import FFmpegTools, MediaError, discover_audio_files, discover_video_files
from recordersync.models import AudioChunk, RecordingSession


def test_discover_media_files_filters_extensions_and_output(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    video_dir = tmp_path / "video"
    output_dir = video_dir / "replace"
    audio_dir.mkdir()
    video_dir.mkdir()
    output_dir.mkdir()
    (audio_dir / "REC_10.WAV").touch()
    (audio_dir / "REC_2.wav").touch()
    (audio_dir / "notes.txt").touch()
    (video_dir / "clip.mov").touch()
    (output_dir / "old.mp4").touch()

    assert [path.name for path in discover_audio_files(audio_dir)] == ["REC_2.wav", "REC_10.WAV"]
    assert discover_video_files(video_dir, excluded_dirs={output_dir}) == [video_dir / "clip.mov"]


def test_probe_audio_prefers_embedded_creation_time(tmp_path: Path) -> None:
    audio = tmp_path / "REC_001.WAV"
    audio.touch()
    payload = {
        "format": {
            "duration": "120.5",
            "tags": {"creation_time": "2026-07-17T01:02:03Z"},
        },
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "pcm_f32le",
                "sample_rate": "48000",
                "channels": 2,
            }
        ],
    }
    completed = CompletedProcess(["ffprobe"], 0, stdout=json.dumps(payload), stderr="")

    with patch("recordersync.media.subprocess.run", return_value=completed):
        chunk = FFmpegTools().probe_audio(audio)

    assert chunk.duration_seconds == pytest.approx(120.5)
    assert chunk.started_at == datetime(2026, 7, 17, 1, 2, 3, tzinfo=UTC)
    assert chunk.stream_signature == (48_000, 2, "pcm_f32le")


def test_probe_video_reads_dimensions_audio_and_color(tmp_path: Path) -> None:
    video = tmp_path / "clip.mov"
    video.touch()
    payload = {
        "format": {"duration": "10"},
        "streams": [
            {
                "codec_type": "video",
                "width": 1080,
                "height": 1920,
                "color_transfer": "arib-std-b67",
            },
            {"codec_type": "audio"},
        ],
    }
    completed = CompletedProcess(["ffprobe"], 0, stdout=json.dumps(payload), stderr="")

    with patch("recordersync.media.subprocess.run", return_value=completed):
        info = FFmpegTools().probe_video(video)

    assert info.is_portrait
    assert info.has_audio
    assert info.color_transfer == "arib-std-b67"


def test_extract_features_decodes_float_pcm() -> None:
    samples = np.linspace(-1, 1, 8_000, dtype=np.float32)
    completed = CompletedProcess(["ffmpeg"], 0, stdout=samples.tobytes(), stderr=b"")

    with (
        patch("recordersync.media.subprocess.run", return_value=completed) as run,
        patch(
            "recordersync.media.build_multiband_features", return_value=np.ones((6, 10))
        ) as build,
    ):
        result = FFmpegTools().extract_features(Path("clip.mov"))

    assert result.shape == (6, 10)
    assert "f32le" in run.call_args.args[0]
    assert build.call_args.kwargs["sample_rate"] == 8_000


def test_probe_failure_masks_command_and_raises_media_error() -> None:
    completed = CompletedProcess(["ffprobe"], 1, stdout="", stderr="invalid data")

    with (
        patch("recordersync.media.subprocess.run", return_value=completed),
        pytest.raises(MediaError, match="invalid data"),
    ):
        FFmpegTools().probe_audio(Path("broken.wav"))


def test_build_session_timeline_concatenates_chunk_features() -> None:
    tools = FFmpegTools()
    session = RecordingSession(
        "session-001",
        (
            AudioChunk(Path("a.wav"), 0.5, 48_000, 2, "pcm_f32le", None),
            AudioChunk(Path("b.wav"), 0.25, 48_000, 2, "pcm_f32le", None),
        ),
    )
    first = np.ones((6, 10), dtype=np.float32)
    second = np.full((6, 5), 2, dtype=np.float32)

    with patch.object(tools, "extract_features", side_effect=[first, second]):
        timeline = tools.build_session_timeline(session)

    assert timeline.features.shape == (6, 15)
    assert timeline.hop_seconds == pytest.approx(0.05)


def test_build_session_timeline_pads_each_chunk_to_preserve_logical_offsets() -> None:
    tools = FFmpegTools()
    session = RecordingSession(
        "session-001",
        (
            AudioChunk(Path("a.wav"), 0.5, 48_000, 2, "pcm_f32le", None),
            AudioChunk(Path("b.wav"), 0.5, 48_000, 2, "pcm_f32le", None),
        ),
    )
    short = np.ones((6, 9), dtype=np.float32)

    with patch.object(tools, "extract_features", side_effect=[short, short]):
        timeline = tools.build_session_timeline(session)

    assert timeline.features.shape == (6, 20)
