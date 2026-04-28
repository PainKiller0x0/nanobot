#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

APT_MIRROR="${WECHAT_RSS_APT_MIRROR:-mirrors.tuna.tsinghua.edu.cn}"
IMAGE_TAG="${1:-wechat-rss-rs:local}"

echo "[build] image=$IMAGE_TAG"
echo "[build] apt_mirror=$APT_MIRROR"

docker build \
  -f Dockerfile.fast \
  --build-arg APT_MIRROR="$APT_MIRROR" \
  -t "$IMAGE_TAG" \
  .