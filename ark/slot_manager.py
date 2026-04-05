"""
L0: SlotManager — 配置/记忆双槽

职责:
1. 维护 A/B 两个工作目录槽位
2. 自检通过后同步 A→B（B 永远是上一个确认安全的版本）
3. 提供槽位切换接口

目录结构:
~/.nanobot/
├── slot_a/          # Slot A (config + memory + sessions + workspace)
├── slot_b/          # Slot B
├── active_slot      # 当前激活的槽位 (a 或 b)
└── pending_switch   # watchdog 切换标记
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# nanobot 工作目录根路径
NANOBOT_ROOT = Path.home() / ".nanobot"
SLOT_A = NANOBOT_ROOT / "slot_a"
SLOT_B = NANOBOT_ROOT / "slot_b"
ACTIVE_SLOT_FILE = NANOBOT_ROOT / "active_slot"
PENDING_SWITCH_FILE = NANOBOT_ROOT / "pending_switch"
PID_FILE = NANOBOT_ROOT / "gateway.pid"

# 自检参数
MAX_SESSION_AGE_SEC = 120  # session 文件超过 2 分钟认为不健康


@dataclass
class Slot:
    """单个槽位"""
    name: str  # "A" 或 "B"
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
    """自检结果"""
    pid_alive: bool = False
    session_fresh: bool = False
    passed: bool = False
    pid: Optional[int] = None
    session_mtime: Optional[datetime] = None
    reason: str = ""


class SlotManager:
    """
    L0: 配置/记忆双槽管理器

    设计原则（对标 Android A/B）:
    - A = 当前运行中的版本
    - B = 上一个确认安全的版本
    - 自检通过后才同步 A→B
    - B 永远落后 A 一个确认版本
    """

    def __init__(self):
        self._current: Optional[Slot] = None
        self._standby: Optional[Slot] = None
        self._initialized = False
        self._init_slots()

    def _init_slots(self):
        """初始化槽位"""
        active = self._get_active_slot_name()

        if active == "a":
            self._current = Slot("A", SLOT_A)
            self._standby = Slot("B", SLOT_B)
        else:
            self._current = Slot("B", SLOT_B)
            self._standby = Slot("A", SLOT_A)

        # 确保槽位目录存在
        for slot in (self._current, self._standby):
            slot.path.mkdir(parents=True, exist_ok=True)

        self._initialized = True
        logger.info(f"SlotManager: current={self._current.name}, standby={self._standby.name}")

    def _get_active_slot_name(self) -> str:
        """读取当前激活的槽位"""
        if ACTIVE_SLOT_FILE.exists():
            name = ACTIVE_SLOT_FILE.read_text().strip().lower()
            if name in ("a", "b"):
                return name
        # 默认 A
        return "a"

    def _set_active_slot(self, name: str):
        """设置激活的槽位"""
        ACTIVE_SLOT_FILE.write_text(name.upper())
        logger.info(f"Active slot set to {name.upper()}")

    @property
    def current(self) -> Slot:
        return self._current

    @property
    def standby(self) -> Slot:
        return self._standby

    # ── 自检 ────────────────────────────────────────────────────────────

    async def self_check(self) -> SelfCheckResult:
        """
        自检：确保当前槽位健康
        检查项:
        1. 进程存活（PID 文件）
        2. session 文件新鲜度
        """
        result = SelfCheckResult()

        # 1. 检查进程
        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                result.pid = pid
                result.pid_alive = self._check_process_alive(pid)
            except (ValueError, OSError):
                result.pid_alive = False

        if not result.pid_alive:
            result.reason = f"PID {'unknown' if result.pid is None else result.pid} not alive"
            return result

        # 2. 检查 session 新鲜度
        latest_session = self._current.sessions / "latest.json"
        if latest_session.exists():
            mtime = datetime.fromtimestamp(latest_session.stat().st_mtime)
            age = (datetime.now() - mtime).total_seconds()
            result.session_mtime = mtime
            result.session_fresh = age < MAX_SESSION_AGE_SEC

            if not result.session_fresh:
                result.reason = f"session too old ({age:.0f}s > {MAX_SESSION_AGE_SEC}s)"
                return result
        else:
            # 没有 session 文件（可能是新启动），只要进程活着就算通过
            logger.debug("No session file found, skipping session freshness check")
            result.session_fresh = True

        result.passed = result.pid_alive and result.session_fresh
        if result.passed:
            result.reason = "all checks passed"
        return result

    def _check_process_alive(self, pid: int) -> bool:
        """检查进程是否存活"""
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    # ── 同步 A→B ────────────────────────────────────────────────────────

    async def sync_current_to_standby(self) -> bool:
        """
        自检通过后，同步当前槽位到备用槽位
        B 变成上一个确认安全的版本
        """
        # 自检
        check = await self.self_check()
        if not check.passed:
            logger.warning(f"Self-check failed, skipping sync: {check.reason}")
            return False

        logger.info(f"Self-check passed, syncing {self._current.name} -> {self._standby.name}")

        # 同步
        await self._sync_to(self._current, self._standby)

        # 交换 current/standby（B=新确认版，A=备用）
        self._current, self._standby = self._standby, self._current
        self._set_active_slot(self._current.name.lower())

        logger.info(f"Sync complete: {self._current.name} is now active and synced")
        return True

    async def _sync_to(self, source: Slot, target: Slot):
        """将 source 槽位同步到 target 槽位"""
        # 同步 config + memory + sessions + workspace
        dirs_to_sync = ["config.json", "memory", "sessions", "workspace"]
        # workspace 是 nanobot 内部 workspace，非 ~/.nanobot/workspace
        internal_workspace = NANOBOT_ROOT / "workspace"

        for item in dirs_to_sync:
            src = source.path / item
            dst = target.path / item

            if not src.exists():
                continue

            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                logger.debug(f"  synced dir: {item}")
            else:
                shutil.copy2(src, dst)
                logger.debug(f"  synced file: {item}")

        logger.info(f"Synced {source.name} -> {target.name}")

    # ── 切换 ────────────────────────────────────────────────────────────

    async def switch_to_standby(self) -> bool:
        """
        切换到备用槽位
        通知 watchdog 执行真正的进程切换
        """
        if not self._standby.path.exists():
            logger.error(f"Standby slot {self._standby.name} not initialized")
            return False

        # 写入 pending_switch 标记，watchdog 检测到后执行切换
        PENDING_SWITCH_FILE.write_text(json.dumps({
            "target_slot": self._standby.name,
            "timestamp": datetime.now().isoformat(),
            "reason": "health_check_failed"
        }))
        logger.info(f"Pending switch to {self._standby.name}, waiting for watchdog")

        return True

    def clear_pending_switch(self):
        """清除切换标记"""
        if PENDING_SWITCH_FILE.exists():
            PENDING_SWITCH_FILE.unlink()

    # ── CLI 辅助 ────────────────────────────────────────────────────────

    def status(self) -> dict:
        """返回槽位状态"""
        check = None
        if self._current and self._current.path.exists():
            latest = self._current.sessions / "latest.json"
            if latest.exists():
                mtime = datetime.fromtimestamp(latest.stat().st_mtime)
                age = (datetime.now() - mtime).total_seconds()
            else:
                mtime = None
                age = None
        else:
            mtime = age = None

        return {
            "current_slot": self._current.name if self._current else "unknown",
            "standby_slot": self._standby.name if self._standby else "unknown",
            "has_pending_switch": PENDING_SWITCH_FILE.exists(),
            "pid_file_exists": PID_FILE.exists(),
            "session_mtime": mtime.isoformat() if mtime else None,
            "session_age_sec": round(age, 1) if age is not None else None,
        }

    def init_slots_from_current(self):
        """
        从当前运行状态初始化 slot（首次设置时调用）
        将当前 ~/.nanobot 下的内容复制到 slot A
        """
        if SLOT_A.exists() and any(SLOT_A.iterdir()):
            logger.info("Slot A already initialized, skipping")
            return

        logger.info("Initializing Slot A from current ~/.nanobot")

        for item in ("config.json", "memory", "sessions"):
            src = NANOBOT_ROOT / item
            dst = SLOT_A / item
            if src.exists():
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)

        # workspace 也复制（内部 workspace）
        internal_ws = NANOBOT_ROOT / "workspace"
        if internal_ws.exists():
            shutil.copytree(internal_ws, SLOT_A / "workspace", dirs_exist_ok=True)

        self._set_active_slot("a")
        logger.info("Slot A initialized")
