"""Local Reflexio memory direct replies."""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REFLEXIO_TIMEOUT = 0.8
DASHBOARD_BASES = ("http://172.17.0.1:8093", "http://127.0.0.1:8093")
_SAVE_PATTERNS = (
    r"^\s*(?:帮我)?记住[：:，,\s]*(.+)$",
    r"^\s*(?:你)?记一下[：:，,\s]*(.+)$",
    r"^\s*以后(?:你)?(?:要)?记得[：:，,\s]*(.+)$",
)
_SEARCH_PATTERNS = (
    r"^\s*(?:查|搜索|找)(?:一下)?记忆[：:，,\s]*(.+)$",
    r"^\s*你(?:还)?记得(.+?)(?:吗|么)?[？?]?\s*$",
)
_STATUS_QUERIES = {
    "记忆状态",
    "记忆怎么样",
    "本地记忆",
    "本地记忆状态",
    "你记得什么",
    "你都记得什么",
    "记忆列表",
}
_PREFERENCE_HINTS = ("偏好", "喜欢", "不喜欢", "习惯", "希望", "以后", "尽量", "不要", "优先")


def extract_memory_to_save(text: str) -> str | None:
    for pattern in _SAVE_PATTERNS:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean_content(match.group(1))
    return None


def extract_memory_search(text: str) -> str | None:
    compact = _compact(text)
    if compact in _STATUS_QUERIES:
        return None
    for pattern in _SEARCH_PATTERNS:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            query = _clean_content(match.group(1))
            if query and query not in {"什么", "哪些", "多少"}:
                return query
    return None


def is_memory_status_query(text: str) -> bool:
    return _compact(text) in _STATUS_QUERIES


def remember_memory(content: str, user_id: str | None) -> str:
    content = _clean_content(content)
    if not content:
        return "没找到要记住的内容。你可以这样说：记住 我喜欢 Rust sidecar。"
    payload = {
        "user_id": user_id or "default_user",
        "category": _guess_category(content),
        "content": content,
        "source": "nanobot-direct-reply",
    }
    data = post_json("/reflexio/api/memories", payload, {})
    if not data.get("success"):
        return "本地记忆写入失败：" + str(data.get("msg") or data.get("error") or "Reflexio 不可用")
    return "\n".join(
        [
            "记住了（本地记忆，未调用 LLM）",
            f"- {content}",
            f"编号：#{data.get('id', '-')}",
            "详情：http://150.158.121.88:8093/reflexio/",
        ]
    )


def format_memory_status() -> str:
    stats = get_json("/reflexio/api/stats", {})
    recent = get_json("/reflexio/api/memories?limit=5", [])
    lines = [
        "本地记忆状态（未调用 LLM）",
        f"本地记忆：{stats.get('total_memories', '-')} 条；最新：{stats.get('latest_memory_at') or '-'}",
        f"历史交互：{stats.get('total_interactions', '-')} 条；旧事实：{stats.get('total_facts', '-')} 条",
        "模式：手动写入、本地 SQLite、默认不自动外传",
    ]
    if isinstance(recent, list) and recent:
        lines.append("最近记住：")
        for item in recent[:5]:
            if isinstance(item, dict):
                lines.append(f"- {_short(item.get('content'), 44)}（{item.get('category', 'note')}）")
    else:
        lines.append("最近记住：暂无。你可以说：记住 我喜欢……")
    lines.append("看板：http://150.158.121.88:8093/reflexio/")
    return "\n".join(lines)


def search_memory(query: str) -> str:
    query = _clean_content(query)
    data = post_json("/reflexio/api/memory/search", {"query": query, "limit": 8}, {"results": []})
    results = data.get("results") if isinstance(data, dict) else []
    lines = [f"本地记忆搜索：{query}（未调用 LLM）"]
    if not isinstance(results, list) or not results:
        lines.append("没搜到。可以先说：记住 ……")
    else:
        for item in results[:8]:
            if isinstance(item, dict):
                lines.append(
                    f"- #{item.get('id', '-')} {_short(item.get('content'), 54)}"
                    f"（{item.get('category', 'note')}，{item.get('created_at', '-')}）"
                )
    lines.append("看板：http://150.158.121.88:8093/reflexio/")
    return "\n".join(lines)


def get_json(path: str, default: Any) -> Any:
    return _request_json("GET", path, None, default)


def post_json(path: str, payload: dict[str, Any], default: Any) -> Any:
    return _request_json("POST", path, payload, default)


def _request_json(method: str, path: str, payload: dict[str, Any] | None, default: Any) -> Any:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    for base in _dashboard_bases():
        req = Request(base + path, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=REFLEXIO_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
            continue
    return default


def _dashboard_bases() -> list[str]:
    values = [os.environ.get("NANOBOT_DASHBOARD_URL", "").strip(), *DASHBOARD_BASES]
    bases: list[str] = []
    for value in values:
        value = value.rstrip("/")
        if value and value not in bases:
            bases.append(value)
    return bases


def _guess_category(content: str) -> str:
    return "preference" if any(hint in content for hint in _PREFERENCE_HINTS) else "note"


def _compact(text: str) -> str:
    return re.sub(r"[\s，。！？!?,.、:：;；]+", "", text.lower())


def _clean_content(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())[:4000]


def _short(value: Any, limit: int = 48) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "..."
