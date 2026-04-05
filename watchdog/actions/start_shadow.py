"""Start shadow gateway action — spawn shadow.py if it's dead."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import logging
from pathlib import Path

from .base import Action

logger = logging.getLogger("watchdog.actions")


class StartShadowAction(Action):
    """Start or restart the shadow gateway process.

    Use this when shadow is dead (not just inactive).
    Unlike ActivateShadowAction which sends TCP command to an already-running shadow.
    """

    name = "ark.start_shadow"
    lockout = 30.0  # don't restart too often

    def __init__(
        self,
        workspace: str = "~/.nanobot",
        shadow_script: str = "/root/nanobot/ark_entry/shadow.py",
        shadow_port: int = 8081,
    ):
        super().__init__()
        self.workspace = Path(os.path.expanduser(workspace))
        self.shadow_script = Path(shadow_script)
        self.shadow_port = shadow_port
        self.pid_file = self.workspace / "gateway_shadow.pid"

    def _process_alive(self, pid: int) -> bool:
        """Check if process is alive AND not a zombie."""
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read().split()
            state = stat[2] if len(stat) > 2 else ""
            return state not in ("Z", "zombie", "x", "X")
        except (ProcessLookupError, FileNotFoundError, PermissionError):
            return False

    def execute(self, status) -> bool:
        """Kill stale PID file and spawn shadow.py."""
        import time

        # 1. Kill stale PID file if it exists (dead process or zombie)
        if self.pid_file.exists():
            try:
                old_pid = int(self.pid_file.read_text().strip())
                if self._process_alive(old_pid):
                    logger.warning(f"Shadow still alive (PID={old_pid}), not starting new one")
                    return True
                else:
                    logger.info(f"Shadow PID {old_pid} is dead/zombie, cleaning up PID file")
                    self.pid_file.unlink()
            except (ValueError, FileNotFoundError):
                pass

        # 2. Spawn shadow.py
        try:
            log_file = open(self.workspace / "shadow_startup.log", "a")
            proc = subprocess.Popen(
                [sys.executable, str(self.shadow_script),
                 "--port", str(self.shadow_port),
                 "--pid-file", str(self.pid_file)],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            # Wait briefly for it to write PID file
            time.sleep(0.5)
            if self.pid_file.exists():
                new_pid = int(self.pid_file.read_text().strip())
                logger.info(f"Shadow started (PID={new_pid})")
            else:
                logger.info(f"Shadow started (PID={proc.pid})")
            return True
        except Exception as e:
            logger.error(f"Failed to start shadow: {e}")
            return False
