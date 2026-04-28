"""Small shared helpers for Nanobot ops skill scripts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SHANGHAI = timezone(timedelta(hours=8))
MISSING = object()


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def short(text: Any, limit: int = 52) -> str:
    s = str(text or "").strip().replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "..."


def parse_dt(value: Any, default_tz=SHANGHAI) -> datetime | None:
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
            dt = dt.replace(tzinfo=default_tz)
        return dt.astimezone(SHANGHAI)
    return None


def fmt_time(value: Any, pattern: str = "%H:%M", default: str = "-") -> str:
    dt = parse_dt(value)
    return dt.strftime(pattern) if dt else default


class JsonHttpClient:
    def __init__(self, base_urls: list[str], timeout: int = 8, post_timeout: int | None = None):
        self.base_urls = [base.rstrip("/") for base in base_urls if base]
        self.timeout = timeout
        self.post_timeout = post_timeout or timeout

    def urls(self, path: str) -> list[str]:
        if path.startswith("http"):
            return [path]
        return [base + path for base in self.base_urls]

    def request(
        self,
        path: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        expect_json: bool = True,
        default: Any = MISSING,
    ) -> Any:
        data = None
        headers = {"Accept": "application/json" if expect_json else "*/*"}
        timeout = self.timeout
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
            timeout = self.post_timeout

        last_exc: Exception | None = None
        for url in self.urls(path):
            req = Request(url, data=data, method=method, headers=headers)
            try:
                with urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if expect_json else raw
            except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_exc = exc
                continue
        if default is not MISSING:
            return default
        raise RuntimeError(f"请求失败：{path} - {last_exc}") from last_exc

    def get_json(self, path: str, default: Any = MISSING) -> Any:
        return self.request(path, default=default)

    def post_json(self, path: str, payload: dict[str, Any] | None = None, default: Any = MISSING) -> Any:
        return self.request(path, method="POST", payload=payload or {}, default=default)

    def get_text(self, path: str, default: Any = MISSING) -> str:
        return self.request(path, expect_json=False, default=default)
