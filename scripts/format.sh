#!/usr/bin/env bash

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
uv run ruff check --fix recordersync tests scripts
uv run ruff format recordersync tests scripts
