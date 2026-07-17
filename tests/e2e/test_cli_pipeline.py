"""합성 레코더 조각부터 최종 영상까지 CLI E2E 검증."""

from __future__ import annotations

import json
import subprocess
import sys
from fractions import Fraction
from typing import Any

import pytest

from .conftest import SyntheticProject, probe_media

pytestmark = pytest.mark.e2e


def _stream(payload: dict[str, Any], codec_type: str) -> dict[str, Any]:
    streams = payload.get("streams", [])
    if not isinstance(streams, list):
        raise TypeError("streams must be a list")
    return next(
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == codec_type
    )


def test_process_cli_matches_split_audio_and_renders_source_profile(
    synthetic_project: SyntheticProject,
) -> None:
    source_stat = synthetic_project.video_path.stat()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "recordersync",
            "process",
            str(synthetic_project.video_dir),
            "--audio-dir",
            str(synthetic_project.audio_dir),
            "--output-dir",
            str(synthetic_project.output_dir),
            "--mode",
            "mix",
            "--camera-audio-volume",
            "0.2",
            "--external-audio-volume",
            "0.8",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    report = json.loads(result.stdout)
    output = synthetic_project.output_dir / "clip.mp4"

    assert report["language"] == "ko"
    assert report["summary"] == {
        "total": 1,
        "matched": 1,
        "unmatched": 0,
        "ambiguous": 0,
        "error": 0,
    }
    assert len(report["audio_sessions"]) == 1
    assert len(report["audio_sessions"][0]["chunks"]) == 2
    assert report["matches"][0]["external_start_seconds"] == pytest.approx(3.0, abs=0.15)
    assert report["matches"][0]["output"] == str(output)
    assert output.is_file()
    assert (synthetic_project.output_dir / "recordersync-report.json").is_file()
    assert "선택된 오디오 파일 (2개)" in result.stderr
    assert "REC_000.wav" in result.stderr
    assert "REC_001.wav" in result.stderr
    assert "선택된 영상 파일 (1개)" in result.stderr
    assert "clip.mov" in result.stderr
    assert "[오디오 분석] 1/1 (100%)" in result.stderr
    assert "[영상 매칭] 1/1 (100%)" in result.stderr
    assert "[영상 렌더] 1/1 (100%)" in result.stderr

    media = probe_media(output)
    video = _stream(media, "video")
    audio = _stream(media, "audio")
    assert video["codec_name"] == "hevc"
    assert video["width"] == 180
    assert video["height"] == 320
    assert Fraction(video["avg_frame_rate"]) == Fraction(24, 1)
    assert "10" in video["pix_fmt"] or "p010" in video["pix_fmt"]
    assert audio["codec_name"] == "aac"
    assert audio["sample_rate"] == "48000"

    final_source_stat = synthetic_project.video_path.stat()
    assert final_source_stat.st_size == source_stat.st_size
    assert final_source_stat.st_mtime_ns == source_stat.st_mtime_ns
