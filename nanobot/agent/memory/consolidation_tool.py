"""Background memory consolidation tool.

Runs in a forked subagent with read-only Bash access.
Consolidates:
  - workspace/memory/*.md  (structured memories)
  - workspace/memory.db    (vector memory SQLite)
  - workspace/memory/HISTORY.md (event log)

Produces compressed, de-duplicated outputs and updates consolidation_meta.

This module is intentionally lightweight — it runs in a subagent process,
not in the main gateway. It must be safe to interrupt at any point.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically: temp file + rename, safe against crash mid-write."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.rename(path)
    except OSError:
        # Clean up temp file on failure
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

from nanobot.agent.memory.consolidation_meta import (
    acquire_lock,
    check_gate,
    release_lock,
    mark_consolidated,
    rollback_lock_mtime,
)



# -------------------------------------------------------------------
# Memory file reading
# -------------------------------------------------------------------

def _read_md_files(workspace: Path) -> list[tuple[Path, str]]:
    """Read all .md files under workspace/memory/, return (path, content)."""
    memory_dir = workspace / "memory"
    if not memory_dir.is_dir():
        return []
    results = []
    for md in sorted(memory_dir.glob("*.md")):
        if md.name == "HISTORY.md":
            continue  # HISTORY.md handled separately
        try:
            results.append((md, md.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            pass
    return results


def _read_history_recent(workspace: Path, max_entries: int = 50) -> str:
    """Read last N entries from HISTORY.md."""
    history_path = workspace / "memory" / "HISTORY.md"
    if not history_path.exists():
        return ""
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-max_entries:])
    except (OSError, UnicodeDecodeError):
        return ""


def _read_memory_db(workspace: Path) -> list[dict[str, Any]]:
    """Read memory.db entries for context."""
    db_path = workspace / "memory.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # Get recent entries, newest first
        rows = cur.execute(
            "SELECT id, key, content, created_at FROM memories ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        logger.warning("consolidation_tool", event="db_read_error", error=str(e))
        return []


# -------------------------------------------------------------------
# Content processing
# -------------------------------------------------------------------

def _normalize_key(key: str) -> str:
    """Normalize a memory key for deduplication."""
    return re.sub(r"\s+", " ", key.strip()).lower()


def _hash_content(content: str) -> str:
    """Short hash for content deduplication."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _extract_key_from_md(content: str) -> str | None:
    """Extract the first markdown heading as the memory key."""
    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    return m.group(1).strip() if m else None


def _deduplicate_memories(
    files: list[tuple[Path, str]],
) -> list[tuple[Path, str]]:
    """Remove duplicate memories based on content hash."""
    seen_hashes: set[str] = set()
    deduped = []
    for path, content in files:
        content_hash = _hash_content(content)
        if content_hash in seen_hashes:
            logger.info("consolidation_tool", event="dedup", path=str(path))
            continue
        seen_hashes.add(content_hash)
        deduped.append((path, content))
    return deduped


def _compress_long_memories(
    files: list[tuple[Path, str]],
) -> list[tuple[Path, str]]:
    """
    Compress overly long memories by keeping only the first heading + summary.
    Memories > COMPRESS_THRESHOLD_LINES are summarized.
    """
    COMPRESS_THRESHOLD_LINES = 100
    results = []
    for path, content in files:
        lines = content.splitlines()
        if len(lines) > COMPRESS_THRESHOLD_LINES:
            # Find the first heading and a reasonable preamble
            heading_idx = -1
            for i, line in enumerate(lines):
                if re.match(r"^#+\s", line):
                    heading_idx = i
                    break
            if heading_idx >= 0:
                # Keep heading + first 30 lines as summary
                summary = "\n".join(lines[heading_idx : heading_idx + 30])
                summary += f"\n\n<!-- 内容已压缩，原长度 {len(lines)} 行 -->"
                results.append((path, summary))
                logger.info("consolidation_tool", event="compressed", path=str(path), lines=len(lines))
                continue
        results.append((path, content))
    return results


# -------------------------------------------------------------------
# Write back
# -------------------------------------------------------------------

def _write_memories(
    files: list[tuple[Path, str]],
    deleted_paths: list[Path],
) -> list[Path]:
    """Write processed memories back, delete removed ones. Returns list of touched paths."""
    touched = []
    for path, content in files:
        try:
            _atomic_write(path, content)
            touched.append(path)
        except OSError as e:
            logger.warning("consolidation_tool", event="write_failed", path=str(path), error=str(e))
    for path in deleted_paths:
        try:
            path.unlink()
            touched.append(path)
        except OSError:
            pass
    return touched


def _prune_memory_db(workspace: Path, keep_entries: int = 50) -> int:
    """Prune old vector memory entries, keep newest keep_entries. Returns count deleted."""
    db_path = workspace / "memory.db"
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT id FROM memories ORDER BY created_at DESC LIMIT ?", (keep_entries,))
        keep_ids = set(r[0] for r in cur.fetchall())
        cur.execute("SELECT id FROM memories")
        all_ids = set(r[0] for r in cur.fetchall())
        delete_ids = all_ids - keep_ids
        if delete_ids:
            placeholders = ",".join("?" * len(delete_ids))
            cur.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", tuple(delete_ids))
            conn.commit()
        conn.close()
        logger.info("consolidation_tool", event="db_pruned", deleted=len(delete_ids))
        return len(delete_ids)
    except sqlite3.Error as e:
        logger.warning("consolidation_tool", event="db_prune_error", error=str(e))
        return 0


# -------------------------------------------------------------------
# Main consolidation entry point
# -------------------------------------------------------------------

def run_consolidation(workspace: Path) -> dict[str, Any]:
    """
    Run full consolidation. Called by the forked subagent.

    Returns a summary dict:
      {
        "success": bool,
        "touched": [Path, ...],
        "deduped": int,
        "compressed": int,
        "db_pruned": int,
        "error": str | None,
      }
    """
    # Skip embedding model during consolidation to prevent OOM
    # on memory-constrained VMs (e.g. 2GB). Embedding model (~400MB)
    # competing with consolidation can cause gateway crash.
    import nanobot.agent.memory.vector_manager as _vm
    _vm._CONSOLIDATION_IN_PROGRESS = True
    try:
        return _run_consolidation_impl(workspace)
    finally:
        _vm._CONSOLIDATION_IN_PROGRESS = False


def _run_consolidation_impl(workspace: Path) -> dict[str, Any]:
    # Try to acquire lock
    gate = check_gate(workspace)
    if not gate.should_consolidate:
        return {
            "success": False,
            "error": f"gate not ready: {gate.reason}",
            "touched": [],
            "deduped": 0,
            "compressed": 0,
            "db_pruned": 0,
        }

    prior_mtime = acquire_lock(workspace)
    if prior_mtime is None:
        return {"success": False, "error": "failed to acquire lock", "touched": [], "deduped": 0, "compressed": 0, "db_pruned": 0}

    result: dict[str, Any] = {
        "success": False,
        "touched": [],
        "deduped": 0,
        "compressed": 0,
        "db_pruned": 0,
        "error": None,
    }

    try:
        # --- Read ---
        files = _read_md_files(workspace)
        history_recent = _read_history_recent(workspace)
        db_entries = _read_memory_db(workspace)

        original_count = len(files) + len(db_entries)

        # --- Process ---
        deduped = _deduplicate_memories(files)
        result["deduped"] = len(files) - len(deduped)

        compressed = _compress_long_memories(deduped)
        result["compressed"] = sum(
            1 for p, c in compressed
            if "<!-- 内容已压缩" in c
        )

        # --- Write back ---
        touched = _write_memories(compressed, deleted_paths=[])
        db_pruned = _prune_memory_db(workspace, keep_entries=50)
        result["db_pruned"] = db_pruned
        result["touched"] = [str(p) for p in touched]

        # --- Mark done ---
        mark_consolidated(workspace)
        result["success"] = True

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(
            "consolidation_tool",
            event="completed",
            success=True,
            deduped=result["deduped"],
            compressed=result["compressed"],
            db_pruned=db_pruned,
            touched=len(touched),
        )
        return result

    except Exception as e:
        logger.error("consolidation_tool", event="consolidation_failed", error=str(e))
        result["error"] = str(e)
        # Rollback lock mtime so time-gate re-fires next turn
        rollback_lock_mtime(workspace, prior_mtime)
        return result

    finally:
        # Always release the lock so next consolidation can proceed.
        # On crash, lock_timeout in consolidation_meta.py handles stale locks.
        release_lock(workspace)
