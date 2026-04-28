#!/usr/bin/env bash
set -euo pipefail
repo=${1:-/root/nanobot-ops}
safe_sources="$repo/sources"
case "$safe_sources" in /root/nanobot-ops/sources) rm -rf "$safe_sources";; *) echo unsafe >&2; exit 1;; esac
mkdir -p "$safe_sources"
copy_source() {
  name=$1
  src=$2
  dst="$repo/sources/$name"
  mkdir -p "$dst/src"
  for f in Cargo.toml Cargo.lock README.md QUICKSTART.md Dockerfile Dockerfile.fast .dockerignore .gitignore build_fast_image.sh start_notify_sidecar.sh; do
    if [ -f "$src/$f" ]; then cp -a "$src/$f" "$dst/"; fi
  done
  if [ -d "$src/src" ]; then
    find "$src/src" -maxdepth 1 -type f \( -name '*.rs' -o -name '*.html' -o -name '*.css' -o -name '*.js' \) -exec cp -a {} "$dst/src/" \;
  fi
  if [ -d "$src/.cargo" ]; then
    mkdir -p "$dst/.cargo"
    find "$src/.cargo" -maxdepth 1 -type f -exec cp -a {} "$dst/.cargo/" \;
  fi
}
copy_source lof-sidecar-rs /root/.nanobot/workspace/skills/qdii-monitor/lof-sidecar-rs
copy_source notify-sidecar-rs /root/.nanobot/workspace/skills/notify-sidecar-rs
copy_source qq-sidecar-rs /root/qq-sidecar-rs
copy_source nanobot-reflexio-rs /root/nanobot-reflexio-rs
copy_source obp-rs /root/obp-rs
copy_source wechat-rss-rs /root/wechat-rss-rs

cat > "$repo/sources/README.md" <<'MD'
# Source snapshots

Runtime source snapshots for Rust sidecars on the live server.

Included:
- Cargo manifests and lockfiles
- selected files from `src/`
- lightweight build files such as Dockerfiles and README files

Excluded on purpose:
- `.env`
- databases
- logs
- `target/`
- runtime `data/`
- live task configs that may contain local-only commands
MD
