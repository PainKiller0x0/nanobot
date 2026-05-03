"""Deterministic fast replies that do not need an LLM call."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from nanobot.bus.events import InboundMessage, OutboundMessage

_MEMORY_WORD = "\u5185\u5b58"
_CAPABILITY_FILE = Path("/root/.nanobot/capabilities.json")
_DASHBOARD_TIMEOUT = 0.8
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

_CASUAL_REPLIES = {
    "\u6709\u70b9\u610f\u601d": "\u6709\u70b9\u610f\u601d\uff0c\u5c55\u5f00\u8bf4\u8bf4\uff1f",
    "\u6709\u70b9\u610f\u601d\u7684": "\u6709\u70b9\u610f\u601d\uff0c\u5c55\u5f00\u8bf4\u8bf4\uff1f",
    "\u6211\u5148\u4e0d\u544a\u8bc9\u4f60": "\u884c\uff0c\u90a3\u6211\u5148\u4fdd\u6301\u597d\u5947\u3002",
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
    if _is_capability_menu_query(text):
        return _outbound(msg, _format_capability_menu())
    if _is_capability_status_query(text):
        return _outbound(msg, _format_capability_status())
    if _is_today_brief_query(text):
        return _outbound(msg, _format_today_brief())
    if _is_ack(text) and _can_direct_ack(history or []):
        return _outbound(msg, "\u597d\uff0c\u6211\u5728\u3002")
    if casual := _casual_reply(text):
        return _outbound(msg, casual)
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


def _casual_reply(text: str) -> str | None:
    return _CASUAL_REPLIES.get(_compact_text(text))


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


def _is_capability_menu_query(text: str) -> bool:
    compact = _compact_text(text)
    exact = {
        "\u4f60\u4f1a\u4ec0\u4e48",
        "\u4f60\u80fd\u505a\u4ec0\u4e48",
        "\u4f60\u80fd\u5e72\u4ec0\u4e48",
        "\u80fd\u529b\u5217\u8868",
        "\u80fd\u529b\u83dc\u5355",
        "\u529f\u80fd\u5217\u8868",
        "\u529f\u80fd\u83dc\u5355",
        "\u6280\u80fd\u5217\u8868",
        "\u6280\u80fd\u83dc\u5355",
        "nanobot\u4f1a\u4ec0\u4e48",
        "nanobot\u80fd\u505a\u4ec0\u4e48",
    }
    if compact in exact:
        return True
    return "\u80fd\u529b" in compact and compact.endswith(("\u6709\u54ea\u4e9b", "\u662f\u4ec0\u4e48", "\u5217\u51fa\u6765"))


def _is_capability_status_query(text: str) -> bool:
    compact = _compact_text(text)
    exact = {
        "\u80fd\u529b\u72b6\u6001",
        "\u80fd\u529b\u5065\u5eb7",
        "\u670d\u52a1\u72b6\u6001",
        "\u670d\u52a1\u8fd8\u6d3b\u7740\u5417",
        "sidecar\u72b6\u6001",
        "sidecars\u72b6\u6001",
        "\u770b\u4e0b\u670d\u52a1",
        "\u67e5\u4e0b\u670d\u52a1",
    }
    return compact in exact


def _is_today_brief_query(text: str) -> bool:
    compact = _compact_text(text)
    exact = {
        "\u4eca\u5929\u5148\u770b\u4ec0\u4e48",
        "\u4eca\u5929\u6709\u4ec0\u4e48\u8981\u770b",
        "\u4eca\u65e5\u6458\u8981",
        "\u4eca\u5929\u6458\u8981",
        "\u4eca\u5929\u600e\u4e48\u5b89\u6392",
        "\u6709\u4ec0\u4e48\u5efa\u8bae",
    }
    return compact in exact


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


def _format_capability_menu() -> str:
    items = _load_capabilities()
    enabled = [item for item in items if item.get("enabled", True)]
    categories: dict[str, list[dict[str, Any]]] = {}
    for item in enabled:
        categories.setdefault(str(item.get("category") or "\u5176\u4ed6"), []).append(item)

    lines = [
        "\U0001f9ed Nanobot \u80fd\u529b\u83dc\u5355\uff08\u672a\u8c03\u7528 LLM\uff09",
        f"\u5df2\u767b\u8bb0\uff1a{len(items)} \u4e2a\uff1b\u5df2\u542f\u7528\uff1a{len(enabled)} \u4e2a",
    ]
    for category, group in sorted(categories.items()):
        lines.append("")
        lines.append(f"\u3010{category}\u3011")
        for item in group[:6]:
            name = str(item.get("name") or item.get("id") or "-")
            desc = _short(item.get("description"), 42)
            triggers = item.get("trigger_phrases") or []
            trigger = f"\uff08\u95ee\uff1a{triggers[0]}\uff09" if triggers else ""
            lines.append(f"- {name}\uff1a{desc}{trigger}")
    lines.extend(
        [
            "",
            "\u5e38\u7528\u95ee\u6cd5\uff1a",
            "- \u5185\u5b58\u600e\u4e48\u6837 / \u670d\u52a1\u72b6\u6001 / \u4eca\u5929\u5148\u770b\u4ec0\u4e48",
            "- LOF \u6709\u673a\u4f1a\u5417 / \u4eca\u5929\u6587\u7ae0\u6709\u54ea\u4e9b / \u4eca\u5929\u70ed\u70b9",
            "\u603b\u63a7\u53f0\uff1ahttp://150.158.121.88:8093/sidecars",
        ]
    )
    return "\n".join(lines)


def _format_capability_status() -> str:
    caps = _fetch_dashboard_json("/api/capabilities", {})
    sidecars = _fetch_dashboard_json("/api/sidecars", {})
    cap_summary = caps.get("summary") if isinstance(caps, dict) else {}
    side_summary = sidecars.get("summary") if isinstance(sidecars, dict) else {}
    cap_items = caps.get("items") if isinstance(caps, dict) else []
    side_items = sidecars.get("items") if isinstance(sidecars, dict) else []

    bad_caps = [item for item in cap_items or [] if isinstance(item, dict) and not item.get("ok")]
    bad_sidecars = [item for item in side_items or [] if isinstance(item, dict) and not item.get("ok")]

    if not cap_summary:
        fallback_items = _load_capabilities()
        cap_summary = {
            "total": len(fallback_items),
            "enabled": sum(1 for item in fallback_items if item.get("enabled", True)),
            "healthy": "-",
        }

    lines = [
        "\U0001f9ed \u80fd\u529b\u72b6\u6001\uff08\u672a\u8c03\u7528 LLM\uff09",
        f"\u80fd\u529b\uff1a{cap_summary.get('healthy', '-')} / {cap_summary.get('total', '-')} \u53ef\u7528\uff0c\u542f\u7528 {cap_summary.get('enabled', '-')}",
        f"\u670d\u52a1\uff1a{side_summary.get('healthy', '-')} / {side_summary.get('total', '-')} \u6b63\u5e38",
    ]
    if bad_caps:
        lines.append("\u5f02\u5e38\u80fd\u529b\uff1a" + "\u3001".join(_name(item) for item in bad_caps[:5]))
    else:
        lines.append("\u5f02\u5e38\u80fd\u529b\uff1a\u6682\u65e0")
    if bad_sidecars:
        lines.append("\u5f02\u5e38\u670d\u52a1\uff1a" + "\u3001".join(_name(item) for item in bad_sidecars[:5]))
    else:
        lines.append("\u5f02\u5e38\u670d\u52a1\uff1a\u6682\u65e0")
    lines.append("\u8be6\u60c5\uff1ahttp://150.158.121.88:8093/sidecars")
    return "\n".join(lines)


def _format_today_brief() -> str:
    system = _fetch_dashboard_json("/api/system", {})
    sidecars = _fetch_dashboard_json("/api/sidecars", {})
    caps = _fetch_dashboard_json("/api/capabilities", {})
    notify = _fetch_dashboard_json("/api/notify-jobs", {})
    articles = _fetch_dashboard_json("/rss/api/entries?days=1&limit=5", {"items": []})
    lof = _fetch_dashboard_json("/api/status", {})

    mem = _dict(system.get("memory")) if isinstance(system, dict) else {}
    side_summary = _dict(sidecars.get("summary")) if isinstance(sidecars, dict) else {}
    cap_summary = _dict(caps.get("summary")) if isinstance(caps, dict) else {}
    jobs = _list(notify.get("job_details") or notify.get("configured_jobs")) if isinstance(notify, dict) else []
    article_items = _list(articles.get("items")) if isinstance(articles, dict) else []
    rows = _list(_dict(lof.get("last_board")).get("rows")) if isinstance(lof, dict) else []
    errors = [
        job
        for job in jobs
        if isinstance(job, dict) and _dict(job.get("status")).get("last_status") in {"error", "timeout"}
    ]
    high_lof = [
        row
        for row in rows
        if isinstance(row, dict) and (_float(row.get("rt_premium_pct")) or 0) >= 5
    ]

    lines = [
        "\U0001f9ed \u4eca\u65e5\u6458\u8981\uff08\u672a\u8c03\u7528 LLM\uff09",
        f"\u7cfb\u7edf\uff1a\u5185\u5b58 {mem.get('used_mb', '-')} / {mem.get('total_mb', '-')} MB\uff1b\u670d\u52a1 {side_summary.get('healthy', '-')} / {side_summary.get('total', '-')} \u6b63\u5e38",
        f"\u80fd\u529b\uff1a{cap_summary.get('healthy', '-')} / {cap_summary.get('total', '-')} \u53ef\u7528",
        f"\u4efb\u52a1\uff1a{len(jobs)} \u4e2a\uff0c\u5f02\u5e38 {len(errors)} \u4e2a",
        f"\u6587\u7ae0\uff1a{len(article_items)} \u7bc7\uff1bLOF \u9ad8\u6ea2\u4ef7\uff1a{len(high_lof)} \u53ea",
        "",
        "\u5148\u770b\u8fd9\u4e9b\uff1a",
    ]
    attention: list[str] = []
    sidecar_items = _list(sidecars.get("items")) if isinstance(sidecars, dict) else []
    for item in sidecar_items:
        if isinstance(item, dict) and not item.get("ok"):
            attention.append(f"\u670d\u52a1\u5f02\u5e38\uff1a{_name(item)}")
    for job in errors[:3]:
        attention.append(f"\u4efb\u52a1\u5f02\u5e38\uff1a{_name(job)}")
    for row in sorted(high_lof, key=lambda r: _float(r.get("rt_premium_pct")) or -999, reverse=True)[:3]:
        attention.append(
            f"LOF\uff1a{row.get('code', '-')} {_short(row.get('name'), 14)} "
            f"{_pct(row.get('rt_premium_pct'))}"
        )
    for article in article_items[:3]:
        if isinstance(article, dict):
            attention.append(f"\u6587\u7ae0\uff1a{_short(article.get('title') or article.get('name'), 36)}")
    if not attention:
        attention.append("\u6ca1\u6709\u786c\u5f02\u5e38\uff0c\u4eca\u5929\u53ef\u4ee5\u5148\u6162\u6162\u770b\u6587\u7ae0\u548c LOF\u3002")
    lines.extend(f"- {item}" for item in attention[:8])
    return "\n".join(lines)


def _load_capabilities() -> list[dict[str, Any]]:
    path = Path(os.environ.get("CAPABILITY_REGISTRY_CONFIG", "") or _CAPABILITY_FILE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = []
    return [item for item in data if isinstance(item, dict)]


def _fetch_dashboard_json(path: str, default: Any) -> Any:
    for base in _dashboard_bases():
        url = base + path
        req = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(req, timeout=_DASHBOARD_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
            continue
    return default


def _dashboard_bases() -> list[str]:
    values = [
        os.environ.get("NANOBOT_DASHBOARD_URL", "").strip(),
        "http://172.17.0.1:8093",
        "http://127.0.0.1:8093",
    ]
    result: list[str] = []
    for value in values:
        value = value.rstrip("/")
        if value and value not in result:
            result.append(value)
    return result


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("id") or "-")


def _short(value: Any, limit: int = 48) -> str:
    text = str(value or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "..."


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(value: Any) -> str:
    number = _float(value)
    return "-" if number is None else f"{number:+.2f}%"
