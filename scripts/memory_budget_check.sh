#!/usr/bin/env bash
set -euo pipefail

# Check whether runtime memory stays under a target budget.

THRESHOLD_MIB=500
DOCKER_SERVICES="nanobot-cage wechat-rss-sidecar"
EXTRA_PATTERNS="nanobot-reflexio-rs|qq-sidecar-rs"

usage() {
  cat <<'USAGE'
Usage:
  scripts/memory_budget_check.sh [options]

Options:
  --threshold-mib <num>   Budget threshold in MiB. Default: 500
  --docker <list>         Space-separated docker services. Default: "nanobot-cage wechat-rss-sidecar"
  --extra-patterns <re>   Regex for host processes (RSS added). Default: "nanobot-reflexio-rs|qq-sidecar-rs"
  -h, --help              Show help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --threshold-mib) THRESHOLD_MIB="${2:-}"; shift 2 ;;
    --docker) DOCKER_SERVICES="${2:-}"; shift 2 ;;
    --extra-patterns) EXTRA_PATTERNS="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

total_mib=0

echo "=== Docker memory ==="
for name in $DOCKER_SERVICES; do
  line="$(docker stats --no-stream --format '{{.Name}}|{{.MemUsage}}' | grep "^${name}|" || true)"
  if [[ -z "$line" ]]; then
    echo "$name: not running"
    continue
  fi
  usage_field="${line#*|}"
  used_raw="${usage_field%%/*}"
  used_raw="$(echo "$used_raw" | xargs)"
  mib="$(python3 - <<PY
s='''$used_raw'''.strip().lower()
num=''.join(ch for ch in s if ch.isdigit() or ch=='.')
unit=''.join(ch for ch in s if ch.isalpha())
val=float(num) if num else 0.0
if unit in ('mib','mb'): out=val
elif unit in ('gib','gb'): out=val*1024
elif unit in ('kib','kb'): out=val/1024
elif unit in ('b',''): out=val/(1024*1024)
else: out=val
print(int(round(out)))
PY
)"
  echo "$name: $used_raw (~${mib} MiB)"
  total_mib=$((total_mib + mib))
done

echo
echo "=== Host extra processes ==="
extra_mib=0
if [[ -n "$EXTRA_PATTERNS" ]]; then
  while IFS='|' read -r pid cmd rss_kb; do
    [[ -z "${pid:-}" ]] && continue
    mib=$(( (rss_kb + 1023) / 1024 ))
    extra_mib=$((extra_mib + mib))
    echo "pid=$pid rss=${mib}MiB cmd=$cmd"
  done < <(ps -eo pid=,cmd=,rss= | grep -E "$EXTRA_PATTERNS" | grep -v grep | awk '{pid=$1; rss=$NF; $1=""; $NF=""; sub(/^ +/, ""); sub(/ +$/, ""); print pid"|"$0"|"rss }')
fi

if [[ $extra_mib -eq 0 ]]; then
  echo "(none)"
fi

total_mib=$((total_mib + extra_mib))

echo
echo "=== Budget result ==="
echo "threshold: ${THRESHOLD_MIB} MiB"
echo "current:   ${total_mib} MiB"

if [[ $total_mib -le $THRESHOLD_MIB ]]; then
  echo "status: PASS"
  exit 0
else
  echo "status: FAIL"
  exit 1
fi