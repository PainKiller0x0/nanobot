#!/usr/bin/env python3
"""QQ-friendly personal ops summaries for Nanobot workspace skills."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_URL_CANDIDATES = [
    os.environ.get("NANOBOT_DASHBOARD_URL", "").strip(),
    "http://127.0.0.1:8093",
    "http://172.17.0.1:8093",
]
TIMEOUT_SECS = 8
SHANGHAI = timezone(timedelta(hours=8))


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def candidate_urls(path: str) -> list[str]:
    if path.startswith("http"):
        return [path]
    return [base.rstrip("/") + path for base in BASE_URL_CANDIDATES if base]


def fetch_json(path: str, default: Any = None) -> Any:
    last_exc: Exception | None = None
    for url in candidate_urls(path):
        req = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(req, timeout=TIMEOUT_SECS) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8", errors="replace"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_exc = exc
            continue
    if default is not None:
        return default
    raise RuntimeError(f"请求失败：{path} - {last_exc}") from last_exc


def post_json(path: str, payload: dict[str, Any]) -> Any:
    body = json.dumps(payload).encode("utf-8")
    last_exc: Exception | None = None
    for url in candidate_urls(path):
        req = Request(
            url,
            data=body,
            method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=TIMEOUT_SECS) as resp:
                raw = resp.read()
            try:
                return json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                return {"ok": True, "raw": raw.decode("utf-8", errors="replace")}
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"触发失败：{path} - {last_exc}") from last_exc


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    candidates = [text]
    if text.endswith("Z"):
        candidates.append(text[:-1] + "+00:00")
    if " +08:00" in text and "T" not in text:
        candidates.append(text.replace(" +08:00", "+08:00").replace(" ", "T", 1))
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SHANGHAI)
        return dt.astimezone(SHANGHAI)
    return None


def is_today(value: Any) -> bool:
    dt = parse_dt(value)
    return bool(dt and dt.date() == now_shanghai().date())


def fmt_time(value: Any) -> str:
    dt = parse_dt(value)
    if not dt:
        return "-"
    return dt.strftime("%H:%M")


def short(text: Any, limit: int = 52) -> str:
    s = str(text or "").strip().replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def pct(value: Any) -> str:
    n = num(value)
    return "-" if n is None else f"{n:+.2f}%"


def status_text(value: Any) -> str:
    mapping = {
        "ok": "正常",
        "sent": "已发送",
        "silent": "静默",
        "error": "错误",
        "timeout": "超时",
        "running": "运行中",
        None: "未知",
    }
    return mapping.get(value, str(value))


def job_name(job: dict[str, Any]) -> str:
    names = {
        "yage-ai": "鸭哥 AI 要闻",
        "wechat-sub-1": "微信文章：记忆承载",
        "wechat-sub-2": "微信文章：记忆承载3",
        "lof-morning": "LOF 早市报告",
        "lof-noon": "LOF 午市报告",
        "lof-close": "LOF 收盘报告",
        "hermes-heartbeat": "HERMES 心跳自检",
        "weather-sz-workday": "深圳工作日天气",
        "weather-gz-friday-noon": "广州周五天气",
        "weather-gz-weekend": "广州周末天气",
        "weather-sz-monday": "深圳周一天气",
    }
    return names.get(str(job.get("id")), job.get("name") or job.get("id") or "-")


def service_name(item: dict[str, Any]) -> str:
    names = {
        "nanobot": "Nanobot 核心",
        "rss": "RSS 订阅看板",
        "qq": "QQ 通知桥",
        "lof": "LOF 看板",
        "notify": "定时任务桥",
        "reflexio": "Reflexio 记忆看板",
        "obp": "OBP 兜底桥",
        "podman-public-rule": "公网端口守卫",
    }
    return names.get(str(item.get("id")), item.get("name") or item.get("id") or "-")


def source_name(entry: dict[str, Any]) -> str:
    for key in ("subscription_name", "source_name", "feed_title", "source", "account_name"):
        if entry.get(key):
            return str(entry[key])
    return "RSS"


def entry_title(entry: dict[str, Any]) -> str:
    return str(entry.get("title") or entry.get("name") or "未命名文章")


def load_bundle() -> dict[str, Any]:
    return {
        "system": fetch_json("/api/system", {}),
        "sidecars": fetch_json("/api/sidecars", {}),
        "lof": fetch_json("/api/status", {}),
        "notify": fetch_json("/api/notify-jobs", {}),
        "articles": fetch_json("/rss/api/entries?days=1&limit=8", {"items": []}),
        "subscriptions": fetch_json("/rss/api/subscriptions", {"items": []}),
    }


def lof_rows(lof: dict[str, Any]) -> list[dict[str, Any]]:
    board = lof.get("last_board") or {}
    rows = board.get("rows") or []
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def top_lof_rows(rows: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: num(row.get("rt_premium_pct") or row.get("latest_premium_pct")) or -999,
        reverse=True,
    )[:limit]


def today_jobs(notify: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = notify.get("job_details") or notify.get("configured_jobs") or []
    if not isinstance(jobs, list):
        return []
    return [
        job
        for job in jobs
        if isinstance(job, dict)
        and is_today((job.get("status") or {}).get("last_finished_at") or (job.get("status") or {}).get("last_started_at"))
    ]


def render_menu() -> str:
    return "\n".join(
        [
            "🧭 Nanobot 现在可以帮你做这些：",
            "1. 今日摘要：问“今天有什么要看”",
            "2. 系统状态：问“内存怎么样 / 服务还活着吗”",
            "3. LOF 雷达：问“LOF 现在有什么机会”",
            "4. 文章雷达：问“今天文章有哪些 / 鸭哥更新了吗”",
            "5. 定时任务：问“cron 任务怎么样 / 哪些任务在跑”",
            "6. 决策建议：问“今天怎么安排 / 有什么建议”",
            "7. 链接收件箱：发链接并说“收一下 / 值得看吗”",
            "8. 刷新动作：明确说“刷新 RSS”或“触发 LOF 刷新”才会执行",
            "",
            "默认只读，不会主动改配置、不重启服务、不发送补发消息。",
        ]
    )


def render_system(data: dict[str, Any]) -> str:
    system = data.get("system") or {}
    sidecars = data.get("sidecars") or {}
    mem = system.get("memory") or {}
    disk = system.get("disk_root") or {}
    load = system.get("loadavg") or {}
    summary = sidecars.get("summary") or {}
    unhealthy = [service_name(item) for item in sidecars.get("items", []) if isinstance(item, dict) and not item.get("ok")]
    lines = [
        "🩺 系统状态",
        f"时间：{system.get('now') or now_shanghai().strftime('%Y-%m-%d %H:%M:%S +08:00')}",
        f"内存：{mem.get('used_mb', '-')} / {mem.get('total_mb', '-')} MB（{mem.get('used_pct', '-')}%）",
        f"Swap：{mem.get('swap_used_mb', '-')} / {mem.get('swap_total_mb', '-')} MB",
        f"负载：{load.get('one', '-')} / {load.get('five', '-')} / {load.get('fifteen', '-')}",
        f"磁盘：{disk.get('used_pct', '-')} 已用，可用 {disk.get('available_mb', '-')} MB",
        f"服务：{summary.get('healthy', 0)} / {summary.get('total', 0)} 正常",
    ]
    if unhealthy:
        lines.append("异常：" + "、".join(unhealthy))
    else:
        lines.append("异常：暂无")
    return "\n".join(lines)


def render_lof(data: dict[str, Any]) -> str:
    lof = data.get("lof") or {}
    last = lof.get("last_run") or {}
    rows = lof_rows(lof)
    top = top_lof_rows(rows, 8)
    candidates = [
        row
        for row in rows
        if (num(row.get("rt_premium_pct")) or 0) >= 5
        and not row.get("suspended")
        and (num(row.get("amount_wan")) or 0) >= 50
    ]
    lines = [
        "📊 LOF 雷达",
        f"最近刷新：{fmt_time(last.get('finished_at'))}，状态：{status_text(last.get('status'))}，耗时：{last.get('duration_ms', '-')}ms",
        f"当前样本：{len(rows)} 只，高溢价可关注：{len(candidates)} 只",
    ]
    if candidates:
        lines.append("套利候选：")
        for row in top_lof_rows(candidates, 5):
            lines.append(
                f"- {row.get('code')} {short(row.get('name'), 12)}：实时 {pct(row.get('rt_premium_pct'))}，"
                f"最新 {pct(row.get('latest_premium_pct'))}，成交 {num(row.get('amount_wan')) or 0:.0f} 万，{row.get('limit_text') or '-'}"
            )
    else:
        lines.append("套利候选：暂无符合阈值的机会")
    if top:
        lines.append("溢价 TOP：")
        for row in top[:6]:
            suspended = "暂停" if row.get("suspended") else "可申购"
            lines.append(
                f"- {row.get('code')} {short(row.get('name'), 12)}：实时 {pct(row.get('rt_premium_pct'))}，"
                f"最新 {pct(row.get('latest_premium_pct'))}，{suspended}"
            )
    return "\n".join(lines)


def render_articles(data: dict[str, Any]) -> str:
    articles = data.get("articles") or {}
    subs = data.get("subscriptions") or {}
    items = articles.get("items") if isinstance(articles, dict) else []
    subscriptions = subs.get("items") if isinstance(subs, dict) else []
    items = items if isinstance(items, list) else []
    subscriptions = subscriptions if isinstance(subscriptions, list) else []
    lines = [
        "📰 文章雷达",
        f"今日抓到：{len(items)} 篇；订阅源：{len(subscriptions)} 个",
    ]
    if subscriptions:
        paused = [
            sub
            for sub in subscriptions
            if isinstance(sub, dict) and str(sub.get("enabled", True)).lower() in {"false", "0", "no"}
        ]
        lines.append(f"订阅状态：{len(subscriptions) - len(paused)} 个运行，{len(paused)} 个暂停")
    if items:
        lines.append("最近文章：")
        for idx, item in enumerate(items[:6], 1):
            if not isinstance(item, dict):
                continue
            published = item.get("published_at") or item.get("created_at") or item.get("updated_at")
            lines.append(f"{idx}. [{source_name(item)}] {short(entry_title(item), 42)}（{fmt_time(published)}）")
    else:
        lines.append("最近文章：暂无")
    return "\n".join(lines)


def render_tasks(data: dict[str, Any]) -> str:
    notify = data.get("notify") or {}
    jobs = notify.get("job_details") or notify.get("configured_jobs") or []
    jobs = jobs if isinstance(jobs, list) else []
    enabled = [job for job in jobs if isinstance(job, dict) and job.get("enabled", True)]
    today = today_jobs(notify)
    errors = [
        job
        for job in jobs
        if isinstance(job, dict) and (job.get("status") or {}).get("last_status") in {"error", "timeout"}
    ]
    recent_candidates = [
        job
        for job in jobs
        if isinstance(job, dict)
        and ((job.get("status") or {}).get("last_finished_at") or (job.get("status") or {}).get("last_started_at"))
    ]
    recent = sorted(
        recent_candidates,
        key=lambda job: (parse_dt((job.get("status") or {}).get("last_finished_at")) or datetime.min.replace(tzinfo=SHANGHAI)),
        reverse=True,
    )
    lines = [
        "⏱️ 定时任务",
        f"配置：{len(jobs)} 个，启用：{len(enabled)} 个，今日触发：{len(today)} 个，异常：{len(errors)} 个",
    ]
    if errors:
        lines.append("需要关注：")
        for job in errors[:5]:
            st = job.get("status") or {}
            lines.append(f"- {job_name(job)}：{status_text(st.get('last_status'))}，{short(st.get('last_error'), 40)}")
    if recent:
        lines.append("最近完成：")
        for job in recent[:10]:
            st = job.get("status") or {}
            stamp = st.get("last_finished_at") or st.get("last_started_at")
            lines.append(f"- {fmt_time(stamp)} {job_name(job)}：{status_text(st.get('last_status'))}")
    else:
        lines.append("最近完成：暂无记录")
    return "\n".join(lines)


def render_today(data: dict[str, Any]) -> str:
    system = data.get("system") or {}
    sidecars = data.get("sidecars") or {}
    notify = data.get("notify") or {}
    articles = (data.get("articles") or {}).get("items") or []
    rows = lof_rows(data.get("lof") or {})
    high = [row for row in rows if (num(row.get("rt_premium_pct")) or 0) >= 5]
    side_summary = sidecars.get("summary") or {}
    mem = (system.get("memory") or {})
    jobs = today_jobs(notify)
    errors = [
        job
        for job in (notify.get("job_details") or notify.get("configured_jobs") or [])
        if isinstance(job, dict) and (job.get("status") or {}).get("last_status") in {"error", "timeout"}
    ]
    lines = [
        "🧭 今日摘要",
        f"时间：{now_shanghai().strftime('%Y-%m-%d %H:%M')}（东八区）",
        f"系统：内存 {mem.get('used_mb', '-')} / {mem.get('total_mb', '-')} MB，服务 {side_summary.get('healthy', 0)} / {side_summary.get('total', 0)} 正常",
        f"任务：今日触发 {len(jobs)} 个，异常 {len(errors)} 个",
        f"文章：今日 {len(articles)} 篇",
        f"LOF：高溢价 {len(high)} 只",
        "",
        "需要你看：",
    ]
    attention: list[str] = []
    if errors:
        for job in errors[:3]:
            attention.append(f"定时任务异常：{job_name(job)}")
    bad_services = [service_name(item) for item in sidecars.get("items", []) if isinstance(item, dict) and not item.get("ok")]
    for name in bad_services[:3]:
        attention.append(f"服务异常：{name}")
    for row in top_lof_rows(high, 3):
        attention.append(f"LOF 高溢价：{row.get('code')} {short(row.get('name'), 12)} {pct(row.get('rt_premium_pct'))}")
    if articles:
        for item in articles[:3]:
            if isinstance(item, dict):
                attention.append(f"新文章：[{source_name(item)}] {short(entry_title(item), 34)}")
    if not attention:
        attention.append("暂无硬异常，今天可以慢慢看。")
    lines.extend(f"- {item}" for item in attention[:8])
    return "\n".join(lines)


def render_decision(data: dict[str, Any]) -> str:
    system = data.get("system") or {}
    sidecars = data.get("sidecars") or {}
    notify = data.get("notify") or {}
    articles = (data.get("articles") or {}).get("items") or []
    rows = lof_rows(data.get("lof") or {})
    mem = system.get("memory") or {}
    mem_pct = num(mem.get("used_pct")) or 0
    bad_services = [service_name(item) for item in sidecars.get("items", []) if isinstance(item, dict) and not item.get("ok")]
    jobs = notify.get("job_details") or notify.get("configured_jobs") or []
    errors = [
        job
        for job in jobs
        if isinstance(job, dict) and (job.get("status") or {}).get("last_status") in {"error", "timeout"}
    ]
    lof_candidates = [
        row
        for row in rows
        if (num(row.get("rt_premium_pct")) or 0) >= 5
        and not row.get("suspended")
        and (num(row.get("amount_wan")) or 0) >= 50
    ]

    actions: list[str] = []
    if bad_services:
        actions.append("先处理服务异常：" + "、".join(bad_services[:3]))
    if errors:
        actions.append("看一眼定时任务错误：" + "、".join(job_name(job) for job in errors[:3]))
    if mem_pct >= 75:
        actions.append(f"内存偏高（{mem_pct:.0f}%），先别加常驻服务。")
    elif mem_pct >= 55:
        actions.append(f"内存中等（{mem_pct:.0f}%），新增能力继续优先按需运行。")
    else:
        actions.append(f"内存健康（{mem_pct:.0f}%），当前架构可以继续轻量扩展。")
    if lof_candidates:
        top = top_lof_rows(lof_candidates, 1)[0]
        actions.append(f"LOF 可重点看：{top.get('code')} {short(top.get('name'), 12)} 实时 {pct(top.get('rt_premium_pct'))}")
    if articles:
        first = articles[0]
        if isinstance(first, dict):
            actions.append(f"文章先看：[{source_name(first)}] {short(entry_title(first), 34)}")
    actions.append("遇到网页链接，直接发“这个值得看吗 + 链接”或“收一下 + 链接”。")

    lines = [
        "🧠 当前决策建议",
        f"时间：{now_shanghai().strftime('%Y-%m-%d %H:%M')}（东八区）",
        f"依据：服务 {len(bad_services)} 个异常；任务 {len(errors)} 个异常；文章 {len(articles)} 篇；LOF 候选 {len(lof_candidates)} 只。",
        "建议动作：",
    ]
    lines.extend(f"- {item}" for item in actions[:8])
    return "\n".join(lines)


def refresh_rss(yes: bool) -> str:
    if not yes:
        return "这是会触发抓取的动作。请在用户明确要求刷新时运行：ops_summary.py refresh-rss --yes"
    result = post_json("/rss/api/refresh-all", {})
    ok = result.get("ok", True)
    return "RSS 刷新已触发。" if ok else f"RSS 刷新返回异常：{short(result, 120)}"


def refresh_lof(yes: bool) -> str:
    if not yes:
        return "这是会触发 LOF 后台刷新的动作。请在用户明确要求刷新时运行：ops_summary.py refresh-lof --yes"
    result = post_json("/api/trigger", {"tag": "手动刷新"})
    if result.get("queued") or result.get("ok", True):
        return f"LOF 刷新已排队：{result.get('tag', '手动刷新')}"
    return f"LOF 刷新返回异常：{short(result, 120)}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Nanobot personal ops assistant summaries")
    parser.add_argument(
        "command",
        choices=["menu", "today", "system", "lof", "articles", "tasks", "decision", "refresh-rss", "refresh-lof"],
    )
    parser.add_argument("--yes", action="store_true", help="confirm mutating refresh action")
    args = parser.parse_args()

    try:
        if args.command == "menu":
            print(render_menu())
        elif args.command == "refresh-rss":
            print(refresh_rss(args.yes))
        elif args.command == "refresh-lof":
            print(refresh_lof(args.yes))
        else:
            data = load_bundle()
            if args.command == "today":
                print(render_today(data))
            elif args.command == "system":
                print(render_system(data))
            elif args.command == "lof":
                print(render_lof(data))
            elif args.command == "articles":
                print(render_articles(data))
            elif args.command == "tasks":
                print(render_tasks(data))
            elif args.command == "decision":
                print(render_decision(data))
    except Exception as exc:  # noqa: BLE001 - QQ output should show a short actionable error.
        print(f"能力菜单脚本失败：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
