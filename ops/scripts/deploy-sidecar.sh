#!/usr/bin/env bash
set -euo pipefail

REPO="${NANOBOT_OPS_REPO:-/root/nanobot-ops}"
BIN_DIR="${SIDECAR_BIN_DIR:-/usr/local/bin}"
APT_MIRROR="${WECHAT_RSS_APT_MIRROR:-mirrors.tuna.tsinghua.edu.cn}"
DRY_RUN=0
SKIP_BUILD=0
SKIP_RESTART=0
STATUS_ONLY=0

declare -a TARGETS=()

usage() {
  cat <<'USAGE'
Usage: deploy-sidecar [options] <target...>

Targets:
  lof        Build/install/restart LOF dashboard sidecar
  notify     Build/install/restart Notify cron bridge
  qq         Build/install/restart QQ bridge
  reflexio   Build/install/restart Reflexio dashboard
  obp        Build/install/restart OBP bridge
  trend     Build/install/restart Trend Radar sidecar
  rss        Build/restart RSS container sidecar with Podman
  all        Deploy all targets above

Options:
  --status       Only show service + health status
  --no-build     Install/restart using existing release artifact or image
  --no-restart   Build/install only, do not restart service
  --dry-run      Print commands without running them
  -h, --help     Show this help

Examples:
  deploy-sidecar lof
  deploy-sidecar notify qq
  deploy-sidecar rss --no-build
  deploy-sidecar all --status
USAGE
}

log() { printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"; }
die() { echo "error: $*" >&2; exit 1; }
run() {
  echo "+ $*"
  if [[ "$DRY_RUN" -eq 0 ]]; then
    "$@"
  fi
}

rust_source() {
  case "$1" in
    lof) echo "$REPO/sources/lof-sidecar-rs|lof-sidecar-rs|lof-sidecar.service|http://127.0.0.1:8093/health" ;;
    notify) echo "$REPO/sources/notify-sidecar-rs|notify-sidecar-rs|notify-sidecar-rs.service|http://127.0.0.1:8094/health" ;;
    qq) echo "$REPO/sources/qq-sidecar-rs|qq-sidecar-rs|qq-sidecar-rs.service|http://172.17.0.1:8092/health" ;;
    reflexio) echo "$REPO/sources/nanobot-reflexio-rs|nanobot-reflexio-rs|nanobot-reflexio-rs.service|http://127.0.0.1:8081/health" ;;
    obp) echo "$REPO/sources/obp-rs|obp-rs|obp-rs.service|http://127.0.0.1:8000/" ;;
    trend) echo "$REPO/sources/trend-sidecar-rs|trend-sidecar-rs|trend-sidecar-rs.service|http://127.0.0.1:8095/health" ;;
    *) return 1 ;;
  esac
}

service_status() {
  local unit=$1 health=${2:-}
  local active="unknown"
  active=$(systemctl is-active "$unit" 2>/dev/null || true)
  printf '%-34s service=%s' "$unit" "${active:-unknown}"
  if [[ -n "$health" ]]; then
    local code latency
    code=$(curl -o /dev/null -sS -m 5 -w '%{http_code}' "$health" 2>/dev/null || true)
    latency=$(curl -o /dev/null -sS -m 5 -w '%{time_total}' "$health" 2>/dev/null || true)
    printf ' health=%s latency=%ss' "${code:-000}" "${latency:-?}"
  fi
  printf '\n'
}

check_health() {
  local unit=$1 health=${2:-}
  if [[ "$SKIP_RESTART" -eq 1 || "$DRY_RUN" -eq 1 ]]; then
    service_status "$unit" "$health"
    return 0
  fi
  sleep 1
  systemctl is-active --quiet "$unit"
  if [[ -n "$health" ]]; then
    curl -fsS -m 8 "$health" >/dev/null
  fi
  service_status "$unit" "$health"
}

deploy_rust() {
  local id=$1 row src bin unit health artifact
  row=$(rust_source "$id") || die "unknown rust target: $id"
  IFS='|' read -r src bin unit health <<< "$row"
  [[ -f "$src/Cargo.toml" ]] || die "missing Cargo.toml: $src"
  artifact="$src/target/release/$bin"

  log "deploy rust sidecar: $id"
  if [[ "$STATUS_ONLY" -eq 1 ]]; then
    service_status "$unit" "$health"
    return 0
  fi
  if [[ "$SKIP_BUILD" -eq 0 ]]; then
    run cargo build --release --manifest-path "$src/Cargo.toml"
  fi
  [[ -x "$artifact" || "$DRY_RUN" -eq 1 ]] || die "missing release binary: $artifact"
  run install -m 0755 "$artifact" "$BIN_DIR/$bin"
  run systemctl daemon-reload
  if [[ "$SKIP_RESTART" -eq 0 ]]; then
    run systemctl restart "$unit"
  fi
  check_health "$unit" "$health"
}

deploy_rss() {
  local src="$REPO/sources/wechat-rss-rs"
  local unit="podman-wechat-rss-sidecar.service"
  local health="http://127.0.0.1:8091/"
  [[ -f "$src/Dockerfile.fast" ]] || die "missing Dockerfile.fast: $src"

  log "deploy rss sidecar"
  if [[ "$STATUS_ONLY" -eq 1 ]]; then
    service_status "$unit" "$health"
    return 0
  fi
  if [[ "$SKIP_BUILD" -eq 0 ]]; then
    local base_image="${WECHAT_RSS_BASE_IMAGE:-localhost/wechat-rss-rs:local}"
    if podman image exists "$base_image"; then
      log "rss local refresh: rebuild host binary and reuse base image $base_image"
      run cargo build --release --manifest-path "$src/Cargo.toml"
      cat > "$src/Dockerfile.local-refresh" <<EOF
FROM $base_image
COPY target/release/wechat-rss-rs /usr/local/bin/wechat-rss-rs
RUN chmod +x /usr/local/bin/wechat-rss-rs
EOF
      run podman build --pull=never -f "$src/Dockerfile.local-refresh" -t localhost/wechat-rss-rs:local "$src"
      run rm -f "$src/Dockerfile.local-refresh"
    else
      base_image="${WECHAT_RSS_BASE_IMAGE:-docker.m.daocloud.io/library/debian:bookworm-slim}"
      log "rss full rebuild: base image $base_image"
      run podman build -f "$src/Dockerfile.fast" --build-arg "BASE_IMAGE=$base_image" --build-arg "APT_MIRROR=$APT_MIRROR" -t localhost/wechat-rss-rs:local "$src"
    fi
  fi
  run systemctl daemon-reload
  if [[ "$SKIP_RESTART" -eq 0 ]]; then
    run systemctl restart "$unit"
  fi
  check_health "$unit" "$health"
}

deploy_one() {
  case "$1" in
    lof|notify|qq|reflexio|obp|trend) deploy_rust "$1" ;;
    rss) deploy_rss ;;
    *) die "unknown target: $1" ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --status) STATUS_ONLY=1 ;;
    --no-build) SKIP_BUILD=1 ;;
    --no-restart) SKIP_RESTART=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    all) TARGETS=(lof notify qq reflexio obp trend rss) ;;
    lof|notify|qq|reflexio|obp|trend|rss) TARGETS+=("$1") ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

[[ ${#TARGETS[@]} -gt 0 ]] || { usage >&2; exit 2; }

for target in "${TARGETS[@]}"; do
  deploy_one "$target"
done

log "done"
