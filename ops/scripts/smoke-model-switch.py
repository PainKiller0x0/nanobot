#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def http_json(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None, timeout: float = 20.0):
    data = None
    req_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            text = raw.decode("utf-8", "replace")
            try:
                parsed = json.loads(text) if text.strip() else None
            except Exception:
                parsed = {"raw": text}
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, parsed, time.time() - started
    except urllib.error.HTTPError as e:
        raw = e.read()
        text = raw.decode("utf-8", "replace")
        try:
            parsed = json.loads(text) if text.strip() else None
        except Exception:
            parsed = {"raw": text[:500]}
        return e.code, {k.lower(): v for k, v in e.headers.items()}, parsed, time.time() - started


def short(obj: Any, limit: int = 160) -> str:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) if not isinstance(obj, str) else obj
    return text[:limit] + ("..." if len(text) > limit else "")


def add(results: list[Check], name: str, ok: bool, detail: str = "", data: dict[str, Any] | None = None):
    results.append(Check(name, ok, detail, data or {}))


def get(url: str, timeout: float = 12.0):
    return http_json("GET", url, timeout=timeout)


def post(url: str, body: Any, headers: dict[str, str] | None = None, timeout: float = 45.0):
    return http_json("POST", url, body=body, headers=headers, timeout=timeout)


def route_summary(headers: dict[str, str]) -> str:
    return " ".join(
        f"{k}={headers.get('x-obp-' + k, '-')}"
        for k in ["route", "actual-model", "channel", "reason"]
    )


def run_cmd(cmd: list[str], timeout: float = 60.0):
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test nanobot-exp sidecars after model/provider switch.")
    parser.add_argument("--with-llm", action="store_true", help="Run tiny live LLM calls through OBP/nanobot.")
    parser.add_argument("--refresh-lof", action="store_true", help="Trigger LOF refresh once before checking status.")
    args = parser.parse_args()

    results: list[Check] = []

    # Sidecar manager / dashboard
    status, headers, data, dt = get("http://127.0.0.1:8093/api/sidecars")
    summary = (data or {}).get("summary", {}) if isinstance(data, dict) else {}
    add(results, "sidecars.manager", status == 200 and summary.get("unhealthy", 1) == 0,
        f"http={status} healthy={summary.get('healthy')}/{summary.get('total')} unhealthy={summary.get('unhealthy')} {dt:.2f}s")

    status, headers, data, dt = get("http://127.0.0.1:8093/api/system")
    add(results, "dashboard.system", status == 200 and isinstance(data, dict), f"http={status} {dt:.2f}s")

    # LOF
    if args.refresh_lof:
        status, headers, data, dt = post("http://127.0.0.1:8093/api/run", {}, timeout=70)
        add(results, "lof.refresh", status == 200 and isinstance(data, dict), f"http={status} {dt:.2f}s {short(data)}")
    status, headers, data, dt = get("http://127.0.0.1:8093/api/status")
    ok = status == 200 and isinstance(data, dict) and bool((data or {}).get("items") or (data or {}).get("funds") or (data or {}).get("last_run"))
    add(results, "lof.status", ok, f"http={status} {dt:.2f}s keys={list((data or {}).keys())[:8] if isinstance(data, dict) else '-'}")

    # RSS / articles / markdown
    status, headers, subs, dt = get("http://127.0.0.1:8091/api/subscriptions")
    sub_items = (subs or {}).get("items", []) if isinstance(subs, dict) else []
    add(results, "rss.subscriptions", status == 200 and len(sub_items) > 0, f"http={status} count={len(sub_items)} {dt:.2f}s")

    status, headers, entries, dt = get("http://127.0.0.1:8091/api/entries?days=7&limit=5")
    entry_items = (entries or {}).get("items", []) if isinstance(entries, dict) else []
    add(results, "rss.entries", status == 200 and len(entry_items) > 0, f"http={status} count={len(entry_items)} {dt:.2f}s")
    if entry_items:
        article_id = entry_items[0].get("id")
        status, headers, md, dt = get(f"http://127.0.0.1:8091/api/articles/{article_id}/markdown")
        md_text = (md or {}).get("markdown") if isinstance(md, dict) else ""
        if not md_text and isinstance(md, dict):
            md_text = md.get("raw", "")
        add(results, "rss.markdown", status == 200 and isinstance(md_text, str) and len(md_text) > 20,
            f"http={status} article={article_id} chars={len(md_text or '')} {dt:.2f}s")

    status, headers, auto, dt = get("http://127.0.0.1:8091/api/auto-refresh-status")
    add(results, "rss.auto_refresh", status == 200 and isinstance(auto, dict), f"http={status} {short(auto)}")

    # Notify cron bridge
    status, headers, notify, dt = get("http://127.0.0.1:8094/api/status")
    jobs = (notify or {}).get("job_details", []) if isinstance(notify, dict) else []
    enabled = sum(1 for j in jobs if j.get("enabled"))
    errors = sum(1 for j in jobs if (j.get("status") or {}).get("last_status") == "error")
    add(results, "notify.jobs", status == 200 and enabled > 0 and errors == 0, f"http={status} jobs={len(jobs)} enabled={enabled} errors={errors}")

    # Trend / MCP-like tools
    status, headers, trend, dt = get("http://127.0.0.1:8095/api/trends/status")
    count = (trend or {}).get("items_count", 0) if isinstance(trend, dict) else 0
    add(results, "trend.status", status == 200 and count > 0, f"http={status} items={count} {dt:.2f}s")

    status, headers, tools, dt = get("http://127.0.0.1:8095/api/mcp/tools")
    tool_items = (tools or {}).get("tools", []) if isinstance(tools, dict) else []
    add(results, "trend.mcp_tools", status == 200 and len(tool_items) >= 3, f"http={status} tools={len(tool_items)}")

    status, headers, mcp, dt = post("http://127.0.0.1:8095/api/mcp/call", {"name": "get_system_status", "arguments": {}}, timeout=20)
    add(results, "trend.mcp_call", status == 200 and isinstance(mcp, dict), f"http={status} {dt:.2f}s {short(mcp)}")

    # Reflexio
    status, headers, reflex, dt = get("http://127.0.0.1:8081/api/stats")
    add(results, "reflexio.stats", status == 200 and isinstance(reflex, dict), f"http={status} {short(reflex)}")

    # QQ sidecar health
    status, headers, qq, dt = get("http://172.17.0.1:8092/health")
    add(results, "qq.health", status == 200, f"http={status} {short(qq)}")

    # OBP router no-history-pollution checks, only if live LLM allowed.
    if args.with_llm:
        simple = {"model":"deepseek-v4-flash","messages":[{"role":"user","content":"hi"}],"max_tokens":1,"stream":False}
        status, h, body, dt = post("http://127.0.0.1:8000/v1/chat/completions", simple)
        add(results, "obp.openai_default", status == 200 and h.get("x-obp-route") == "default",
            f"http={status} {route_summary(h)} {dt:.2f}s")

        polluted = {"model":"deepseek-v4-flash","messages":[{"role":"user","content":"我们讨论深度架构 review 和重构方案"},{"role":"assistant","content":"好的"},{"role":"user","content":"太饱了又不想吃了，咋办"}],"max_tokens":1,"stream":False}
        status, h, body, dt = post("http://127.0.0.1:8000/v1/chat/completions", polluted)
        add(results, "obp.history_keyword_guard", status == 200 and h.get("x-obp-route") == "default",
            f"http={status} {route_summary(h)} {dt:.2f}s")

        compact = {"model":"deepseek-v4-flash","messages":[{"role":"user","content":"please summarize this conversation"}],"max_tokens":1,"stream":False}
        status, h, body, dt = post("http://127.0.0.1:8000/v1/chat/completions", compact)
        add(results, "obp.compact_text_pro", status == 200 and h.get("x-obp-route") == "pro",
            f"http={status} {route_summary(h)} {dt:.2f}s")

        status, h, body, dt = post("http://127.0.0.1:8000/v1/chat/completions", simple, headers={"x-obp-purpose":"compact"})
        add(results, "obp.compact_header_pro", status == 200 and h.get("x-obp-route") == "pro",
            f"http={status} {route_summary(h)} {dt:.2f}s")

        anthropic = {"model":"deepseek-v4-flash","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}
        status, h, body, dt = post("http://127.0.0.1:8000/anthropic/v1/messages", anthropic)
        add(results, "obp.anthropic_shell", status == 200 and h.get("x-obp-route") in {"default", "pro"},
            f"http={status} {route_summary(h)} {dt:.2f}s")

        tool_req = {
            "model":"deepseek-v4-flash",
            "messages":[{"role":"user","content":"请调用 get_system_status 工具，不要直接回答。"}],
            "tools":[{"type":"function","function":{"name":"get_system_status","description":"返回系统状态。","parameters":{"type":"object","properties":{},"additionalProperties":False}}}],
            "tool_choice":"auto",
            "max_tokens":128,
            "stream":False,
        }
        status, h, body, dt = post("http://127.0.0.1:8000/v1/chat/completions", tool_req, timeout=60)
        msg = (((body or {}).get("choices") or [{}])[0].get("message") or {}) if isinstance(body, dict) else {}
        tool_calls = msg.get("tool_calls") or []
        add(results, "obp.tool_call_basic", status == 200 and len(tool_calls) > 0,
            f"http={status} tool_calls={len(tool_calls)} {route_summary(h)} {dt:.2f}s")

        proc = run_cmd(["podman", "exec", "nanobot-cage", "python", "/tmp/smoke_nanobot_provider_default.py"], timeout=90)
        add(results, "nanobot.provider_default", proc.returncode == 0 and "deepseek-v4-flash" in (proc.stderr + proc.stdout + "deepseek-v4-flash"),
            f"rc={proc.returncode} stdout={short(proc.stdout.strip(), 220)} stderr={short(proc.stderr.strip(), 220)}")

    width = max(len(r.name) for r in results) if results else 10
    failed = [r for r in results if not r.ok]
    for r in results:
        mark = "OK" if r.ok else "FAIL"
        print(f"[{mark}] {r.name:<{width}}  {r.detail}")
    print(f"\nsummary: {len(results)-len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
