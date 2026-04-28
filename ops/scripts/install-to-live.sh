#!/usr/bin/env bash
set -euo pipefail
repo=${1:-/root/nanobot-ops}
install -m 0755 "$repo/bin/sidecarctl" /usr/local/bin/sidecarctl
install -m 0755 "$repo/sbin/rust-sidecar-maintain" /usr/local/sbin/rust-sidecar-maintain
if [ -f "$repo/scripts/deploy-sidecar.sh" ]; then install -m 0755 "$repo/scripts/deploy-sidecar.sh" /usr/local/sbin/deploy-sidecar; fi
if [ -f "$repo/sbin/podman-port-forward-allow.sh" ]; then install -m 0755 "$repo/sbin/podman-port-forward-allow.sh" /usr/local/sbin/podman-port-forward-allow.sh; fi
install -m 0644 "$repo/config/sidecars.json" /root/.nanobot/sidecars.json
for f in "$repo"/systemd/*.service "$repo"/systemd/*.target; do
  [ -e "$f" ] || continue
  install -m 0644 "$f" "/etc/systemd/system/$(basename "$f")"
done
if [ -d "$repo/systemd/drop-ins" ]; then
  find "$repo/systemd/drop-ins" -type f -name '*.conf' | while read -r f; do
    rel=${f#"$repo/systemd/drop-ins/"}
    dst="/etc/systemd/system/$rel"
    mkdir -p "$(dirname "$dst")"
    install -m 0644 "$f" "$dst"
  done
fi
systemctl daemon-reload
