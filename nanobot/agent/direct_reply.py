"""Deterministic fast replies that do not need an LLM call."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from nanobot.bus.events import InboundMessage, OutboundMessage


_MEMORY_WORD = "\u5185\u5b58"
_ACK_WORDS = {
    "ok",
    "okay",
    "\u55ef",
    "\u55ef\u55ef",
    "\u597d",
    "\u597d\u7684",
    "\u597d\u53ef\u4ee5",
    "\u53ef\u4ee5",
    "\u884c",
    "\u884c\u7684",
    "\u6ca1\u95ee\u9898",
    "\u6536\u5230",
    "\u4e86\u89e3",
    "\u660e\u767d",
}
_ACTION_HINTS = (
    "\u8981\u4e0d\u8981",
    "\u662f\u5426",
    "\u786e\u8ba4",
    "\u9009\u62e9",
    "\u9700\u8981\u6211",
    "\u6211\u53ef\u4ee5",
    "\u8981\u6211",
    "\u7ee7\u7eed\u5417",
    "\u6267\u884c\u5417",
    "\u8fd0\u884c\u5417",
    "\u91cd\u542f\u5417",
    "\u5220\u9664\u5417",
    "\u63d0\u4ea4\u5417",
    "\u63a8\u9001\u5417",
    "\u90e8\u7f72\u5417",
    "\u5b89\u88c5\u5417",
    "\u540c\u6b65\u5417",
    "reply",
    "choose",
)


def build_direct_reply(
    msg: InboundMessage,
    *,
    model: str,
    start_time: float,
    last_usage: dict[str, int] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> OutboundMessage | None:
    """Return a deterministic reply for cheap status/chitchat intents, if matched."""
    text = (msg.content or "").strip()
    if _is_memory_query(text):
        return _outbound(msg, _format_memory_report(model, start_time, last_usage or {}))
    if _is_ack(text) and _can_direct_ack(history or []):
        return _outbound(msg, "\u597d\uff0c\u6211\u5728\u3002")
    return None


def _outbound(msg: InboundMessage, content: str) -> OutboundMessage:
    return OutboundMessage(
        channel=msg.channel,
        chat_id=msg.chat_id,
        content=content,
        metadata={**(msg.metadata or {}), "_direct_reply": True},
    )


def _compact_text(text: str) -> str:
    return re.sub(r"[\s\uff0c\u3002\uff01\uff1f!?,.\u3001:\uff1a;\uff1b]+", "", text.lower())


def _is_memory_query(text: str) -> bool:
    compact = _compact_text(text)
    if not compact:
        return False
    exact = {
        _MEMORY_WORD,
        f"{_MEMORY_WORD}\u600e\u4e48\u6837",
        f"{_MEMORY_WORD}\u60c5\u51b5",
        f"{_MEMORY_WORD}\u5360\u7528",
        f"\u670d\u52a1\u5668{_MEMORY_WORD}",
        f"nanobot{_MEMORY_WORD}",
    }
    if compact in exact:
        return True
    return _MEMORY_WORD in compact and len(compact) <= 18 and compact.startswith((
        "\u770b\u4e0b",
        "\u770b\u770b",
        "\u67e5\u4e0b",
        "\u67e5\u4e00\u4e0b",
    ))


def _is_ack(text: str) -> bool:
    compact = _compact_text(text)
    return compact in _ACK_WORDS


def _can_direct_ack(history: list[dict[str, Any]]) -> bool:
    """Avoid swallowing confirmations for pending questions or proposed actions."""
    last_assistant = ""
    for item in reversed(history):
        if item.get("role") != "assistant":
            continue
        content = item.get("content")
        if isinstance(content, str):
            last_assistant = content
            break
    if not last_assistant:
        return True
    compact = _compact_text(last_assistant)
    return not any(hint in compact for hint in _ACTION_HINTS)


def _format_memory_report(model: str, start_time: float, last_usage: dict[str, int]) -> str:
    mem = _read_meminfo()
    cgroup = _read_cgroup_memory()
    rss = _read_process_rss()
    uptime = _format_duration(max(0, int(time.time() - start_time)))

    lines = ["\u5185\u5b58\u76f4\u67e5\uff08\u672a\u8c03\u7528 LLM\uff09"]
    if mem:
        total = mem.get("MemTotal", 0)
        available = mem.get("MemAvailable", 0)
        used = max(0, total - available)
        pct = (used / total * 100) if total else 0
        lines.append(
            f"\u5bbf\u4e3b\u673a\uff1a{_fmt_kib(used)} / {_fmt_kib(total)}\uff0c"
            f"\u53ef\u7528 {_fmt_kib(available)}\uff08{pct:.0f}%\uff09"
        )
    if cgroup:
        current, limit = cgroup
        if limit:
            pct = current / limit * 100 if limit else 0
            lines.append(f"\u5bb9\u5668\uff1a{_fmt_bytes(current)} / {_fmt_bytes(limit)}\uff08{pct:.0f}%\uff09")
        else:
            lines.append(f"\u5bb9\u5668\uff1a{_fmt_bytes(current)}")
    if rss:
        lines.append(f"nanobot \u8fdb\u7a0b RSS\uff1a{_fmt_kib(rss)}")
    lines.append(f"\u8fd0\u884c\u65f6\u957f\uff1a{uptime}")
    lines.append(f"\u6a21\u578b\uff1a{model}")
    if last_usage:
        prompt = last_usage.get("prompt_tokens", 0)
        cached = last_usage.get("cached_tokens", 0)
        completion = last_usage.get("completion_tokens", 0)
        lines.append(f"\u4e0a\u6b21 LLM\uff1aprompt {prompt}\uff0ccached {cached}\uff0ccompletion {completion}")
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
        parts.append(f"{days}\u5929")
    if hours:
        parts.append(f"{hours}\u5c0f\u65f6")
    if minutes or not parts:
        parts.append(f"{minutes}\u5206\u949f")
    return "".join(parts)
