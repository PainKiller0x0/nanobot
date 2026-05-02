# nanobot-ops

This is the lightweight operations snapshot for the live Nanobot server.

It tracks only service wiring and helper scripts. Secrets and runtime data stay outside this repo.

Architecture notes live in [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

## Useful commands

```bash
sidecarctl status
sidecarctl url rss
sidecarctl logs lof
sidecarctl restart notify
systemctl status nanobot-llm-warmup.service
```

```bash
/usr/local/sbin/rust-sidecar-maintain status
/usr/local/sbin/rust-sidecar-maintain build-install
/usr/local/sbin/rust-sidecar-maintain clean-targets
```

## Layout

- `config/sidecars.json`: Sidecar registry consumed by the manager page.
- `config/notify-sidecar-rs/config.example.json`: sanitized notify bridge example; keep the real `config.json` on the server only.
- `bin/sidecarctl`: CLI for checking URLs, status, logs, and restarts.
- `sbin/rust-sidecar-maintain`: Build/install/cache cleanup helper for Rust sidecars.
- `systemd/`: systemd units for Nanobot, Podman, sidecars, and the post-start LLM warmup oneshot.
- `scripts/sync-from-live.sh`: refresh this repo from the current server state.
- `scripts/install-to-live.sh`: install the tracked files back to the server.

## Web

- Sidecar manager: http://150.158.121.88:8093/sidecars
- LOF board: http://150.158.121.88:8093/


## Source snapshots

Rust sidecar source snapshots live under `sources/`.
They intentionally exclude `.env`, databases, logs, `target/`, and runtime data.

Refresh scripts and source snapshots from the live server:

```bash
/root/nanobot-ops/scripts/sync-from-live.sh
```


## Stack target

The live server has a systemd group target:

```bash
systemctl status nanobot-stack.target
sidecarctl stack
sidecarctl doctor
sidecarctl restart all --dry-run
```

Services have lightweight `PartOf=nanobot-stack.target` drop-ins so the stack can be started and grouped consistently without adding memory overhead.
