"""
NanoBot ARK — 三层容灾系统

Layers:
- L0: SlotManager — 配置/记忆双槽，自检后同步 A→B
- L1: ShadowEngine — 影子进程热备，健康检查 + 故障切换
- L2: SnapshotManager — rsync 增量快照

NOTE: This package uses lazy imports via __getattr__ to avoid eagerly
loading the full nanobot stack when only a submodule (e.g. shadow_gateway)
is needed. This is critical for the shadow gateway standby process which
must stay lightweight.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

# Lazy import support (Python 3.7+)
_SUBMODULES = {
    "SlotManager": ".slot_manager",
    "Slot": ".slot_manager",
    "SelfCheckResult": ".slot_manager",
    "ShadowEngine": ".shadow_engine",
    "ShadowGateway": ".shadow_gateway",
    "SnapshotManager": ".snapshot_manager",
    "Snapshot": ".snapshot_manager",
    "ArkOrchestrator": ".ark",
    "build_gateway": ".gateway_shared",
}

if TYPE_CHECKING:
    # Type checkers get the real imports
    from .slot_manager import SlotManager, Slot, SelfCheckResult  # noqa: F401
    from .shadow_engine import ShadowEngine  # noqa: F401
    from .shadow_gateway import ShadowGateway  # noqa: F401
    from .snapshot_manager import SnapshotManager, Snapshot  # noqa: F401
    from .ark import ArkOrchestrator  # noqa: F401
    from .gateway_shared import build_gateway  # noqa: F401


def __getattr__(name: str):
    if name not in _SUBMODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    submodule_path = _SUBMODULES[name]
    module = importlib.import_module(submodule_path, __package__)
    obj = getattr(module, name)
    # Cache in sys.modules to avoid repeated lookups
    sys.modules[__name__].__dict__[name] = obj
    return obj


__all__ = list(_SUBMODULES.keys())
