"""12시간·영상 200개 목표의 순수 매칭 성능 벤치마크."""

from __future__ import annotations

import argparse
import resource
import time
from pathlib import Path

import numpy as np

from recordersync.matching import FeatureTimeline, MatchOptions, match_video_features
from recordersync.models import MatchStatus


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=12.0)
    parser.add_argument("--videos", type=int, default=200)
    parser.add_argument("--clip-seconds", type=float, default=60.0)
    parser.add_argument("--max-seconds", type=float, default=600.0)
    parser.add_argument("--max-rss-mb", type=float, default=2_048.0)
    parser.add_argument(
        "--partial",
        action="store_true",
        help="전체 매칭이 실패하는 중간 단절 구간의 전역 재탐색 비용을 측정합니다.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.hours <= 0 or args.videos <= 0 or args.clip_seconds <= 0:
        raise ValueError("hours, videos, and clip-seconds must be > 0")

    hop_seconds = 0.05
    frame_count = round(args.hours * 3_600 / hop_seconds)
    clip_frames = round(args.clip_seconds / hop_seconds)
    if clip_frames >= frame_count:
        raise ValueError("clip must be shorter than the recording timeline")
    if args.partial and clip_frames * 3 >= frame_count:
        raise ValueError("partial benchmark requires a timeline at least 3x the clip length")

    rng = np.random.default_rng(20260717)
    session_features = rng.normal(size=(6, frame_count)).astype(np.float32)
    timeline = FeatureTimeline("session-001", session_features, hop_seconds)
    options = MatchOptions(min_confidence=0.7, enable_partial=args.partial)
    timings: list[float] = []

    started = time.perf_counter()
    for index in range(args.videos):
        if args.partial:
            segment_frames = clip_frames * 2 // 5
            gap_frames = clip_frames - segment_frames * 2
            start = (index * (frame_count - clip_frames * 3)) // args.videos
            second_start = start + clip_frames * 2
            video_features = np.concatenate(
                (
                    session_features[:, start : start + segment_frames],
                    rng.normal(size=(6, gap_frames)).astype(np.float32),
                    session_features[:, second_start : second_start + segment_frames],
                ),
                axis=1,
            )
        else:
            start = (index * (frame_count - clip_frames)) // args.videos
            video_features = session_features[:, start : start + clip_frames].copy()
        match_started = time.perf_counter()
        result = match_video_features(
            Path(f"clip-{index:04d}.mov"),
            args.clip_seconds,
            video_features,
            [timeline],
            options,
        )
        timings.append(time.perf_counter() - match_started)
        if result.external_start_seconds is None or (
            args.partial and result.status is not MatchStatus.PARTIAL
        ):
            raise RuntimeError(f"Synthetic clip {index} did not match")

    elapsed = time.perf_counter() - started
    p95 = float(np.percentile(np.asarray(timings), 95))
    p99 = float(np.percentile(np.asarray(timings), 99))
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
    print(
        f"timeline_hours={args.hours:g} videos={args.videos} partial={str(args.partial).lower()} "
        f"elapsed_seconds={elapsed:.3f} per_video_p95_seconds={p95:.3f} "
        f"per_video_p99_seconds={p99:.3f} "
        f"peak_rss_mb={peak_rss:.1f}"
    )
    return 0 if elapsed <= args.max_seconds and peak_rss <= args.max_rss_mb else 1


if __name__ == "__main__":
    raise SystemExit(main())
