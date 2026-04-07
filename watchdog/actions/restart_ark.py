"""Restart ARK action - kill everything and restart the whole ARK system."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
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
        self.restart_command = restart_command or ["/root/nanobot/venv/bin/python3", "-m", "nanobot", "ark", "start"]
        self.workspace = Path(os.path.expanduser(workspace))

    def _kill_process(self, pid: int, name: str):
        """Helper to kill a process gracefully, then forcefully."""
        try:
            logger.info(f"Terminating {name} (PID={pid})...")
            os.kill(pid, signal.SIGTERM)
            
            # Wait for up to 3 seconds for graceful exit
            for _ in range(3):
                time.sleep(1)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    logger.info(f"{name} exited gracefully.")
                    return
            
            # If still alive, KILL
            logger.warning(f"{name} (PID={pid}) failed to exit gracefully. Sending SIGKILL...")
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass

    def execute(self, status) -> bool:
        """Kill ARK processes and restart."""
        logger.warning(f"RestartArkAction triggered: {status.reason}")

        # Kill main gateway
        main_pid_file = self.workspace / "gateway_main.pid"
        if main_pid_file.exists():
            try:
                pid = int(main_pid_file.read_text().strip())
                self._kill_process(pid, "main gateway")
            except ValueError:
                pass

        # Kill shadow gateway
        shadow_pid_file = self.workspace / "gateway_shadow.pid"
        if shadow_pid_file.exists():
            try:
                pid = int(shadow_pid_file.read_text().strip())
                self._kill_process(pid, "shadow gateway")
            except ValueError:
                pass

        # Global pkill safety net
        try:
            subprocess.run(["pkill", "-9", "-f", "nanobot gateway"], timeout=5, capture_output=True)
            subprocess.run(["pkill", "-9", "-f", "nanobot ark"], timeout=5, capture_output=True)
        except Exception as e:
            logger.warning(f"pkill failed: {e}")

        # Wait extra time for port 8081 release
        logger.info("Waiting 5 seconds for ports to clear...")
        time.sleep(5)

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
