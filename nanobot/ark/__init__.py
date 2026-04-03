"""
NanoBot ARK — 三层容灾系统

Layers:
- L0: SlotManager — 配置/记忆双槽，自检后同步 A→B
- L1: ShadowEngine — 影子进程热备，健康检查 + 故障切换
- L2: SnapshotManager — rsync 增量快照
"""

from .slot_manager import SlotManager, Slot, SelfCheckResult
from .shadow_engine import ShadowEngine
from .shadow_gateway import ShadowGateway
from .snapshot_manager import SnapshotManager, Snapshot
from .ark import ArkOrchestrator

__all__ = [
    "SlotManager",
    "Slot",
    "SelfCheckResult",
    "ShadowEngine",
    "ShadowGateway",
    "SnapshotManager",
    "Snapshot",
    "ArkOrchestrator",
]

# Convenience re-exports
from .gateway_shared import build_gateway  # noqa: F401
