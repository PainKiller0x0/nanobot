"""ARK system monitor - Stable version (REPAIRED FULL)."""
from __future__ import annotations
import os, asyncio, time
from pathlib import Path
from .base import HealthStatus, Monitor

class ArkMonitor(Monitor):
    interval = 10.0
    cooldown = 60.0
    def __init__(self, workspace="~/.nanobot"):
        super().__init__()
        self.workspace = Path(os.path.expanduser(workspace))
        self.main_pid_file = self.workspace / "gateway_main.pid"
    def _read_pid(self, path):
        try: return int(path.read_text().strip())
        except: return None
    def _process_alive(self, pid):
        try: os.kill(pid, 0); return True
        except: return False
    def check(self) -> HealthStatus:
        main_pid = self._read_pid(self.main_pid_file)
        main_alive = main_pid is not None and self._process_alive(main_pid)
        
        # STABLE STRATEGY: As long as MAIN is alive, we are OK.
        if main_alive:
            return HealthStatus(name="ark.ok", healthy=True, reason="Main gateway alive")
        
        return HealthStatus(name="ark.dead", healthy=False, reason="Main gateway dead")
