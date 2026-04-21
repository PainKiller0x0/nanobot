#!/usr/bin/env bash
set -euo pipefail

# Migrate wechat-rss-sidecar to Rust implementation with guardrails.
# This script is intentionally conservative and refuses to apply when
# the Rust app appears to be a placeholder.

RS_REPO="/root/wechat-rss-rs"
IMAGE="wechat-rss-rs:local"
CONTAINER_NAME="wechat-rss-sidecar"
BACKUP_NAME="wechat-rss-sidecar-backup"
HOST_PORT="8091"
NANOBOT_HOME="${HOME}/.nanobot"
NANOBOT_REPO="/root/nanobot"
APPLY=false
FORCE=false

usage() {
  cat <<'USAGE'
Usage:
  scripts/apply_wechat_rss_rs.sh [options]

Options:
  --rs-repo <path>         Rust repo path. Default: /root/wechat-rss-rs
  --image <name>           Output image tag. Default: wechat-rss-rs:local
  --container <name>       Container name. Default: wechat-rss-sidecar
  --backup-name <name>     Backup container name. Default: wechat-rss-sidecar-backup
  --host-port <port>       Host port. Default: 8091
  --nanobot-home <path>    Host nanobot home mount. Default: ~/.nanobot
  --nanobot-repo <path>    Host nanobot repo mount. Default: /root/nanobot
  --force                  Skip placeholder check
  --apply                  Apply migration
  -h, --help               Show help

Examples:
  scripts/apply_wechat_rss_rs.sh
  scripts/apply_wechat_rss_rs.sh --apply
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rs-repo) RS_REPO="${2:-}"; shift 2 ;;
    --image) IMAGE="${2:-}"; shift 2 ;;
    --container) CONTAINER_NAME="${2:-}"; shift 2 ;;
    --backup-name) BACKUP_NAME="${2:-}"; shift 2 ;;
    --host-port) HOST_PORT="${2:-}"; shift 2 ;;
    --nanobot-home) NANOBOT_HOME="${2:-}"; shift 2 ;;
    --nanobot-repo) NANOBOT_REPO="${2:-}"; shift 2 ;;
    --force) FORCE=true; shift ;;
    --apply) APPLY=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ ! -d "$RS_REPO" ]]; then
  echo "Error: rs repo not found: $RS_REPO" >&2
  exit 1
fi
if [[ ! -f "$RS_REPO/Cargo.toml" ]]; then
  echo "Error: Cargo.toml not found in $RS_REPO" >&2
  exit 1
fi
if [[ ! -f "$RS_REPO/src/main.rs" ]]; then
  echo "Error: src/main.rs not found in $RS_REPO" >&2
  exit 1
fi

if ! $FORCE && grep -q 'Hello, world!' "$RS_REPO/src/main.rs"; then
  echo "Error: detected placeholder Rust app (Hello, world). Refusing to migrate." >&2
  echo "Hint: implement real HTTP service first, or rerun with --force for testing." >&2
  exit 1
fi

echo "[wechat-rs] building image: $IMAGE"
docker build -t "$IMAGE" -f - "$RS_REPO" <<'DOCKERFILE'
FROM rust:1.90-bookworm AS build
WORKDIR /src
COPY . .
RUN cargo build --release

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/wechat-rss-rs /usr/local/bin/wechat-rss-rs
ENV WECHAT_RSS_HOST=0.0.0.0
ENV WECHAT_RSS_PORT=8091
EXPOSE 8091
CMD ["wechat-rss-rs"]
DOCKERFILE

if ! $APPLY; then
  echo "[wechat-rs] dry-run complete (image built only)."
  echo "[wechat-rs] run with --apply to switch container."
  exit 0
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$BACKUP_NAME"; then
  docker rm -f "$BACKUP_NAME" >/dev/null 2>&1 || true
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
  echo "[wechat-rs] backing up existing container -> $BACKUP_NAME"
  docker stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
  docker rename "$CONTAINER_NAME" "$BACKUP_NAME"
fi

echo "[wechat-rs] starting new rust container"
docker run -d \
  --name "$CONTAINER_NAME" \
  --restart always \
  -p "${HOST_PORT}:8091" \
  -v "${NANOBOT_HOME}:/root/.nanobot" \
  -v "${NANOBOT_REPO}:/root/nanobot:ro" \
  "$IMAGE" >/dev/null

sleep 2
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
  echo "[wechat-rs] new container failed to stay up, rolling back" >&2
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  if docker ps -a --format '{{.Names}}' | grep -qx "$BACKUP_NAME"; then
    docker rename "$BACKUP_NAME" "$CONTAINER_NAME"
    docker start "$CONTAINER_NAME" >/dev/null 2>&1 || true
  fi
  exit 1
fi

echo "[wechat-rs] migration applied"