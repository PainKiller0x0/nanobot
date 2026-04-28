#!/usr/bin/env python3
"""QQ-friendly client for the local Trend Radar sidecar."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

_SHARED_DIR = Path(__file__).resolve().parents[1] / "_shared"
if _SHARED_DIR.exists():
    sys.path.insert(0, str(_SHARED_DIR))

from ops_common import JsonHttpClient, fmt_time as common_fmt_time, now_shanghai, parse_dt, short


HTTP = JsonHttpClient(
    [
        os.environ.get("TREND_SIDECAR_URL", "").strip(),
        "http://127.0.0.1:8095",
        "http://127.0.0.1:8093/trends",
        "http://172.17.0.1:8093/trends",
    ],
    timeout=10,
    post_timeout=25,
)
fetch_json = HTTP.get_json
post_json = HTTP.post_json


def fmt_time(value):
    return common_fmt_time(value, "%m-%d %H:%M")


def age_note(value):
    dt = parse_dt(value)
    if not dt:
        return "更新时间未知"
    minutes = int((now_shanghai() - dt).total_seconds() // 60)
    if minutes < 0:
        minutes = 0
    if minutes >= 60:
        return f"更新：{fmt_time(value)}，约 {minutes // 60} 小时前"
    return f"更新：{fmt_time(value)}，约 {minutes} 分钟前"


def item_line(item: dict[str, Any], idx: int | None = None) -> str:
    prefix = f"{idx}. " if idx is not None else "- "
    rank = item.get("rank") or item.get("best_rank") or "-"
    title = short(item.get("title"), 58)
    source = item.get("source_name") or item.get("source_id") or "-"
    url = item.get("url") or item.get("mobile_url") or ""
    link = f"\n   {url}" if url else ""
    return f"{prefix}{title}｜{source} #{rank}{link}"


def ensure_ok(data: dict[str, Any]) -> None:
    if data.get("ok") is False:
        raise RuntimeError(data.get("error") or "热点雷达返回失败")


def cmd_brief(_args: argparse.Namespace) -> str:
    status = fetch_json("/api/trends/status")
    brief = fetch_json("/api/trends/brief")
    ensure_ok(status)
    ensure_ok(brief)
    topics = brief.get("topics") or []
    top_items = brief.get("top_items") or []
    source_counts = brief.get("source_counts") or []
    source_text = ", ".join(f"{x.get('name')} {x.get('count')}" for x in source_counts[:5]) or "-"

    lines = [
        "热点雷达概览",
        f"- 数据：{brief.get('items_count', status.get('items_count', 0))} 条，{age_note(status.get('updated_at'))}",
        f"- 来源：{source_text}",
    ]
    if topics:
        lines.append("- 话题：" + " / ".join(f"{x.get('name')}({x.get('count')})" for x in topics[:8]))
    if top_items:
        lines.append("重点新闻：")
        lines.extend(item_line(x, i) for i, x in enumerate(top_items[:8], 1))
    lines.append("看板：http://150.158.121.88:8093/trends/")
    return "\n".join(lines)


def cmd_latest(args: argparse.Namespace) -> str:
    params = {"limit": args.limit}
    if args.source:
        params["source"] = args.source
    data = fetch_json("/api/trends/latest?" + urlencode(params))
    ensure_ok(data)
    items = data.get("items") or []
    lines = [f"最新热榜（{len(items)} 条）"]
    lines.extend(item_line(x, i) for i, x in enumerate(items, 1))
    return "\n".join(lines)


def cmd_search(args: argparse.Namespace) -> str:
    data = fetch_json("/api/trends/search?" + urlencode({"q": args.query, "limit": args.limit}))
    ensure_ok(data)
    items = data.get("items") or []
    lines = [f"热点搜索：{args.query}", f"- 命中：{len(items)} 条"]
    lines.extend(item_line(x, i) for i, x in enumerate(items[: args.limit], 1))
    return "\n".join(lines)


def cmd_topic(args: argparse.Namespace) -> str:
    data = fetch_json("/api/trends/topic/" + quote(args.keyword))
    ensure_ok(data)
    items = data.get("items") or []
    platforms = data.get("platforms") or []
    lines = [
        f"话题分析：{args.keyword}",
        f"- 结论：{data.get('analysis') or '-'}",
        f"- 命中：{data.get('count', 0)} 条，平台：{' / '.join(platforms) or '-'}，最佳排名：#{data.get('best_rank') or '-'}",
    ]
    lines.extend(item_line(x, i) for i, x in enumerate(items[: args.limit], 1))
    return "\n".join(lines)


def cmd_refresh(_args: argparse.Namespace) -> str:
    data = post_json("/api/trends/refresh")
    ensure_ok(data)
    return f"热点雷达已刷新：{data.get('items', 0)} 条，错误 {len(data.get('errors') or [])} 个，时间 {fmt_time(data.get('updated_at'))}"


def cmd_tools(_args: argparse.Namespace) -> str:
    data = fetch_json("/api/mcp/tools")
    ensure_ok(data)
    tools = data.get("tools") or []
    lines = [
        "Trend Radar MCP 工具",
        "- JSON-RPC：http://127.0.0.1:8095/mcp",
        "- 公网代理：http://150.158.121.88:8093/trends/mcp",
    ]
    lines.extend(f"{i}. {x.get('name')}：{short(x.get('description'), 70)}" for i, x in enumerate(tools, 1))
    return "\n".join(lines)


def cmd_mcp_call(args: argparse.Namespace) -> str:
    try:
        arguments = json.loads(args.args_json or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"--args-json 不是合法 JSON：{exc}") from exc
    data = post_json("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": args.tool, "arguments": arguments}})
    return json.dumps(data.get("result", data), ensure_ascii=False, indent=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trend Radar sidecar client")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("brief")

    latest = sub.add_parser("latest")
    latest.add_argument("--limit", type=int, default=12)
    latest.add_argument("--source", default="")

    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=12)

    topic = sub.add_parser("topic")
    topic.add_argument("keyword")
    topic.add_argument("--limit", type=int, default=12)

    sub.add_parser("refresh")
    sub.add_parser("tools")

    mcp = sub.add_parser("mcp-call")
    mcp.add_argument("tool")
    mcp.add_argument("--args-json", default="{}")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        handler = {
            "brief": cmd_brief,
            "latest": cmd_latest,
            "search": cmd_search,
            "topic": cmd_topic,
            "refresh": cmd_refresh,
            "tools": cmd_tools,
            "mcp-call": cmd_mcp_call,
        }[args.command]
        print(handler(args))
        return 0
    except Exception as exc:
        print(f"热点雷达暂时不可用：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
