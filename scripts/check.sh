#!/usr/bin/env bash

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
uv run ruff check recordersync tests scripts
uv run ruff format --check recordersync tests scripts
uv run mypy recordersync scripts/check_pr_title.py
uv run pytest tests/unit -q
uv run pip-audit
uv run radon cc recordersync -a -nc
uv run radon mi recordersync -s
uv build
