#!/usr/bin/env bash

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
command -v ffmpeg >/dev/null || { echo "ffmpeg가 필요합니다." >&2; exit 1; }
command -v ffprobe >/dev/null || { echo "ffprobe가 필요합니다." >&2; exit 1; }
uv run pytest tests/e2e -v --tb=short --no-cov
