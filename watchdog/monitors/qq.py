"""QQ connection monitor — watches gateway log for connection errors."""

from __future__ import annotations

import re
import time
from pathlib import Path

from .base import HealthStatus, Monitor


class QqMonitor(Monitor):
    """Monitor QQ bot connection via gateway log file.

    Reads the log file from last known position and looks for
    reconnect/disconnect keywords.
    """

    interval = 10.0
    cooldown = 60.0

    def __init__(
        self,
        log_file: str = "~/.nanobot/slot_b/workspace/nanobot_gateway.log",
        keywords: list[str] | None = None,
        pos_file: str | None = None,
    ):
        super().__init__()
        self.log_file = Path(log_file).expanduser()
        self.pos_file = Path(pos_file or f"{self.log_file}.watchdog.pos").expanduser()
        self.keywords = keywords or [
            "4009",
            "reconnect",
            "Session timed out",
            "QQ bot disconnected",
            "WebSocket disconnected",
        ]
        self._pattern = re.compile(
            "|".join(re.escape(k) for k in self.keywords),
            re.IGNORECASE,
        )

    def _read_from_pos(self) -> tuple[str, int]:
        """Read log file from last position, return (new_content, new_pos)."""
        if not self.log_file.exists():
            return "", 0

        pos = 0
        if self.pos_file.exists():
            try:
                pos = int(self.pos_file.read_text().strip())
            except ValueError:
                pos = 0

        try:
            content = self.log_file.read_text()
            size = len(content.encode())
            if pos > size:
                pos = 0  # log was rotated/truncated
            new_content = content[pos:]
            return new_content, size
        except (FileNotFoundError, OSError):
            return "", 0

    def check(self) -> HealthStatus:
        """Scan log for QQ connection issues."""
        new_content, new_pos = self._read_from_pos()

        if not new_content:
            if not self.log_file.exists():
                return HealthStatus(
                    name="qq.log_missing",
                    healthy=False,
                    reason=f"Gateway log not found: {self.log_file}",
                )
            # No new content — nothing to check
            self.pos_file.write_text(str(new_pos))
            return HealthStatus(name="qq.ok", healthy=True, reason="no new log entries")

        matches = self._pattern.findall(new_content)
        self.pos_file.write_text(str(new_pos))

        if matches:
            return HealthStatus(
                name="qq.disconnected",
                healthy=False,
                reason=f"QQ connection issue detected: {matches[0]}",
                details={"match": matches[0], "matched_content": new_content[:500]},
            )

        return HealthStatus(name="qq.ok", healthy=True, reason="QQ connected")
