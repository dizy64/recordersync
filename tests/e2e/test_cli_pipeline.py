"""합성 레코더 조각부터 최종 영상까지 CLI E2E 검증."""

from __future__ import annotations

import json
import subprocess
import sys
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from .conftest import (
    DriftSyntheticProject,
    PartialSyntheticProject,
    SyntheticProject,
    VfrSyntheticProject,
    probe_media,
    probe_video_timestamps,
)

pytestmark = pytest.mark.e2e


def _stream(payload: dict[str, Any], codec_type: str) -> dict[str, Any]:
    streams = payload.get("streams", [])
    if not isinstance(streams, list):
        raise TypeError("streams must be a list")
    return next(stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == codec_type)


def _audio_rms(path: Path, start_seconds: float) -> float:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(start_seconds),
            "-t",
            "1",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "f32le",
            "pipe:1",
        ],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    samples = np.frombuffer(result.stdout, dtype="<f4")
    return float(np.sqrt(np.mean(np.square(samples))))


def test_분석_CLI는_기본적으로_사람용_출력을_제공하고_JSON을_지원한다(
    synthetic_project: SyntheticProject,
) -> None:
    base_command = [
        sys.executable,
        "-m",
        "recordersync",
        "analyze",
        str(synthetic_project.video_dir),
        "--audio-dir",
        str(synthetic_project.audio_dir),
    ]

    human = subprocess.run(
        base_command,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )

    assert human.returncode == 0, human.stderr or human.stdout
    assert "분석 결과: 1/1개 매칭 (100.0%)" in human.stdout
    assert "- clip.mov | 매칭 여부: 성공 | 매칭률:" in human.stdout
    assert "추천: replace" in human.stdout
    assert "추천 실행:" in human.stdout
    assert "recordersync process" in human.stdout
    assert '"audio_sessions"' not in human.stdout

    machine = subprocess.run(
        [*base_command, "--json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )

    assert machine.returncode == 0, machine.stderr or machine.stdout
    payload = json.loads(machine.stdout)
    assert payload["summary"]["matched"] == 1
    assert payload["matches"][0]["video"] == str(synthetic_project.video_path)
    assert payload["matches"][0]["recommended_mode"] == "replace"
    assert payload["recommended_command"][:3] == [
        "recordersync",
        "process",
        str(synthetic_project.video_dir),
    ]


def test_처리_CLI는_분할_오디오를_매칭하고_원본_프로필로_렌더링한다(
    synthetic_project: SyntheticProject,
) -> None:
    analysis_report = synthetic_project.video_dir.parent / "analysis.json"
    analysis = subprocess.run(
        [
            sys.executable,
            "-m",
            "recordersync",
            "analyze",
            str(synthetic_project.video_dir),
            "--audio-dir",
            str(synthetic_project.audio_dir),
            "--report",
            str(analysis_report),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )

    assert analysis.returncode == 0, analysis.stderr or analysis.stdout
    assert analysis_report.is_file()
    assert f"--analysis-report {analysis_report}" in analysis.stdout
    assert "선택된 오디오 파일 (2개)" in analysis.stderr
    assert "[오디오 분석] 1/1 (100%)" in analysis.stderr
    assert "[영상 매칭] 1/1 (100%)" in analysis.stderr

    source_stat = synthetic_project.video_path.stat()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "recordersync",
            "process",
            str(synthetic_project.video_dir),
            "--analysis-report",
            str(analysis_report),
            "--output-dir",
            str(synthetic_project.output_dir),
            "--mode",
            "mix",
            "--camera-audio-volume",
            "0.2",
            "--external-audio-volume",
            "0.8",
            "--target-lufs",
            "-16",
            "--max-true-peak-dbtp",
            "-1",
            "--output-channel-layout",
            "stereo",
            "--loudness-tolerance-lu",
            "0.5",
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
        "partial": 0,
        "unmatched": 0,
        "ambiguous": 0,
        "error": 0,
    }
    assert len(report["audio_sessions"]) == 1
    assert len(report["audio_sessions"][0]["chunks"]) == 2
    match = report["matches"][0]
    assert match["external_start_seconds"] == pytest.approx(3.0, abs=0.15)
    assert match["output"] == str(output)
    assert match["audio_levels"]["policy"] == {
        "target_lufs": -16.0,
        "maximum_true_peak_dbtp": -1.0,
        "output_channel_layout": "stereo",
        "loudness_tolerance_lu": 0.5,
        "dynamics": "none",
    }
    assert match["audio_levels"]["input"]["codec"] == "float_mix"
    assert match["audio_levels"]["output"]["integrated_loudness_lufs"] == pytest.approx(-16.0, abs=0.5)
    assert match["audio_levels"]["output"]["true_peak_dbtp"] <= -1.0
    assert match["audio_levels"]["validation"] == {"passed": True, "failures": []}
    assert output.is_file()
    assert (synthetic_project.output_dir / "recordersync-report.json").is_file()
    assert f"분석 리포트 재사용: {analysis_report}" in result.stderr
    assert "선택된 오디오 파일" not in result.stderr
    assert "[오디오 분석]" not in result.stderr
    assert "[영상 매칭]" not in result.stderr
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


def test_처리_CLI는_VFR_프레임_타임스탬프를_보존한다(
    vfr_synthetic_project: VfrSyntheticProject,
) -> None:
    source_timestamps = probe_video_timestamps(vfr_synthetic_project.video_path)
    source_deltas = tuple(current - previous for previous, current in pairwise(source_timestamps))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "recordersync",
            "process",
            str(vfr_synthetic_project.video_dir),
            "--audio-dir",
            str(vfr_synthetic_project.audio_dir),
            "--output-dir",
            str(vfr_synthetic_project.output_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    output = vfr_synthetic_project.output_dir / "vfr.mp4"
    assert output.is_file()
    output_timestamps = probe_video_timestamps(output)
    assert len(source_timestamps) == 120
    assert len(output_timestamps) == len(source_timestamps)
    assert max(source_deltas) - min(source_deltas) > 0.02
    assert output_timestamps == pytest.approx(source_timestamps, abs=0.001)


def test_처리_CLI는_옵션_없는_기본_mix_정책을_실제_FFmpeg_경계에_적용한다(
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
            "--output-suffix",
            "_default_mix",
            "--mode",
            "mix",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    report = json.loads(result.stdout)
    match = report["matches"][0]
    output = synthetic_project.output_dir / "clip_default_mix.mp4"

    assert match["output"] == str(output)
    assert match["audio_levels"]["policy"] == {
        "target_lufs": -16.0,
        "maximum_true_peak_dbtp": -1.0,
        "output_channel_layout": "stereo",
        "loudness_tolerance_lu": 0.5,
        "dynamics": "none",
    }
    assert match["audio_levels"]["input"]["codec"] == "float_mix"
    assert match["audio_levels"]["validation"] == {"passed": True, "failures": []}
    assert output.is_file()

    audio = _stream(probe_media(output), "audio")
    assert audio["channels"] == 2
    assert audio["sample_rate"] == "48000"

    final_source_stat = synthetic_project.video_path.stat()
    assert final_source_stat.st_size == source_stat.st_size
    assert final_source_stat.st_mtime_ns == source_stat.st_mtime_ns


def test_처리_CLI는_자동_mix_dry_run에서_원본을_측정하고_추천만_보고한다(
    synthetic_project: SyntheticProject,
) -> None:
    expected_output = synthetic_project.output_dir / "clip_auto_recommend.mp4"
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
            "--output-suffix",
            "_auto_recommend",
            "--mode",
            "mix",
            "--mix-profile",
            "auto",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    match = json.loads(result.stdout)["matches"][0]
    recommendation = match["mix_recommendation"]
    assert match["output"] == str(expected_output)
    assert recommendation["status"] == "recommended"
    assert recommendation["camera"]["audio"]["codec"] == "float_analysis"
    assert recommendation["external"]["audio"]["codec"] == "float_analysis"
    assert recommendation["policy"]["camera_audio_volume"] == pytest.approx(1.0)
    assert recommendation["policy"]["external_gain_db"] <= 0
    assert recommendation["policy"]["external_highpass_hz"] in {80.0, 100.0}
    assert recommendation["policy"]["audio_level_policy"]["dynamics"] == "none"
    assert recommendation["failures"] == []
    assert "[믹스 분석] 1/1 (100%) clip.mov" in result.stderr
    assert "[영상 렌더]" not in result.stderr
    assert "[믹스 추천] clip.mov" in result.stderr
    assert not expected_output.exists()


def test_처리_CLI는_자동_mix_추천을_같은_렌더_경로에_적용하고_검증한다(
    synthetic_project: SyntheticProject,
) -> None:
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
            "--output-suffix",
            "_auto_mix",
            "--mode",
            "mix",
            "--mix-profile",
            "auto",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    match = json.loads(result.stdout)["matches"][0]
    output = synthetic_project.output_dir / "clip_auto_mix.mp4"
    assert match["output"] == str(output)
    assert match["mix_recommendation"]["status"] == "applied"
    assert match["audio_levels"]["input"]["codec"] == "float_mix"
    assert match["audio_levels"]["validation"] == {"passed": True, "failures": []}
    assert match["audio_levels"]["output"]["true_peak_dbtp"] <= -1.0
    assert "[믹스 추천] clip.mov | 상태: 적용" in result.stderr
    assert "[음량 검증] clip.mov | 음량 검증: 통과" in result.stderr
    assert output.is_file()


def test_처리_CLI는_static_gain을_적용하고_최종_AAC_음량을_재검증한다(
    synthetic_project: SyntheticProject,
) -> None:
    analysis_report = synthetic_project.video_dir.parent / "analysis-loudness.json"
    analysis = subprocess.run(
        [
            sys.executable,
            "-m",
            "recordersync",
            "analyze",
            str(synthetic_project.video_dir),
            "--audio-dir",
            str(synthetic_project.audio_dir),
            "--report",
            str(analysis_report),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert analysis.returncode == 0, analysis.stderr or analysis.stdout

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "recordersync",
            "process",
            str(synthetic_project.video_dir),
            "--analysis-report",
            str(analysis_report),
            "--output-dir",
            str(synthetic_project.output_dir),
            "--output-suffix",
            "_safe",
            "--target-lufs",
            "-24",
            "--max-true-peak-dbtp",
            "-1",
            "--output-channel-layout",
            "stereo",
            "--loudness-tolerance-lu",
            "0.5",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    report = json.loads(result.stdout)
    levels = report["matches"][0]["audio_levels"]
    output = synthetic_project.output_dir / "clip_safe.mp4"

    assert output.is_file()
    assert levels["policy"] == {
        "target_lufs": -24.0,
        "maximum_true_peak_dbtp": -1.0,
        "output_channel_layout": "stereo",
        "loudness_tolerance_lu": 0.5,
        "dynamics": "none",
    }
    assert levels["input"]["decoder_error"] is None
    assert levels["decision"]["applied_gain_db"] == pytest.approx(-24.0 - levels["input"]["integrated_loudness_lufs"])
    assert levels["output"]["integrated_loudness_lufs"] == pytest.approx(-24.0, abs=0.5)
    assert levels["output"]["true_peak_dbtp"] <= -1.0
    assert levels["output"]["channels"] == 2
    assert levels["output"]["sample_rate"] == 48_000
    assert levels["output"]["codec"] == "aac"
    assert levels["output"]["decoder_error"] is None
    assert levels["validation"] == {"passed": True, "failures": []}
    assert "[음량 검증] clip.mov | 음량 검증: 통과" in result.stderr


def test_폴백_처리는_다중_부분_구간만_레코더_오디오로_교체한다(
    partial_synthetic_project: PartialSyntheticProject,
) -> None:
    analysis = subprocess.run(
        [
            sys.executable,
            "-m",
            "recordersync",
            "analyze",
            str(partial_synthetic_project.video_dir),
            "--audio-dir",
            str(partial_synthetic_project.audio_dir),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )

    assert analysis.returncode == 2, analysis.stderr or analysis.stdout
    analysis_payload = json.loads(analysis.stdout)
    assert analysis_payload["matches"][0]["status"] == "partial"
    assert analysis_payload["matches"][0]["recommended_mode"] == "fallback"
    assert "--mode" in analysis_payload["recommended_command"]
    assert "fallback" in analysis_payload["recommended_command"]
    assert "--recommended-only" in analysis_payload["recommended_command"]

    source_stat = partial_synthetic_project.video_path.stat()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "recordersync",
            "process",
            str(partial_synthetic_project.video_dir),
            "--audio-dir",
            str(partial_synthetic_project.audio_dir),
            "--output-dir",
            str(partial_synthetic_project.output_dir),
            "--mode",
            "fallback",
            "--recommended-only",
            "--camera-audio-volume",
            "0.2",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    report = json.loads(result.stdout)
    match = report["matches"][0]
    output = partial_synthetic_project.output_dir / "partial.mp4"

    assert report["version"] == 3
    assert report["summary"]["partial"] == 1
    assert match["status"] == "partial"
    assert match["recommended_mode"] == "fallback"
    assert len(match["segments"]) >= 2
    assert 0.35 <= match["coverage_ratio"] <= 0.7
    assert output.is_file()
    assert _audio_rms(output, 7) > _audio_rms(output, 2) * 2
    assert _audio_rms(output, 19) > _audio_rms(output, 13) * 2

    media = probe_media(output)
    assert float(media["format"]["duration"]) == pytest.approx(22, abs=0.2)
    video = _stream(media, "video")
    assert video["width"] == 180
    assert video["height"] == 320
    assert Fraction(video["avg_frame_rate"]) == Fraction(24, 1)

    final_source_stat = partial_synthetic_project.video_path.stat()
    assert final_source_stat.st_size == source_stat.st_size
    assert final_source_stat.st_mtime_ns == source_stat.st_mtime_ns


def test_분석_CLI는_반복_끝_구간을_과도한_클럭_드리프트로_적용하지_않는다(
    drift_synthetic_project: DriftSyntheticProject,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "recordersync",
            "analyze",
            str(drift_synthetic_project.video_dir),
            "--audio-dir",
            str(drift_synthetic_project.audio_dir),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    match = payload["matches"][0]
    assert match["status"] == "matched"
    assert match["confidence"] > 0.8
    assert match["tempo_ratio"] == 1.0
