"""
RTK-style token savings tracker.

Records each command execution's input/output sizes and computes
token savings.  Data is stored in a local SQLite database with
90-day auto-cleanup.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

# ~4 chars per token (conservative estimate for mixed content)
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate token count from character length."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


class TokenTracker:
    """
    Track token savings per command execution.

    Usage:
        tracker = TokenTracker(workspace)
        tracker.track("git status", "2 files changed", "~200 chars")
        report = tracker.get_report()
    """

    def __init__(self, workspace: Path | str | None = None):
        if workspace is None:
            workspace = Path.home() / ".nanobot"
        elif isinstance(workspace, str):
            workspace = Path(workspace)
        db_dir = workspace / "filters"
        db_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = db_dir / "rtk_history.db"
        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(self._db_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS commands (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp    TEXT    NOT NULL,
                    cmd          TEXT    NOT NULL,
                    raw_len      INTEGER NOT NULL,
                    filtered_len INTEGER NOT NULL,
                    raw_tokens   INTEGER NOT NULL,
                    filtered_tokens INTEGER NOT NULL,
                    saved_tokens INTEGER NOT NULL,
                    savings_pct  REAL    NOT NULL,
                    exit_code    INTEGER DEFAULT 0,
                    exec_ms      INTEGER DEFAULT 0
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp
                ON commands(timestamp DESC)
            """)

    def track(
        self,
        cmd: str,
        raw_output: str,
        filtered_output: str,
        exit_code: int = 0,
        exec_ms: int = 0,
    ) -> dict[str, Any]:
        """
        Record one command execution.

        Returns a dict with savings stats for logging.
        """
        raw_tokens = estimate_tokens(raw_output)
        filtered_tokens = estimate_tokens(filtered_output)
        saved = raw_tokens - filtered_tokens
        pct = (saved / raw_tokens * 100) if raw_tokens > 0 else 0.0

        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cmd": cmd,
            "raw_len": len(raw_output),
            "filtered_len": len(filtered_output),
            "raw_tokens": raw_tokens,
            "filtered_tokens": filtered_tokens,
            "saved_tokens": saved,
            "savings_pct": round(pct, 1),
            "exit_code": exit_code,
            "exec_ms": exec_ms,
        }

        try:
            with sqlite3.connect(self._db_path) as db:
                db.execute(
                    """
                    INSERT INTO commands
                        (timestamp, cmd, raw_len, filtered_len,
                         raw_tokens, filtered_tokens, saved_tokens,
                         savings_pct, exit_code, exec_ms)
                    VALUES
                        (:timestamp, :cmd, :raw_len, :filtered_len,
                         :raw_tokens, :filtered_tokens, :saved_tokens,
                         :savings_pct, :exit_code, :exec_ms)
                    """,
                    row,
                )
                # 90-day cleanup
                db.execute(
                    "DELETE FROM commands WHERE timestamp < datetime('now', '-90 days')"
                )
        except sqlite3.Error:
            logger.exception("TokenTracker: failed to write to {}", self._db_path)

        row["savings_pct"] = row["savings_pct"]
        return row

    def get_report(self, days: int = 90) -> dict[str, Any]:
        """Return aggregate savings report."""
        cutoff = f"datetime('now', '-{days} days')"
        with sqlite3.connect(self._db_path) as db:
            row = db.execute(
                f"""
                SELECT
                    COUNT(*)                              AS total_commands,
                    COALESCE(SUM(saved_tokens), 0)       AS total_saved,
                    COALESCE(ROUND(AVG(savings_pct), 1), 0) AS avg_savings,
                    COALESCE(SUM(exec_ms), 0)            AS total_ms,
                    COALESCE(AVG(exec_ms), 0)            AS avg_ms
                FROM commands
                WHERE timestamp > {cutoff}
                """,
            ).fetchone()

            top = db.execute(
                f"""
                SELECT cmd, COUNT(*) AS cnt, ROUND(AVG(savings_pct),1) AS avg_pct
                FROM commands
                WHERE timestamp > {cutoff}
                GROUP BY cmd
                ORDER BY cnt DESC
                LIMIT 5
                """,
            ).fetchall()

        return {
            "total_commands": row[0],
            "total_saved_tokens": row[1],
            "avg_savings_pct": row[2],
            "total_exec_ms": row[3],
            "avg_exec_ms": round(row[4]),
            "top_commands": [
                {"cmd": r[0], "count": r[1], "avg_savings": r[2]}
                for r in top
            ],
        }
