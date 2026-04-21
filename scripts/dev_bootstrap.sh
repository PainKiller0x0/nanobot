#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
