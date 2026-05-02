"""Deterministic fast replies that do not need an LLM call."""

from __future__ import annotations

import re
import time
from pathlib import Path

from nanobot.bus.events import InboundMessage, OutboundMessage


_MEMORY_PATTERNS = (
    re.compile(r"^(内存|内存怎么样|内存情况|内存占用|服务器内存|nanobot内存)[？?。!！\s]*$"),
    re.compile(r"^(看下|看看|查下|查一下).{0,8}内存.{0,8}$"),
)


def build_direct_reply(
    msg: InboundMessage,
    *,
    model: str,
    start_time: float,
    last_usage: dict[str, int] | None = None,
) -> OutboundMessage | None:
    """Return a deterministic reply for cheap status intents, if matched."""
    text = (msg.content or "").strip()
    if _is_memory_query(text):
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=_format_memory_report(model, start_time, last_usage or {}),
            metadata={**(msg.metadata or {}), "_direct_reply": True},
        )
    return None


def _is_memory_query(text: str) -> bool:
    compact = re.sub(r"\s+", "", text.lower())
    return any(pattern.search(compact) for pattern in _MEMORY_PATTERNS)


def _format_memory_report(model: str, start_time: float, last_usage: dict[str, int]) -> str:
    mem = _read_meminfo()
    cgroup = _read_cgroup_memory()
    rss = _read_process_rss()
    uptime = _format_duration(max(0, int(time.time() - start_time)))

    lines = ["内存直查（未调用 LLM）"]
    if mem:
        total = mem.get("MemTotal", 0)
        available = mem.get("MemAvailable", 0)
        used = max(0, total - available)
        pct = (used / total * 100) if total else 0
        lines.append(
            f"宿主机：{_fmt_kib(used)} / {_fmt_kib(total)}，可用 {_fmt_kib(available)}（{pct:.0f}%）"
        )
    if cgroup:
        current, limit = cgroup
        if limit:
            pct = current / limit * 100 if limit else 0
            lines.append(f"容器：{_fmt_bytes(current)} / {_fmt_bytes(limit)}（{pct:.0f}%）")
        else:
            lines.append(f"容器：{_fmt_bytes(current)}")
    if rss:
        lines.append(f"nanobot 进程 RSS：{_fmt_kib(rss)}")
    lines.append(f"运行时长：{uptime}")
    lines.append(f"模型：{model}")
    if last_usage:
        prompt = last_usage.get("prompt_tokens", 0)
        cached = last_usage.get("cached_tokens", 0)
        completion = last_usage.get("completion_tokens", 0)
        lines.append(f"上次 LLM：prompt {prompt}，cached {cached}，completion {completion}")
    return "\n".join(lines)


def _read_meminfo() -> dict[str, int]:
    data: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            name, rest = line.split(":", 1)
            value = rest.strip().split()[0]
            data[name] = int(value)
    except Exception:
        return {}
    return data


def _read_process_rss() -> int:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except Exception:
        return 0
    return 0


def _read_cgroup_memory() -> tuple[int, int | None] | None:
    current = _read_int("/sys/fs/cgroup/memory.current")
    if current is None:
        return None
    raw_limit = _read_text("/sys/fs/cgroup/memory.max")
    if not raw_limit or raw_limit == "max":
        return current, None
    try:
        return current, int(raw_limit)
    except ValueError:
        return current, None


def _read_int(path: str) -> int | None:
    text = _read_text(path)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _fmt_kib(kib: int) -> str:
    return _fmt_bytes(kib * 1024)


def _fmt_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{value} B"


def _format_duration(seconds: int) -> str:
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes or not parts:
        parts.append(f"{minutes}分钟")
    return "".join(parts)
