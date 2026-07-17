"""오디오 특징 생성과 FFT 매칭 정책."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from recordersync.matching import (
    FeatureTimeline,
    MatchOptions,
    build_multiband_features,
    match_video_features,
    refine_feature_alignment,
)
from recordersync.models import MatchStatus


def _standardize(features: np.ndarray) -> np.ndarray:
    mean = features.mean(axis=1, keepdims=True)
    std = features.std(axis=1, keepdims=True)
    return (features - mean) / std


def test_build_multiband_features_returns_finite_normalized_bands() -> None:
    sample_rate = 8_000
    seconds = 3
    time = np.arange(sample_rate * seconds) / sample_rate
    samples = (np.sin(2 * np.pi * 220 * time) + 0.5 * np.sin(2 * np.pi * 1_200 * time)).astype(
        np.float32
    )

    features = build_multiband_features(samples, sample_rate=sample_rate)

    assert features.shape[0] == 6
    assert features.shape[1] > 10
    assert np.isfinite(features).all()
    assert features.mean(axis=1) == pytest.approx(np.zeros(6), abs=1e-5)


def test_match_video_features_finds_inserted_region() -> None:
    rng = np.random.default_rng(7)
    video = _standardize(rng.normal(size=(6, 120)).astype(np.float32))
    session = rng.normal(scale=0.05, size=(6, 1_000)).astype(np.float32)
    session[:, 420:540] += video

    result = match_video_features(
        Path("clip.mov"),
        duration_seconds=6.0,
        video_features=video,
        sessions=[FeatureTimeline("session-001", _standardize(session), 0.05)],
        options=MatchOptions(min_confidence=0.7, min_peak_margin=0.05),
    )

    assert result.status is MatchStatus.MATCHED
    assert result.session_id == "session-001"
    assert result.external_start_seconds == pytest.approx(21.0, abs=0.05)
    assert result.peak_margin >= 0.05


def test_match_video_features_marks_repeated_pattern_ambiguous() -> None:
    rng = np.random.default_rng(9)
    video = _standardize(rng.normal(size=(6, 100)).astype(np.float32))
    session = rng.normal(scale=0.02, size=(6, 700)).astype(np.float32)
    session[:, 100:200] += video
    session[:, 450:550] += video

    result = match_video_features(
        Path("clip.mov"),
        duration_seconds=5.0,
        video_features=video,
        sessions=[FeatureTimeline("session-001", _standardize(session), 0.05)],
        options=MatchOptions(min_confidence=0.7, min_peak_margin=0.05),
    )

    assert result.status is MatchStatus.AMBIGUOUS
    assert result.peak_margin < 0.05


def test_match_video_features_detects_overlapping_repeated_pattern_as_ambiguous() -> None:
    rng = np.random.default_rng(10)
    repeating_unit = rng.normal(size=(6, 20)).astype(np.float32)
    video = _standardize(np.tile(repeating_unit, (1, 5)))
    session = rng.normal(scale=0.01, size=(6, 500)).astype(np.float32)
    session[:, 100:260] += np.tile(repeating_unit, (1, 8))

    result = match_video_features(
        Path("clip.mov"),
        duration_seconds=5.0,
        video_features=video,
        sessions=[FeatureTimeline("session-001", _standardize(session), 0.05)],
        options=MatchOptions(min_confidence=0.7, min_peak_margin=0.05),
    )

    assert result.status is MatchStatus.AMBIGUOUS


def test_match_video_features_marks_unrelated_audio_unmatched() -> None:
    rng = np.random.default_rng(11)
    video = _standardize(rng.normal(size=(6, 100)).astype(np.float32))
    session = _standardize(rng.normal(size=(6, 700)).astype(np.float32))

    result = match_video_features(
        Path("clip.mov"),
        duration_seconds=5.0,
        video_features=video,
        sessions=[FeatureTimeline("session-001", session, 0.05)],
        options=MatchOptions(min_confidence=0.75, min_peak_margin=0.05),
    )

    assert result.status is MatchStatus.UNMATCHED
    assert result.session_id is None


def test_match_video_features_rejects_short_session() -> None:
    features = np.ones((6, 100), dtype=np.float32)

    result = match_video_features(
        Path("clip.mov"),
        duration_seconds=5.0,
        video_features=features,
        sessions=[FeatureTimeline("session-001", features[:, :50], 0.05)],
    )

    assert result.status is MatchStatus.UNMATCHED
    assert "shorter" in (result.reason or "")


def test_match_video_features_allows_same_region_for_multiple_videos() -> None:
    rng = np.random.default_rng(13)
    video = _standardize(rng.normal(size=(6, 80)).astype(np.float32))
    session = rng.normal(scale=0.02, size=(6, 500)).astype(np.float32)
    session[:, 200:280] += video
    timeline = FeatureTimeline("session-001", _standardize(session), 0.05)
    options = MatchOptions(min_confidence=0.7, min_peak_margin=0.05)

    first = match_video_features(Path("cam-a.mov"), 4.0, video, [timeline], options)
    second = match_video_features(Path("cam-b.mov"), 4.0, video, [timeline], options)

    assert first.status is MatchStatus.MATCHED
    assert second.status is MatchStatus.MATCHED
    assert first.external_start_seconds == second.external_start_seconds


def test_refine_feature_alignment_estimates_clock_drift() -> None:
    rng = np.random.default_rng(17)
    video = _standardize(rng.normal(size=(6, 2_000)).astype(np.float32))
    session = rng.normal(scale=0.01, size=(6, 3_000)).astype(np.float32)
    head_frames = 400
    session[:, 200 : 200 + head_frames] += video[:, :head_frames]
    tail_reference_start = video.shape[1] - head_frames
    tail_session_start = 200 + tail_reference_start + 4
    session[:, tail_session_start : tail_session_start + head_frames] += video[:, -head_frames:]

    start_frame, tempo_ratio = refine_feature_alignment(
        _standardize(session),
        video,
        coarse_start_frame=200,
        hop_seconds=0.05,
        window_seconds=20.0,
        search_seconds=1.0,
    )

    assert start_frame == 200
    assert tempo_ratio == pytest.approx((1_600 + 4) / 1_600, rel=1e-4)
