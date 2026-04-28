#!/usr/bin/env bash
set -euo pipefail
repo=${1:-/root/nanobot-ops}
mkdir -p "$repo"/{bin,sbin,config,systemd/drop-ins}
install -m 0755 /usr/local/bin/sidecarctl "$repo/bin/sidecarctl"
install -m 0755 /usr/local/sbin/rust-sidecar-maintain "$repo/sbin/rust-sidecar-maintain"
if [ -f /usr/local/sbin/podman-port-forward-allow.sh ]; then install -m 0755 /usr/local/sbin/podman-port-forward-allow.sh "$repo/sbin/podman-port-forward-allow.sh"; fi
install -m 0644 /root/.nanobot/sidecars.json "$repo/config/sidecars.json"
for unit in podman-nanobot-cage.service podman-wechat-rss-sidecar.service podman-port-forward-allow.service lof-sidecar.service notify-sidecar-rs.service qq-sidecar-rs.service nanobot-reflexio-rs.service obp-rs.service; do
  install -m 0644 "/etc/systemd/system/$unit" "$repo/systemd/$unit"
  if [ -d "/etc/systemd/system/$unit.d" ]; then
    mkdir -p "$repo/systemd/drop-ins/$unit.d"
    find "/etc/systemd/system/$unit.d" -maxdepth 1 -type f -name '*.conf' -exec install -m 0644 {} "$repo/systemd/drop-ins/$unit.d/" \;
  fi
done
if [ -f /etc/systemd/system/nanobot-stack.target ]; then install -m 0644 /etc/systemd/system/nanobot-stack.target "$repo/systemd/nanobot-stack.target"; fi
if [ -x "$repo/scripts/sync-sources-from-live.sh" ]; then "$repo/scripts/sync-sources-from-live.sh" "$repo"; fi
