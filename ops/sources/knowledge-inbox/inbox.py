#!/usr/bin/env python3
"""Lightweight knowledge inbox and decision packet tool for Nanobot."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

SHANGHAI = timezone(timedelta(hours=8))
DATA_DIR = Path(os.environ.get("NANOBOT_INBOX_DIR", "/root/.nanobot/data/knowledge-inbox"))
ITEMS_FILE = DATA_DIR / "items.json"
MD_DIR = DATA_DIR / "markdown"
MAX_FETCH_BYTES = 2_000_000
TIMEOUT_SECS = 15
LLM_TIMEOUT_SECS = float(os.environ.get("NANOBOT_INBOX_LLM_TIMEOUT_SECS", "14"))
LLM_SETTINGS_PATH = Path(
    os.environ.get(
        "NANOBOT_INBOX_LLM_SETTINGS",
        "/root/.nanobot/workspace/wechat_rss_service/settings.json",
    )
)
USER_AGENT = "NanobotKnowledgeInbox/1.0 (+local personal assistant)"
WECHAT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
WECHAT_HOSTS = {"mp.weixin.qq.com"}
WECHAT_ENV_MARKERS = ("环境异常", "当前环境异常", "完成验证后即可继续访问", "去验证")

INTEREST_KEYWORDS = {
    "ai", "llm", "agent", "openai", "claude", "gemini", "rust", "python", "nanobot",
    "sidecar", "podman", "k8s", "k3s", "memory", "内存", "服务器", "自动化", "工具",
    "基金", "lof", "qdii", "溢价", "套利", "美股", "市场", "投资", "经济",
    "效率", "认知", "决策", "系统", "长期", "风险", "职业", "人生",
}
AD_KEYWORDS = {
    "广告", "推广", "赞助", "优惠", "折扣", "返现", "扫码", "领取", "课程", "训练营", "社群",
    "付费", "下单", "购买", "咨询", "私域", "带货", "种草", "招商", "加盟", "限时",
}


def now_local() -> datetime:
    return datetime.now(SHANGHAI)


def ensure_dirs() -> None:
    MD_DIR.mkdir(parents=True, exist_ok=True)


def load_items() -> dict[str, dict[str, Any]]:
    if not ITEMS_FILE.exists():
        return {}
    try:
        data = json.loads(ITEMS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    if isinstance(data, list):
        return {str(v.get("id")): v for v in data if isinstance(v, dict) and v.get("id")}
    return {}


def save_items(items: dict[str, dict[str, Any]]) -> None:
    ensure_dirs()
    tmp = ITEMS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(ITEMS_FILE)


def clean_ws(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"[\t\r\f\v ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def short(text: Any, limit: int = 80) -> str:
    s = clean_ws(str(text or "")).replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def valid_url(value: str) -> str:
    url = (value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("只支持 http/https URL")
    return url


def is_wechat_article_url(url: str) -> bool:
    parsed = urlparse(url or "")
    return parsed.netloc.lower() in WECHAT_HOSTS


def looks_like_wechat_env_block(title: str, markdown: str) -> bool:
    text = clean_ws(f"{title}\n{markdown}")
    return "环境异常" in text and any(marker in text for marker in WECHAT_ENV_MARKERS)


def request_headers_for_url(url: str) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.5",
    }
    if is_wechat_article_url(url):
        # WeChat blocks the custom bot UA with an environment check page. A normal
        # browser UA returns the article HTML for public links, which we then parse
        # locally into Markdown.
        headers.update({
            "User-Agent": WECHAT_USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://mp.weixin.qq.com/",
        })
    return headers


def load_free_longcat_settings() -> dict[str, str] | None:
    if os.environ.get("NANOBOT_INBOX_LLM_ENABLED", "1").strip().lower() in {"0", "false", "off", "no"}:
        return None
    try:
        raw = json.loads(LLM_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    llm = raw.get("llm") if isinstance(raw.get("llm"), dict) else raw
    if not isinstance(llm, dict) or not llm.get("enabled", False):
        return None
    api_base = str(llm.get("api_base") or "").strip()
    api_key = str(llm.get("api_key") or "").strip()
    model = str(llm.get("model") or "").strip()
    if not api_base or not api_key or not model:
        return None
    if "longcat" not in api_base.lower() or "longcat-flash-lite" not in model.lower():
        return None
    return {"api_base": api_base, "api_key": api_key, "model": model}


def chat_completions_url(api_base: str) -> str:
    url = api_base.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    return url


def plain_markdown_for_summary(markdown: str, limit: int = 7000) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown or "")
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[#>*_`\-]+", " ", text)
    text = clean_ws(text)
    return text[:limit]


def summarize_with_longcat(title: str, markdown: str) -> str:
    settings = load_free_longcat_settings()
    if not settings:
        return ""
    body = plain_markdown_for_summary(markdown)
    if len(body) < 600:
        return ""
    prompt = (
        "请用中文为下面文章做一个给个人知识收件箱看的摘要。\n"
        "要求：3条短 bullet；不要复述链接；不要输出标题；总字数控制在180字以内；"
        "重点说明核心观点、为什么值得看、我可以怎么用。\n\n"
        f"标题：{title}\n\n正文：\n{body}"
    )
    payload = {
        "model": settings["model"],
        "messages": [
            {"role": "system", "content": "你是一个克制、准确的中文阅读摘要助手。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 260,
        "stream": False,
    }
    req = Request(
        chat_completions_url(settings["api_base"]),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings['api_key']}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=LLM_TIMEOUT_SECS) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return ""
    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    return clean_ws(content)[:360]


class MarkdownHTMLParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.parts: list[str] = []
        self.skip_depth = 0
        self.in_title = False
        self.link_href: str | None = None
        self.link_text: list[str] = []

    def _append(self, text: str) -> None:
        text = clean_ws(text)
        if not text:
            return
        if self.link_href is not None:
            self.link_text.append(text)
        else:
            if self.parts and not self.parts[-1].endswith(("\n", " ")):
                self.parts.append(" ")
            self.parts.append(text)

    def _newline(self, count: int = 1) -> None:
        if not self.parts:
            return
        joined_tail = "".join(self.parts[-3:])
        if joined_tail.endswith("\n" * count):
            return
        self.parts.append("\n" * count)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {k.lower(): v or "" for k, v in attrs}
        if tag in {"script", "style", "noscript", "svg", "canvas"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = True
            return
        if tag == "meta":
            key = (attrs_map.get("property") or attrs_map.get("name") or "").lower()
            val = attrs_map.get("content") or ""
            if key and val:
                self.meta[key] = clean_ws(val)
            return
        if tag in {"h1", "h2", "h3"}:
            self._newline(2)
            self.parts.append("## " if tag != "h1" else "# ")
        elif tag in {"p", "div", "section", "article", "blockquote"}:
            self._newline(2)
        elif tag == "li":
            self._newline(1)
            self.parts.append("- ")
        elif tag == "br":
            self._newline(1)
        elif tag == "a":
            href = attrs_map.get("href", "").strip()
            self.link_href = urljoin(self.base_url, href) if href else ""
            self.link_text = []
        elif tag == "img":
            src = attrs_map.get("src", "").strip()
            alt = clean_ws(attrs_map.get("alt", "图片")) or "图片"
            if src:
                self._append(f"![{alt}]({urljoin(self.base_url, src)})")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas"}:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = False
            return
        if tag == "a" and self.link_href is not None:
            text = clean_ws(" ".join(self.link_text))
            href = self.link_href
            self.link_href = None
            self.link_text = []
            if text and href:
                self._append(f"[{text}]({href})")
            elif text:
                self._append(text)
            return
        if tag in {"p", "div", "section", "article", "blockquote", "li", "h1", "h2", "h3"}:
            self._newline(2)

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
            return
        self._append(data)

    def result(self) -> tuple[str, str, str]:
        title = clean_ws(" ".join(self.title_parts))
        title = self.meta.get("og:title") or self.meta.get("twitter:title") or title
        desc = self.meta.get("description") or self.meta.get("og:description") or self.meta.get("twitter:description") or ""
        markdown = clean_ws("".join(self.parts))
        markdown = re.sub(r"\n{3,}", "\n\n", markdown)
        return title, clean_ws(desc), markdown.strip()


@dataclass
class FetchedPage:
    url: str
    final_url: str
    title: str
    description: str
    markdown: str
    content_type: str


def fetch_url(url: str) -> FetchedPage:
    url = valid_url(url)
    req = Request(url, headers=request_headers_for_url(url))
    try:
        with urlopen(req, timeout=TIMEOUT_SECS) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(MAX_FETCH_BYTES + 1)
            final_url = resp.geturl()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"抓取失败：{exc}") from exc
    if len(raw) > MAX_FETCH_BYTES:
        raw = raw[:MAX_FETCH_BYTES]
    charset = "utf-8"
    match = re.search(r"charset=([\w.-]+)", content_type, flags=re.I)
    if match:
        charset = match.group(1)
    text = raw.decode(charset, errors="replace")
    if "html" in content_type.lower() or "<html" in text[:500].lower():
        parser = MarkdownHTMLParser(final_url)
        parser.feed(text)
        title, desc, markdown = parser.result()
    else:
        title, desc, markdown = "", "", clean_ws(text)
    if not title:
        title = short(markdown.splitlines()[0] if markdown else final_url, 90)
    return FetchedPage(url=url, final_url=final_url, title=title, description=desc, markdown=markdown, content_type=content_type)


def item_id_for_url(url: str) -> str:
    parsed = urlparse(url)
    host = re.sub(r"[^a-zA-Z0-9]+", "-", parsed.netloc.lower()).strip("-")[:18]
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{now_local().strftime('%Y%m%d')}-{host}-{digest}"


def extract_keywords(text: str, limit: int = 10) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+.-]{2,}|[\u4e00-\u9fff]{2,}", text.lower())
    counts: dict[str, int] = {}
    stop = {"https", "http", "com", "www", "the", "and", "for", "with", "this", "that", "from", "一个", "我们", "他们", "这个", "不是", "什么", "如果", "因为", "所以", "但是", "然后", "可以", "没有"}
    for token in tokens:
        if token in stop or len(token) > 24:
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)
    return [k for k, _ in ranked[:limit]]


def extract_links(markdown: str, limit: int = 8) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for text, url in re.findall(r"\[([^\]]{1,80})\]\((https?://[^)\s]+)\)", markdown):
        links.append({"text": clean_ws(text), "url": url})
        if len(links) >= limit:
            break
    return links


def first_sentences(text: str, limit: int = 220) -> str:
    body = re.sub(r"!?\[[^\]]*\]\([^)]*\)", "", text)
    body = re.sub(r"[#>*_`\-]+", " ", body)
    body = clean_ws(body).replace("\n", " ")
    if not body:
        return ""
    parts = re.split(r"(?<=[。！？.!?])\s+", body)
    out = ""
    for part in parts:
        if not part:
            continue
        if len(out) + len(part) > limit:
            break
        out = (out + " " + part).strip()
    return short(out or body, limit)


def score_page(title: str, description: str, markdown: str) -> tuple[int, str, list[str]]:
    text = f"{title}\n{description}\n{markdown[:4000]}".lower()
    ascii_tokens = set(re.findall(r"[a-z][a-z0-9_+.-]{1,}", text))

    def keyword_hit(keyword: str) -> bool:
        low = keyword.lower()
        if re.fullmatch(r"[a-z0-9_+.-]+", low):
            return low in ascii_tokens
        return low in text

    score = 45
    reasons: list[str] = []
    matched_interest = sorted({kw for kw in INTEREST_KEYWORDS if keyword_hit(kw)})
    matched_ads = sorted({kw for kw in AD_KEYWORDS if keyword_hit(kw)})
    if matched_interest:
        add = min(25, 5 + len(matched_interest) * 3)
        score += add
        reasons.append("命中你的长期关注：" + "、".join(matched_interest[:6]))
    content_len = len(clean_ws(markdown))
    if content_len >= 2500:
        score += 10
        reasons.append("内容足够长，可能有信息密度")
    elif content_len < 500:
        score -= 12
        reasons.append("正文偏短，可能只是入口页或摘要")
    link_count = len(re.findall(r"\]\(https?://", markdown))
    if link_count >= 5:
        score += 5
        reasons.append("包含较多外链，适合做资料入口")
    if matched_ads:
        score -= min(35, 12 + len(matched_ads) * 4)
        reasons.append("疑似营销/广告信号：" + "、".join(matched_ads[:6]))
    if re.search(r"404|not found|access denied|forbidden", title.lower() + markdown[:300].lower()):
        score -= 30
        reasons.append("页面可能不可读或权限受限")
    score = max(0, min(100, score))
    if score >= 75:
        label = "值得优先看"
    elif score >= 58:
        label = "可以稍后看"
    elif score >= 42:
        label = "只需扫一眼"
    else:
        label = "大概率可跳过"
    if not reasons:
        reasons.append("没有明显强信号，建议按标题兴趣决定")
    return score, label, reasons


def write_markdown(item: dict[str, Any], markdown: str) -> Path:
    path = MD_DIR / f"{item['id']}.md"
    header = [
        f"# {item.get('title') or 'Untitled'}",
        "",
        f"- URL: {item.get('url')}",
        f"- Final URL: {item.get('final_url')}",
        f"- Captured: {item.get('captured_at')}",
        f"- Decision: {item.get('decision_label')} ({item.get('decision_score')}/100)",
        "",
        "---",
        "",
    ]
    path.write_text("\n".join(header) + markdown.strip() + "\n", encoding="utf-8")
    return path


def capture(url: str, note: str = "", tags: list[str] | None = None, force: bool = False) -> dict[str, Any]:
    ensure_dirs()
    page = fetch_url(url)
    wechat_blocked = is_wechat_article_url(page.final_url or page.url) and looks_like_wechat_env_block(page.title, page.markdown)
    if wechat_blocked:
        raise RuntimeError("微信文章解析失败：微信返回环境验证页，未保存空文章")
    items = load_items()
    existing = next((v for v in items.values() if v.get("url") == page.url or v.get("final_url") == page.final_url), None)
    item_id = existing.get("id") if existing and not force else item_id_for_url(page.final_url or page.url)
    score, label, reasons = score_page(page.title, page.description, page.markdown)
    keywords = extract_keywords(f"{page.title}\n{page.description}\n{page.markdown}")
    extractive_summary = first_sentences(page.description or page.markdown)
    llm_summary = summarize_with_longcat(page.title, page.markdown)
    item = {
        "id": item_id,
        "url": page.url,
        "final_url": page.final_url,
        "host": urlparse(page.final_url or page.url).netloc,
        "title": page.title,
        "description": page.description,
        "summary": llm_summary or extractive_summary,
        "summary_source": "longcat_free" if llm_summary else "extractive",
        "extractive_summary": extractive_summary,
        "captured_at": now_local().isoformat(timespec="seconds"),
        "content_type": page.content_type,
        "content_chars": len(page.markdown),
        "decision_score": score,
        "decision_label": label,
        "decision_reasons": reasons,
        "keywords": keywords,
        "links": extract_links(page.markdown),
        "note": note,
        "tags": tags or [],
        "source_status": "ok",
    }
    md_path = write_markdown(item, page.markdown)
    item["markdown_path"] = str(md_path)
    items[item_id] = item
    save_items(items)
    return item


def sorted_items(limit: int = 10) -> list[dict[str, Any]]:
    items = list(load_items().values())
    items.sort(key=lambda x: str(x.get("captured_at") or ""), reverse=True)
    return items[:limit]


def find_item(ref: str) -> dict[str, Any] | None:
    items = load_items()
    if ref in items:
        return items[ref]
    for item in items.values():
        if str(item.get("id", "")).startswith(ref):
            return item
    return None


def resolve_item_key(ref: str, items: dict[str, dict[str, Any]]) -> str | None:
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref in items:
        return ref
    exact = [key for key, item in items.items() if str(item.get("id") or "") == ref]
    if len(exact) == 1:
        return exact[0]
    matches = sorted({
        key
        for key, item in items.items()
        if key.startswith(ref) or str(item.get("id") or "").startswith(ref)
    })
    if len(matches) == 1:
        return matches[0]
    return None


def safe_unlink_markdown(item: dict[str, Any]) -> bool:
    raw = str(item.get("markdown_path") or "").strip()
    if not raw:
        return False
    path = Path(raw)
    if not path.is_absolute():
        path = DATA_DIR / path
    try:
        resolved = path.resolve(strict=True)
        data_root = DATA_DIR.resolve(strict=True)
    except OSError:
        return False
    if not resolved.is_file() or data_root not in (resolved, *resolved.parents):
        return False
    try:
        resolved.unlink()
        return True
    except OSError:
        return False


def delete_item(ref: str, keep_markdown: bool = False) -> tuple[dict[str, Any], bool]:
    items = load_items()
    key = resolve_item_key(ref, items)
    if key is None:
        raise ValueError(f"没找到唯一匹配的收件箱条目：{ref}")
    item = items.pop(key)
    save_items(items)
    markdown_deleted = False if keep_markdown else safe_unlink_markdown(item)
    return item, markdown_deleted


def render_item(item: dict[str, Any], verbose: bool = False) -> str:
    lines = [
        f"📥 已入收件箱：{item.get('title') or '未命名'}",
        f"ID：{item.get('id')}",
        f"判断：{item.get('decision_label')}（{item.get('decision_score')}/100）",
    ]
    if item.get("summary"):
        summary = str(item.get("summary") or "").strip()
        if "\n" in summary:
            lines.append("摘要：\n" + summary)
        else:
            lines.append(f"摘要：{summary}")
    reasons = item.get("decision_reasons") or []
    if reasons:
        lines.append("理由：" + "；".join(str(x) for x in reasons[:3]))
    return "\n".join(lines)


def render_list(limit: int) -> str:
    items = sorted_items(limit)
    if not items:
        return "📭 收件箱还是空的。发一个链接并说“收一下”就能存起来。"
    lines = [f"📚 最近收件箱（{len(items)} 条）"]
    for idx, item in enumerate(items, 1):
        lines.append(f"{idx}. {short(item.get('title'), 36)}")
        lines.append(f"   {item.get('decision_label')} {item.get('decision_score')}/100 · {item.get('id')}")
    return "\n".join(lines)


def render_read(ref: str, chars: int = 900) -> str:
    item = find_item(ref)
    if not item:
        return f"没找到这个收件箱条目：{ref}"
    path = Path(str(item.get("markdown_path") or ""))
    body = ""
    if path.exists():
        body = path.read_text(encoding="utf-8", errors="replace")
    return "\n".join([
        render_item(item, verbose=True),
        "",
        "预览：",
        short(body, chars),
    ])


def render_delete(ref: str, keep_markdown: bool = False) -> str:
    item, markdown_deleted = delete_item(ref, keep_markdown=keep_markdown)
    lines = [
        f"🗑️ 已删除收件箱条目：{item.get('title') or '未命名'}",
        f"ID：{item.get('id')}",
    ]
    if keep_markdown:
        lines.append("Markdown：已保留")
    else:
        lines.append("Markdown：" + ("已删除" if markdown_deleted else "未找到或已跳过"))
    return "\n".join(lines)


def render_decide(target: str, question: str = "") -> str:
    if re.match(r"^https?://", target or ""):
        item = capture(target)
    else:
        item = find_item(target)
        if not item:
            return f"没找到这个条目：{target}"
    lines = [
        f"🧠 决策包：{item.get('title') or '未命名'}",
        f"结论：{item.get('decision_label')}（{item.get('decision_score')}/100）",
    ]
    if question:
        lines.append(f"你的问题：{question}")
    if item.get("summary"):
        lines.append(f"核心内容：{item.get('summary')}")
    reasons = item.get("decision_reasons") or []
    if reasons:
        lines.append("依据：")
        lines.extend(f"- {r}" for r in reasons[:4])
    score = int(item.get("decision_score") or 0)
    if score >= 75:
        action = "今天优先读，读完可以让 nanobot 帮你提炼行动项。"
    elif score >= 58:
        action = "先放待读，碎片时间看；不需要立刻打断当前事情。"
    elif score >= 42:
        action = "扫标题和小结即可，除非它正好回答你手头的问题。"
    else:
        action = "可以先跳过，除非你就是想验证它为什么低价值。"
    lines.append(f"建议动作：{action}")
    links = item.get("links") or []
    if links:
        lines.append("原文链接保留：")
        for link in links[:3]:
            lines.append(f"- [{short(link.get('text'), 28)}]({link.get('url')})")
    return "\n".join(lines)


def render_brief(limit: int = 8) -> str:
    items = sorted_items(limit)
    if not items:
        return "📭 暂无待读材料。"
    priority = [x for x in items if int(x.get("decision_score") or 0) >= 75]
    maybe = [x for x in items if 58 <= int(x.get("decision_score") or 0) < 75]
    lines = ["🧺 待读决策简报", f"最近 {len(items)} 条；优先 {len(priority)} 条，稍后 {len(maybe)} 条"]
    if priority:
        lines.append("先看：")
        for item in priority[:3]:
            lines.append(f"- {short(item.get('title'), 38)}（{item.get('decision_score')}/100）")
    if maybe:
        lines.append("稍后：")
        for item in maybe[:3]:
            lines.append(f"- {short(item.get('title'), 38)}（{item.get('decision_score')}/100）")
    low = [x for x in items if int(x.get("decision_score") or 0) < 58]
    if low:
        lines.append(f"可跳过/扫一眼：{len(low)} 条")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Nanobot knowledge inbox")
    sub = parser.add_subparsers(dest="command", required=True)

    p_capture = sub.add_parser("capture")
    p_capture.add_argument("url")
    p_capture.add_argument("--note", default="")
    p_capture.add_argument("--tag", action="append", default=[])
    p_capture.add_argument("--force", action="store_true")

    p_decide = sub.add_parser("decide")
    p_decide.add_argument("target")
    p_decide.add_argument("--question", default="")

    p_list = sub.add_parser("list")
    p_list.add_argument("--limit", type=int, default=8)

    p_read = sub.add_parser("read")
    p_read.add_argument("ref")
    p_read.add_argument("--chars", type=int, default=900)

    p_delete = sub.add_parser("delete")
    p_delete.add_argument("ref")
    p_delete.add_argument("--keep-markdown", action="store_true")

    p_brief = sub.add_parser("brief")
    p_brief.add_argument("--limit", type=int, default=8)

    args = parser.parse_args()
    try:
        if args.command == "capture":
            item = capture(args.url, note=args.note, tags=args.tag, force=args.force)
            print(render_item(item))
        elif args.command == "decide":
            print(render_decide(args.target, question=args.question))
        elif args.command == "list":
            print(render_list(max(1, min(args.limit, 30))))
        elif args.command == "read":
            print(render_read(args.ref, chars=max(200, min(args.chars, 5000))))
        elif args.command == "delete":
            print(render_delete(args.ref, keep_markdown=args.keep_markdown))
        elif args.command == "brief":
            print(render_brief(max(1, min(args.limit, 30))))
    except Exception as exc:  # noqa: BLE001 - QQ should receive a compact failure.
        print(f"知识收件箱失败：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
