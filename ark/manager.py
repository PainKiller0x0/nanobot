#!/usr/bin/env python3
"""
ARK Manager — (FIXED & STABILIZED)
- Fixed: No immediate failover on start.
- Fixed: Explicit slot paths for config/workspace.
"""

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from ark.snapshot_manager import SnapshotManager

NANOBOT_ROOT = Path.home() / ".nanobot"
ARK_DIR = NANOBOT_ROOT / "ark"
SLOT_A_DIR = NANOBOT_ROOT / "slot_a"
SLOT_B_DIR = NANOBOT_ROOT / "slot_b"
ACTIVE_SLOT_FILE = NANOBOT_ROOT / "active_slot"
MAIN_PID_FILE = NANOBOT_ROOT / "gateway_main.pid"
SHADOW_PID_FILE = NANOBOT_ROOT / "gateway_shadow.pid"
LOG_DIR = NANOBOT_ROOT / "logs"

# Logging
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / "ark_manager.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("ark.manager")

HEALTH_CHECK_INTERVAL = 15.0  # Increased for stability
FAIL_THRESHOLD = 3

class ArkManager:
    def __init__(self):
        self.snapshots = SnapshotManager()
        self.main_fails = 0
        self._running = False
        self._active_slot = self._get_active_slot()

    def _get_active_slot(self) -> str:
        if ACTIVE_SLOT_FILE.exists():
            return ACTIVE_SLOT_FILE.read_text().strip().lower()
        return "b"  # Default to B (Shadow) to avoid affecting root config

    def _is_process_alive(self, pid_file: Path) -> bool:
        if not pid_file.exists():
            return False
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return True
        except (ValueError, OSError):
            return False

    async def _start_gateway(self, slot: str, port: int, pid_file: Path):
        """Start a gateway in a specific slot with isolated config/workspace."""
        slot_dir = SLOT_A_DIR if slot == "a" else SLOT_B_DIR
        config_path = slot_dir / "config.json"
        workspace_path = slot_dir / "workspace"
        
        logger.info(f"ARK: Launching Gateway in Slot {slot.upper()} (port {port})...")
        
        venv_python = Path.home() / "nanobot" / "venv" / "bin" / "python3"
        if not venv_python.exists(): venv_python = "python3"

        # Force slot-specific config and workspace
        cmd = [
            str(venv_python), "-m", "nanobot", "gateway",
            "--port", str(port),
            "--config", str(config_path),
            "--workspace", str(workspace_path),
            "--pid-file", str(pid_file)
        ]
        
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info(f"ARK: Gateway process spawned (Slot {slot.upper()})")
        except Exception as e:
            logger.error(f"ARK: Failed to launch: {e}")

    async def start(self):
        logger.info("=== ARK System (Stable Mode) Started ===")
        self._running = True
        
        # Initial check/start: give main process a chance to exist
        if not self._is_process_alive(MAIN_PID_FILE):
            logger.info("ARK: No main gateway detected on start. Booting primary...")
            await self._start_gateway("b", 8080, MAIN_PID_FILE)
            await asyncio.sleep(20) # Grace period for startup

        while self._running:
            if not self._is_process_alive(MAIN_PID_FILE):
                self.main_fails += 1
                logger.warning(f"ARK: Main gateway down! ({self.main_fails}/{FAIL_THRESHOLD})")
                
                if self.main_fails >= FAIL_THRESHOLD:
                    logger.error("ARK: Failover triggered! Restarting main in Slot B.")
                    await self.snapshots.create_snapshot(reason="failover")
                    await self._start_gateway("b", 8080, MAIN_PID_FILE)
                    self.main_fails = 0 # Reset after attempt
            else:
                if self.main_fails > 0:
                    logger.info("ARK: Main gateway recovered.")
                    self.main_fails = 0
            
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)

if __name__ == "__main__":
    try:
        asyncio.run(ArkManager().start())
    except KeyboardInterrupt:
        pass
