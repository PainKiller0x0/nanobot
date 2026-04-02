# Changelog

All notable changes to **nanobot** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-04-03

### Added

**OC-1.0 — Agent Core Upgrades**
- `TaskRegistry` & `TaskTool`: register/cancel/get_status/list_tasks/drain for background task tracking
- Plugin hooks: `on_session_start`, `on_session_end`, `after_tool_call` — extensible at agent/runner level
- Subagent attachments: Base64/text files up to 10MB with workspace path validation
- `CompactionConfig`: threshold=0.75, target=0.35, preserve_recent=4 for memory compaction

**ZC-1.5 — Utilities & Security**
- `ConnectionPool`: async connection pooling and reuse (factory pattern, max_size control)
- `BatchProcessor`: batched IO for reduced syscall overhead (configurable batch_size + timeout)
- `LazyLoader`: on-demand deferred imports to reduce startup cost
- `PathPolicy`: workspace containment + security path blocking (BLOCKED_PATHS)
- `PairingManager`: 6-digit one-time pairing codes with expiry for device authorization
- `EnvOverride`: `NANOBOT_*` environment variable config overrides with type coercion (bool/int/float/json)

**PC-1.6 — Platform & Config**
- `PlatformDetector`: arch/os/low_resource detection with per-platform optimization flags
- `ConfigValidator`: type/min-max/required/choices validation with coercion from env strings
- `HotReloadConfig`: file-watch config reload without restart

**Auto-Consolidation System** *(inspired by autoDream.ts)*
- Background memory deduplication, long-memory compression, and DB pruning
- Time-gate only (24h threshold, adapted for single-user local use)
- Scan throttle (10min) prevents per-message overhead when gate is open but not due
- Lock + mtime rollback ensures safe concurrent access and retry on failure
- `ConsolidationTool` for on-demand consolidation status query

**Channels**
- WeChat: `PlatformDetector` adaptive poll timeout (low-resource environments get shorter timeout)

**QQ Watchdog**
- Fixed double-gateway race condition on server restart (`_any_gateway_running()` check)
- Responsibilities separated: watchdog handles QQ-specific errors; `nanobot-gateway.service` handles general restarts

### Changed

- WeChat poll timeout adapts to platform resource constraints
- Memory context builder now injects semantically relevant memories via vector search (all-MiniLM-L6-v2)

### Fixed

- **PathPolicy**: workspace `/root/.nanobot` was incorrectly blocked by `/root` in BLOCKED_PATHS
- **HotReloadConfig**: `_timer` attribute was never initialized; `stop()` did not cancel the timer
- **ConnectionPool**: `asyncio.Lock` was shared across all instances (class-level bug); `iscoroutine()` was called on factory function instead of its return value
- **QQ Watchdog**: on server restart, watchdog detected its own dead PID file and spawned a duplicate gateway conflicting with `nanobot-gateway.service`
- **Auto-consolidation**: lock was never released after successful consolidation; gate check happened after lock acquisition (causing spurious lock files when gate wasn't ready); floating-point precision caused time gate to misfire at exact 24h boundary

[unreleased]: https://github.com/HKUDS/nanobot/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/HKUDS/nanobot/releases/tag/v0.2.0
