"""
ArkOrchestrator — 统一调度器

协调 L0/L1/L2 三层容灾机制:
- L0: SlotManager — 配置/记忆双槽，自检后同步 A→B
- L1: ShadowEngine — 影子进程热备，健康检查 + 故障切换
- L2: SnapshotManager — rsync 增量快照

启动顺序:
1. L1 ShadowEngine（核心，启动双子进程）
2. L0 定时自检同步（每小时）
3. L2 定时快照（每 6 小时）
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

from .shadow_engine import ShadowEngine
from .slot_manager import SlotManager
from .snapshot_manager import SnapshotManager

logger = logging.getLogger(__name__)

SYNC_INTERVAL_HOURS = 1
SNAPSHOT_INTERVAL_HOURS = 6
SNAPSHOT_JITTER_SEC = 1800  # ±30 分钟抖动


class ArkOrchestrator:
    """
    Ark 统一调度器
    """

    def __init__(
        self,
        slot_manager: Optional[SlotManager] = None,
        shadow_engine: Optional[ShadowEngine] = None,
        snapshot_manager: Optional[SnapshotManager] = None,
        main_port: int = 8080,
        shadow_port: int = 8081,
    ):
        self._slots = slot_manager or SlotManager()
        self._shadow = shadow_engine or ShadowEngine(
            main_port=main_port,
            shadow_port=shadow_port,
        )
        self._snapshots = snapshot_manager or SnapshotManager()

        self._tasks: list[asyncio.Task] = []
        self._shutdown = False

    async def start(self):
        """启动 Ark"""
        logger.info("Starting Ark orchestrator...")

        # 启动 Shadow Engine (L1) — 核心，启动双子进程
        await self._shadow.start()

        # 启动定时同步 (L0)
        self._tasks.append(asyncio.create_task(self._sync_loop()))

        # 启动定时快照 (L2)
        self._tasks.append(asyncio.create_task(self._snapshot_loop()))

        logger.info("Ark orchestrator started")

    async def stop(self):
        """停止 Ark"""
        self._shutdown = True

        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)

        await self._shadow.stop()
        logger.info("Ark orchestrator stopped")

    # ── L0: 定时同步 ──────────────────────────────────────────────────

    async def _sync_loop(self):
        """定时自检 + 同步 A→B"""
        while not self._shutdown:
            await asyncio.sleep(SYNC_INTERVAL_HOURS * 3600)

            try:
                check = await self._slots.self_check()
                if check.passed:
                    ok = await self._slots.sync_current_to_standby()
                    if ok:
                        logger.info("Periodic sync completed")
                else:
                    logger.debug(f"Self-check skipped: {check.reason}")
            except Exception as e:
                logger.error(f"Periodic sync failed: {e}")

    # ── L2: 定时快照 ─────────────────────────────────────────────────

    async def _snapshot_loop(self):
        """定时快照循环"""
        while not self._shutdown:
            await asyncio.sleep(SNAPSHOT_INTERVAL_HOURS * 3600)

            # 随机抖动 ±30 分钟
            await asyncio.sleep(random.uniform(-SNAPSHOT_JITTER_SEC, SNAPSHOT_JITTER_SEC))

            try:
                await self._snapshots.create_snapshot(reason="scheduled")
            except Exception as e:
                logger.error(f"Periodic snapshot failed: {e}")

    # ── CLI 状态 ──────────────────────────────────────────────────────

    def status(self) -> dict:
        """返回 Ark 整体状态"""
        return {
            "shadow": {
                "main_active": self._shadow._main_is_active,
                "shadow_activated": self._shadow._shadow_activated,
                "main_pid": (
                    self._shadow._main_gateway.process.pid
                    if self._shadow._main_gateway and self._shadow._main_gateway.process
                    else None
                ),
                "shadow_pid": (
                    self._shadow._shadow_gateway.process.pid
                    if self._shadow._shadow_gateway and self._shadow._shadow_gateway.process
                    else None
                ),
            },
            "slots": self._slots.status(),
            "snapshots": {
                "count": len(self._snapshots.list_snapshots()),
                "latest": (
                    self._snapshots._get_latest().id
                    if self._snapshots._get_latest() else None
                ),
            },
        }
