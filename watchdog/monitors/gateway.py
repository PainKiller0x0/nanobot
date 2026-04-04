"""Gateway process monitor — checks main and shadow gateway are alive."""

from __future__ import annotations

import os
import time
from pathlib import Path

from .base import HealthStatus, Monitor


class GatewayMonitor(Monitor):
    """Monitor main and shadow gateway processes via PID files."""

    interval = 5.0
    cooldown = 30.0

    def __init__(
        self,
        main_pid_file: str = "~/.nanobot/gateway_main.pid",
        shadow_pid_file: str = "~/.nanobot/gateway_shadow.pid",
        grace_period: float = 60.0,
    ):
        super().__init__()
        self.main_pid_file = Path(os.path.expanduser(main_pid_file))
        self.shadow_pid_file = Path(os.path.expanduser(shadow_pid_file))
        self.grace_period = grace_period
        self._start_time = time.monotonic()

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

    def _pid_matches(self, pid: int, path: Path) -> bool:
        try:
            return pid == int(path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return False

    def check(self) -> HealthStatus:
        """Check both gateways and return the worst status."""
        # Main gateway
        main_pid = self._read_pid(self.main_pid_file)
        main_alive = main_pid is not None and self._process_alive(main_pid)
        main_pid_ok = main_pid is not None and self._pid_matches(main_pid, self.main_pid_file)

        # Grace period — don't alert within first N seconds of startup
        in_grace = (time.monotonic() - self._start_time) < self.grace_period

        if not main_alive and not in_grace:
            return HealthStatus(
                name="gateway.main_dead",
                healthy=False,
                reason=f"main gateway dead (PID file: {self.main_pid_file})",
                details={"pid": main_pid, "pid_file": str(self.main_pid_file)},
            )

        # Shadow gateway
        shadow_pid = self._read_pid(self.shadow_pid_file)
        shadow_alive = shadow_pid is not None and self._process_alive(shadow_pid)

        if not shadow_alive:
            return HealthStatus(
                name="gateway.shadow_dead",
                healthy=False,
                reason=f"shadow gateway dead (PID file: {self.shadow_pid_file})",
                details={"pid": shadow_pid, "pid_file": str(self.shadow_pid_file)},
            )

        return HealthStatus(
            name="gateway.ok",
            healthy=True,
            reason="main and shadow both alive",
            details={
                "main_pid": main_pid,
                "shadow_pid": shadow_pid,
            },
        )
