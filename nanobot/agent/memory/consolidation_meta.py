"""Auto-consolidation metadata management.

Manages the time gate state for background memory consolidation,
adapted for single-user local use (QQ channel).

Key principles:
  - Gate order (cheapest first): time → scan-throttle → lock
  - Session gate removed (not applicable to single-user scenario)
  - Lock file prevents concurrent consolidations
  - Scan throttle avoids checking every message turn
  - User never notified proactively; stored in metadata for on-demand query
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

LOCK_FILE = ".auto_consolidation.lock"
META_FILE = ".auto_consolidation_meta.json"
SCAN_FILE = ".auto_consolidation_scan.json"

# How long to wait before re-scanning sessions when time-gate is open but
# session-gate is still closed (avoids constant stat() on every message)
SCAN_THROTTLE_SECONDS = 10 * 60  # 10 minutes

# Lock timeout: treat stale locks (crashed processes) as expired after this.
# Must be longer than a normal consolidation run; 5 min covers most cases.
LOCK_TIMEOUT_SECONDS = 5 * 60  # 5 minutes

DEFAULT_THRESHOLD_HOURS = 1.0


@dataclass
class ConsolidationMeta:
    """Tracks auto-consolidation state (single-user: time gate only)."""

    last_consolidated_at: float = 0.0  # epoch seconds, 0 = never
    threshold_hours: float = DEFAULT_THRESHOLD_HOURS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConsolidationMeta:
        return cls(
            last_consolidated_at=float(data.get("last_consolidated_at", 0)),
            threshold_hours=float(data.get("threshold_hours", DEFAULT_THRESHOLD_HOURS)),
        )


def _get_meta_path(workspace: Path) -> Path:
    return workspace / META_FILE


def _get_lock_path(workspace: Path) -> Path:
    return workspace / LOCK_FILE


# -------------------------------------------------------------------
# Read / Write
# -------------------------------------------------------------------

def read_meta(workspace: Path) -> ConsolidationMeta:
    """Load consolidation metadata, returns default if absent or corrupt."""
    path = _get_meta_path(workspace)
    try:
        with open(path) as f:
            return ConsolidationMeta.from_dict(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return ConsolidationMeta()


def write_meta(workspace: Path, meta: ConsolidationMeta) -> None:
    """Persist consolidation metadata atomically."""
    path = _get_meta_path(workspace)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(meta.to_dict(), f, indent=2)
    tmp.rename(path)


# -------------------------------------------------------------------
# Lock management
# -------------------------------------------------------------------

def acquire_lock(workspace: Path) -> float | None:
    """
    Try to acquire the consolidation lock.

    Returns the prior mtime of the lock file if acquired, None if already locked
    (or if the existing lock is stale and was cleaned up here, also returns None).

    The prior mtime is used for rollback on failure.
    """
    lock_path = _get_lock_path(workspace)
    try:
        prior_mtime = lock_path.stat().st_mtime
    except FileNotFoundError:
        prior_mtime = 0.0

    # If a stale lock exists, clean it first
    if prior_mtime > 0 and time.time() - prior_mtime > LOCK_TIMEOUT_SECONDS:
        try:
            lock_path.unlink()
        except OSError:
            pass
        prior_mtime = 0.0

    try:
        with open(lock_path, "w") as f:
            f.write(str(os.getpid()))
        return prior_mtime
    except OSError:
        return None


def release_lock(workspace: Path) -> None:
    """Release the consolidation lock."""
    try:
        _get_lock_path(workspace).unlink()
    except FileNotFoundError:
        pass


def rollback_lock_mtime(workspace: Path, prior_mtime: float) -> None:
    """
    On consolidation failure, restore the lock file's mtime to prior_mtime.
    This makes the time-gate pass again on the next check without touching data.
    """
    lock_path = _get_lock_path(workspace)
    try:
        os.utime(lock_path, (prior_mtime, prior_mtime))
    except OSError:
        pass


def is_locked(workspace: Path) -> bool:
    """Check if another consolidation is in progress (stale locks are ignored)."""
    lock_path = _get_lock_path(workspace)
    if not lock_path.exists():
        return False
    # Treat locks older than LOCK_TIMEOUT_SECONDS as stale (crashed process)
    mtime = lock_path.stat().st_mtime
    if time.time() - mtime > LOCK_TIMEOUT_SECONDS:
        try:
            lock_path.unlink()
        except OSError:
            pass
        return False
    return True


# -------------------------------------------------------------------
# Scan throttle
# -------------------------------------------------------------------

def _get_scan_path(workspace: Path) -> Path:
    return workspace / SCAN_FILE


def read_last_scan(workspace: Path) -> float:
    """Return epoch of last session scan, or 0 if never."""
    try:
        with open(_get_scan_path(workspace)) as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0.0


def write_last_scan(workspace: Path, ts: float | None = None) -> None:
    """Record current time as last scan timestamp."""
    path = _get_scan_path(workspace)
    ts = ts or time.time()
    with open(path, "w") as f:
        f.write(str(ts))


# -------------------------------------------------------------------
# Gate check
# -------------------------------------------------------------------

@dataclass
class GateStatus:
    """Result of a gate evaluation."""

    should_consolidate: bool
    reason: str
    hours_since: float
    locked: bool


def check_gate(workspace: Path) -> GateStatus:
    """
    Evaluate consolidation gates in order (cheapest first).

    Gate order (单用户本地适配):
      1. Time gate: now - last_consolidated_at >= threshold_hours
      2. Throttle gate: last scan within SCAN_THROTTLE_SECONDS → skip
      3. Lock gate: no other consolidation running

    Returns GateStatus with should_consolidate=True if all gates pass.
    """
    meta = read_meta(workspace)
    now = time.time()

    # --- Time gate ---
    if meta.last_consolidated_at == 0:
        hours_since = float("inf")
        seconds_since = float("inf")
    else:
        seconds_since = now - meta.last_consolidated_at
        hours_since = seconds_since / 3600.0

    threshold_seconds = int(meta.threshold_hours * 3600)
    if seconds_since < threshold_seconds:
        return GateStatus(
            should_consolidate=False,
            reason=f"时间门未开: {hours_since:.1f}h < {meta.threshold_hours}h",
            hours_since=hours_since,
            locked=False,
        )

    # --- Throttle gate: if time-gate passes but we haven't scanned recently,
    # skip to avoid scanning sessions on every message ---
    last_scan = read_last_scan(workspace)
    if (now - last_scan) < SCAN_THROTTLE_SECONDS:
        return GateStatus(
            should_consolidate=False,
            reason=f"扫描节流: 距上次 {(now - last_scan) / 60:.0f}min，{(SCAN_THROTTLE_SECONDS // 60)}min 内跳过",
            hours_since=hours_since,
            locked=False,
        )

    # Record that we scanned this turn (even if consolidation doesn't fire,
    # to prevent re-scanning on every message)
    write_last_scan(workspace)

    # --- Lock gate (stale locks auto-cleaned by is_locked) ---
    if is_locked(workspace):
        return GateStatus(
            should_consolidate=False,
            reason="锁已被其他进程持有",
            hours_since=hours_since,
            locked=True,
        )

    return GateStatus(
        should_consolidate=True,
        reason=f"所有门通过: {hours_since:.1f}h",
        hours_since=hours_since,
        locked=False,
    )


# -------------------------------------------------------------------
# Post-consolidation bookkeeping
# -------------------------------------------------------------------

def mark_consolidated(workspace: Path) -> None:
    """Record successful consolidation."""
    meta = read_meta(workspace)
    meta.last_consolidated_at = time.time()
    write_meta(workspace, meta)
    write_last_scan(workspace)  # reset scan throttle after consolidation
    logger.info("consolidation_meta", event="consolidated", at=meta.last_consolidated_at)
