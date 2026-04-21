#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m ruff check nanobot --select F401,F841
python3 -m pytest -q tests/channels/test_channel_manager_delta_coalescing.py tests/channels/test_channel_plugins.py
