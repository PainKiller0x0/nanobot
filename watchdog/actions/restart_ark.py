"""Restart ARK action — kill everything and restart the whole ARK system."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
from pathlib import Path

from .base import Action

logger = logging.getLogger("watchdog.actions")


class RestartArkAction(Action):
    """Kill the entire ARK process tree and restart it."""

    name = "ark.restart_ark"
    lockout = 120.0  # don't restart again within 2 minutes

    def __init__(
        self,
        restart_command: list[str] | None = None,
        workspace: str = "~/.nanobot",
    ):
        super().__init__()
        self.restart_command = restart_command or ["python3", "-m", "nanobot", "ark", "start"]
        self.workspace = Path(os.path.expanduser(workspace))

    def execute(self, status) -> bool:
        """Kill ARK processes and restart."""
        logger.warning(f"RestartArkAction triggered: {status.reason}")

        # Find and kill all nanobot gateway processes
        try:
            # Kill main gateway
            main_pid_file = self.workspace / "gateway_main.pid"
            if main_pid_file.exists():
                try:
                    pid = int(main_pid_file.read_text().strip())
                    os.kill(pid, signal.SIGTERM)
                    logger.info(f"Killed main gateway PID={pid}")
                except (ProcessLookupError, ValueError):
                    pass

            # Kill shadow gateway
            shadow_pid_file = self.workspace / "gateway_shadow.pid"
            if shadow_pid_file.exists():
                try:
                    pid = int(shadow_pid_file.read_text().strip())
                    os.kill(pid, signal.SIGTERM)
                    logger.info(f"Killed shadow gateway PID={pid}")
                except (ProcessLookupError, ValueError):
                    pass

            # Kill ark manager (no dedicated PID file — use pkill as safety net)

        except Exception as e:
            logger.error(f"Error killing processes: {e}")

        # Also use pkill as safety net
        try:
            subprocess.run(["pkill", "-f", "nanobot gateway"], timeout=10, capture_output=True)
            subprocess.run(["pkill", "-f", "nanobot ark"], timeout=10, capture_output=True)
        except Exception as e:
            logger.warning(f"pkill failed: {e}")

        # Wait a moment for graceful shutdown
        import time
        time.sleep(3)

        # Restart ARK
        try:
            logger.info(f"Restarting ARK: {' '.join(self.restart_command)}")
            subprocess.Popen(
                self.restart_command,
                cwd=str(self.workspace.parent),
                stdout=open(self.workspace / "watchdog_restart.log", "a"),
                stderr=subprocess.STDOUT,
            )
            logger.info("ARK restart command sent")
            return True
        except Exception as e:
            logger.error(f"Failed to restart ARK: {e}")
            return False
