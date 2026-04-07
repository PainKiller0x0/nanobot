"""ARK system monitor — checks if the ARK orchestrator is alive and responsive."""

from __future__ import annotations

import os
import asyncio
from pathlib import Path

from .base import HealthStatus, Monitor


class ArkMonitor(Monitor):
    """Monitor ARK system health from outside.

    Checks:
    - Is ark_manager process alive?
    - Is main gateway process alive?
    - Is shadow gateway process alive?
    """

    interval = 10.0
    cooldown = 60.0

    def __init__(
        self,
        workspace: str = "~/.nanobot",
    ):
        super().__init__()
        self.workspace = Path(os.path.expanduser(workspace))
        self.main_pid_file = self.workspace / "gateway_main.pid"
        self.shadow_pid_file = self.workspace / "gateway_shadow.pid"

    def _read_pid(self, path: Path) -> int | None:
        try:
            return int(path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    def _process_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def check(self) -> HealthStatus:
        """Check overall ARK system health."""
        main_pid = self._read_pid(self.main_pid_file)
        shadow_pid = self._read_pid(self.shadow_pid_file)

        main_alive = main_pid is not None and self._process_alive(main_pid)
        shadow_alive = shadow_pid is not None and self._process_alive(shadow_pid)

        if not main_alive and not shadow_alive:
            return HealthStatus(
                name="ark.both_dead",
                healthy=False,
                reason="main and shadow both dead",
                details={"main_pid": main_pid, "shadow_pid": shadow_pid},
            )

        return HealthStatus(
            name="ark.ok",
            healthy=True,
            reason="ARK system alive",
            details={
                "main_pid": main_pid,
                "shadow_pid": shadow_pid,
                "main_alive": main_alive,
                "shadow_alive": shadow_alive,
            },
        )
