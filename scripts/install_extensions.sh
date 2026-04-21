#!/usr/bin/env bash
set -euo pipefail

# Spelling-compatible wrapper.
exec "$(dirname "$0")/install_extentions.sh" "$@"
