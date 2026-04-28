# Restore checklist

1. Install Rust sidecar binaries to `/usr/local/bin`.
2. Run `scripts/install-to-live.sh`.
3. Run `systemctl daemon-reload` if you edited unit files manually.
4. Restart sidecars with `/usr/local/sbin/rust-sidecar-maintain restart`.
5. Verify with `sidecarctl status` and the manager page.

Do not commit secrets such as `/root/.nanobot/config.json`, RSS settings, databases, tokens, or private keys here.
