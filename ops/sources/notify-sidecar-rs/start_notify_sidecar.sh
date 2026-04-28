#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="/root/.nanobot/workspace/skills/notify-sidecar-rs"
LOG_FILE="${BASE_DIR}/notify_sidecar.log"
PID_FILE="${BASE_DIR}/notify_sidecar.pid"
PORT="${NOTIFY_SIDECAR_PORT:-8094}"
CONFIG_FILE="${NOTIFY_SIDECAR_CONFIG:-${BASE_DIR}/config.json}"
SERVICE_NAME="notify-sidecar-rs.service"

cd "$BASE_DIR"
mkdir -p "${BASE_DIR}/data"

cargo build --release --offline

if command -v systemctl >/dev/null 2>&1 && systemctl cat "$SERVICE_NAME" >/dev/null 2>&1; then
  systemctl restart "$SERVICE_NAME"
  echo "notify-sidecar-rs restarted via systemd (${SERVICE_NAME}) port=${PORT}"
  systemctl --no-pager --full status "$SERVICE_NAME" | sed -n '1,14p'
  exit 0
fi

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "Stopping existing notify-sidecar-rs pid=${old_pid}"
    kill "$old_pid" || true
    for _ in {1..20}; do
      if ! kill -0 "$old_pid" 2>/dev/null; then
        break
      fi
      sleep 0.2
    done
  fi
fi

NOTIFY_SIDECAR_PORT="$PORT" \
NOTIFY_SIDECAR_CONFIG="$CONFIG_FILE" \
nohup "$BASE_DIR/target/release/notify-sidecar-rs" >> "$LOG_FILE" 2>&1 &
new_pid=$!
echo "$new_pid" > "$PID_FILE"
echo "notify-sidecar-rs started pid=${new_pid} port=${PORT}"
