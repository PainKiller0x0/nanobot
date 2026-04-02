"""Consolidation status tool — allows the agent to report on auto-consolidation state."""

from datetime import datetime
from typing import Any

from nanobot.agent.memory.consolidation_meta import (
    check_gate,
    is_locked,
    read_meta,
)
from nanobot.agent.tools.base import Tool


class ConsolidationTool(Tool):
    """Query the auto-consolidation system status."""

    def __init__(self, workspace: Any) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "consolidation_status"

    @property
    def description(self) -> str:
        return (
            "Query the auto-consolidation system status. Returns when the last "
            "consolidation ran, how many hours have passed, and whether "
            "the next consolidation is due."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> str:
        meta = read_meta(self._workspace)
        gate = check_gate(self._workspace)
        locked = is_locked(self._workspace)

        if meta.last_consolidated_at == 0:
            last_str = "从未执行过"
        else:
            last_dt = datetime.fromtimestamp(meta.last_consolidated_at)
            last_str = last_dt.strftime("%Y-%m-%d %H:%M:%S")

        hours = gate.hours_since
        hours_str = f"{hours:.1f} 小时" if hours != float("inf") else "首次"

        lines = [
            f"**上次压缩时间**: {last_str}",
            f"**距上次**: {hours_str}",
            f"**时间门阈值**: {meta.threshold_hours} 小时",
            f"**当前锁状态**: {'🔒 进行中' if locked else '✅ 空闲'}",
            f"**门控状态**: {'✅ 所有门已开，待触发' if gate.should_consolidate else '⏳ ' + gate.reason}",
        ]

        return "\n".join(lines)
