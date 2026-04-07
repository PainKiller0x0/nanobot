"""
L0: SlotManager — 配置/记忆双槽 (REPAIRED FULL - STABLE VERSION)
"""
from __future__ import annotations
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
NANOBOT_ROOT = Path.home() / ".nanobot"
SLOT_A = NANOBOT_ROOT / "slot_a"
SLOT_B = NANOBOT_ROOT / "slot_b"
ACTIVE_SLOT_FILE = NANOBOT_ROOT / "active_slot"
PENDING_SWITCH_FILE = NANOBOT_ROOT / "pending_switch"
PID_FILE = NANOBOT_ROOT / "gateway_main.pid"
MAX_SESSION_AGE_SEC = 120
RSYNC_AVAILABLE = shutil.which("rsync") is not None

@dataclass
class Slot:
    name: str
    path: Path
    config: Path = field(init=False)
    memory: Path = field(init=False)
    sessions: Path = field(init=False)
    workspace: Path = field(init=False)
    def __post_init__(self):
        self.config = self.path / "config.json"
        self.memory = self.path / "memory"
        self.sessions = self.path / "sessions"
        self.workspace = self.path / "workspace"

@dataclass
class SelfCheckResult:
    pid_alive: bool = False
    session_fresh: bool = False
    passed: bool = False
    pid: Optional[int] = None
    session_mtime: Optional[datetime] = None
    reason: str = ""

class SlotManager:
    def __init__(self):
        self._current = None
        self._standby = None
        self._init_slots()
    def _init_slots(self):
        active = "a"
        if ACTIVE_SLOT_FILE.exists():
            name = ACTIVE_SLOT_FILE.read_text().strip().lower()
            if name in ("a", "b"): active = name
        if active == "a":
            self._current, self._standby = Slot("A", SLOT_A), Slot("B", SLOT_B)
        else:
            self._current, self._standby = Slot("B", SLOT_B), Slot("A", SLOT_A)
        for slot in (self._current, self._standby):
            slot.path.mkdir(parents=True, exist_ok=True)
    @property
    def current(self) -> Slot: return self._current
    @property
    def standby(self) -> Slot: return self._standby
    async def self_check(self) -> SelfCheckResult:
        result = SelfCheckResult()
        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                result.pid = pid
                os.kill(pid, 0)
                result.pid_alive = True
            except: result.pid_alive = False
        if not result.pid_alive:
            result.reason = "PID not alive"
            return result
        latest_session = self._current.sessions / "latest.json"
        if latest_session.exists():
            mtime = datetime.fromtimestamp(latest_session.stat().st_mtime)
            age = (datetime.now() - mtime).total_seconds()
            result.session_fresh = age < MAX_SESSION_AGE_SEC
            if not result.session_fresh:
                result.reason = f"session too old ({age:.0f}s)"
                return result
        else: result.session_fresh = True
        result.passed = result.pid_alive and result.session_fresh
        return result
    async def sync_current_to_standby(self) -> bool:
        check = await self.self_check()
        if not check.passed: return False
        await self._sync_to(self._current, self._standby)
        return True
    async def _sync_to(self, source: Slot, target: Slot):
        items = ["config.json", "memory", "sessions", "workspace"]
        for item in items:
            src, dst = source.path / item, target.path / item
            if not src.exists(): continue
            if RSYNC_AVAILABLE:
                subprocess.run(["rsync", "-rt", "--delete", f"{src}/" if src.is_dir() else str(src), str(dst)], capture_output=True)
            else:
                if src.is_dir():
                    if dst.exists(): shutil.rmtree(dst)
                    shutil.copytree(src, dst)
                else: shutil.copy2(src, dst)
    async def switch_to_standby(self) -> bool:
        PENDING_SWITCH_FILE.write_text(json.dumps({"target_slot": self._standby.name, "at": datetime.now().isoformat()}))
        return True
    def status(self) -> dict:
        result = {
            "current_slot": self._current.name,
            "standby_slot": self._standby.name,
            "pid_file_exists": PID_FILE.exists(),
            "session_age_sec": None,
            "has_pending_switch": PENDING_SWITCH_FILE.exists()
        }
        latest_session = self._current.sessions / "latest.json"
        if latest_session.exists():
            age = (datetime.now() - datetime.fromtimestamp(latest_session.stat().st_mtime)).total_seconds()
            result["session_age_sec"] = int(age)
        return result
