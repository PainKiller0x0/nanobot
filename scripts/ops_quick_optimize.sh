#!/usr/bin/env bash
set -euo pipefail

# Quick low-risk cleanup to reduce accidental memory/disk pressure.

APPLY=false
GIT_MAX_AGE_SEC=900
BACKUP_DIR="/root/.nanobot/workspace/wechat_rss_service"
KEEP_BACKUPS=3

usage() {
  cat <<'USAGE'
Usage:
  scripts/ops_quick_optimize.sh [options]

Options:
  --apply                 Perform changes (default is dry-run)
  --git-max-age-sec <n>   Kill stale /tmp/nanobot-ext-test git jobs older than n sec. Default: 900
  --keep-backups <n>      Keep latest app.py.bak_* files. Default: 3
  -h, --help              Show help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=true; shift ;;
    --git-max-age-sec) GIT_MAX_AGE_SEC="${2:-}"; shift 2 ;;
    --keep-backups) KEEP_BACKUPS="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

echo "[ops] mode: $($APPLY && echo apply || echo dry-run)"

echo "[ops] stale git jobs in /tmp/nanobot-ext-test (age>${GIT_MAX_AGE_SEC}s):"
mapfile -t stale_pids < <(ps -eo pid=,etimes=,cmd= | awk -v max="$GIT_MAX_AGE_SEC" '$2>max && $0 ~ /git/ && $0 ~ /nanobot-ext-test/ {print $1 "|" $0}')
if [[ ${#stale_pids[@]} -eq 0 ]]; then
  echo "  none"
else
  printf '  %s\n' "${stale_pids[@]}"
  if $APPLY; then
    for row in "${stale_pids[@]}"; do
      pid="${row%%|*}"
      kill "$pid" 2>/dev/null || true
    done
    echo "  killed"
  fi
fi

echo "[ops] backup files cleanup in $BACKUP_DIR (keep latest ${KEEP_BACKUPS}):"
if [[ -d "$BACKUP_DIR" ]]; then
  mapfile -t backups < <(ls -1t "$BACKUP_DIR"/app.py.bak_* 2>/dev/null || true)
  if [[ ${#backups[@]} -le $KEEP_BACKUPS ]]; then
    echo "  nothing to clean"
  else
    to_remove=("${backups[@]:$KEEP_BACKUPS}")
    printf '  remove candidates:\n'
    printf '    %s\n' "${to_remove[@]}"
    if $APPLY; then
      rm -f "${to_remove[@]}"
      echo "  removed"
    fi
  fi
else
  echo "  directory missing"
fi

echo "[ops] memory snapshot:"
free -h