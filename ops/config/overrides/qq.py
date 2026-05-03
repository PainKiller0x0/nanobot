"""QQ channel implementation using botpy SDK.

Inbound:
- Parse QQ botpy messages (C2C / Group)
- Download attachments to media dir using chunked streaming write (memory-safe)
- Publish to Nanobot bus via BaseChannel._handle_message()
- Content includes a clear, actionable "Received files:" list with local paths

Outbound:
- Send attachments (msg.media) first via QQ rich media API (base64 upload + msg_type=7)
- Then send text (plain or markdown)
- msg.media supports local paths, file:// paths, and http(s) URLs

Notes:
- QQ restricts many audio/video formats. We conservatively classify as image vs file.
- Attachment structures differ across botpy versions; we try multiple field candidates.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote, urlparse

import aiohttp
from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base
from nanobot.security.network import validate_url_target
from nanobot.utils.helpers import split_message

try:
    from nanobot.config.paths import get_media_dir
except Exception:  # pragma: no cover
    get_media_dir = None  # type: ignore

try:
    import botpy
    from botpy.http import Route

    QQ_AVAILABLE = True
except ImportError:  # pragma: no cover
    QQ_AVAILABLE = False
    botpy = None
    Route = None

if TYPE_CHECKING:
    from botpy.message import BaseMessage, C2CMessage, GroupMessage
    from botpy.types.message import Media


# QQ rich media file_type: 1=image, 4=file
# (2=voice, 3=video are restricted; we only use image vs file)
QQ_FILE_TYPE_IMAGE = 1
QQ_FILE_TYPE_FILE = 4

_IMAGE_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
    ".ico",
    ".svg",
}

# Replace unsafe characters with "_", keep Chinese and common safe punctuation.
_SAFE_NAME_RE = re.compile(r"[^\w.\-()\[\]（）【】\u4e00-\u9fff]+", re.UNICODE)
_CN_WECHAT = "\u5fae\u4fe1"
_CN_OFFICIAL = "\u516c\u4f17\u53f7"
_CN_ARTICLE = "\u6587\u7ae0"
_CN_POST = "\u63a8\u6587"
_CN_IN_ARTICLE = "\u6587\u4e2d"
_WECHAT_QUESTION_HINTS = (
    "\u662f\u5426",
    "\u6709\u6ca1\u6709",
    "\u63d0\u5230",
    "\u8bb2\u4e86\u4ec0\u4e48",
    "\u662f\u4ec0\u4e48",
    "\u5565",
    "\u603b\u7ed3",
    "\u6982\u62ec",
)
_WECHAT_TITLE_HINTS = (
    "\u6700\u65b0\u7684\u6587\u7ae0\u540d",
    "\u6700\u65b0\u6587\u7ae0\u540d",
    "\u6700\u65b0\u6807\u9898",
    "\u6700\u65b0\u4e00\u7bc7",
    "\u6700\u65b0\u6587\u7ae0",
)
_YAGE_HINT = "\u9e2d\u54e5"
_YAGE_LATEST_HINTS = ("\u6700\u65b0", "\u53d1\u6211", "\u770b\u770b", "\u6765\u7bc7")
_YAGE_ARTICLE_HINTS = ("\u6587\u7ae0", "\u8981\u95fb", "\u624b\u8bb0", "yage")
_YAGE_RECENT_HINTS = (
    "\u6628\u5929",
    "\u6628\u665a",
    "\u4e0a\u4e00\u7bc7",
    "\u4e0a\u4e00\u671f",
    "\u4e0a\u671f",
    "\u8fd1\u671f",
)
_YAGE_ACTION_HINTS = (
    "\u7ed9\u6211",
    "\u53d1\u6211",
    "\u53d1\u4e00\u7bc7",
    "\u63a8\u9001",
    "\u63a8\u4e00\u4e0b",
    "\u6765\u4e00\u7bc7",
    "\u6765\u4e2a",
    "\u770b\u770b",
    "\u770b\u4e0b",
    "\u67e5\u4e00\u4e0b",
    "\u5e2e\u6211\u627e",
    "\u5e2e\u6211\u62ff",
)
_CN_NUM_MAP = {
    "\u4e00": 1,
    "\u4e8c": 2,
    "\u4e09": 3,
    "\u56db": 4,
    "\u4e94": 5,
    "\u516d": 6,
    "\u4e03": 7,
    "\u516b": 8,
    "\u4e5d": 9,
    "\u5341": 10,
    "\u4e24": 2,
}
_SIGNED_PAYLOAD_PREFIX = "NBRAW1-SHA256:"
_SIGNED_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SILENT_MARKER = "(NOOUTPUTKEEP_SILENT)"
_SILENT_MARKER_ALT = "(NO_OUTPUT_KEEP_SILENT)"
_SILENT_MARKER_RE = re.compile(r"[（(]\s*NO_?OUTPUT_?KEEP_?SILENT\s*[)）]", re.IGNORECASE)
_YAGE_ARTICLE_RE = re.compile(r"^\s*\[\u9e2d\u54e5 AI \u624b\u8bb0\]", re.MULTILINE)
_YAGE_CACHE_FILE = "/root/.nanobot/workspace/skills/news-curator/yage_cache.json"
_YAGE_URL_IN_LINK_RE = re.compile(r"\((https?://yage-ai\.kit\.com/posts/[^)\s]+)\)")
_YAGE_URL_BARE_RE = re.compile(r"https?://yage-ai\.kit\.com/posts/[^\s)\]]+")
_YAGE_DATE_IN_URL_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
_WECHAT_CACHE_FILE = "/root/.nanobot/workspace/skills/wechat-rss-sidecar/wechat_push_cache.json"
_WECHAT_ACK_MARKER_RE = re.compile(r"<!--\s*NBACK_WECHAT\s+sub:(\d+)\s+entry:(\d+)\s*-->")
_GENERIC_URL_RE = re.compile(r"https?://[^\s<>\]）)\"']+")
_INBOX_SPECIAL_HOSTS = ("mp.weixin.qq.com", "yage-ai.kit.com", "jintiankansha.me")


def _sanitize_filename(name: str) -> str:
    """Sanitize filename to avoid traversal and problematic chars."""
    name = (name or "").strip()
    name = Path(name).name
    name = _SAFE_NAME_RE.sub("_", name).strip("._ ")
    return name


def _is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in _IMAGE_EXTS


def _parse_qq_timestamp(ts: str | None) -> datetime | None:
    """Parse QQ API timestamp string to UTC datetime.

    QQ returns timestamps as Unix epoch in seconds (or ms), e.g. '1743890400'.
    """
    if not ts:
        return None
    try:
        # Try as numeric string (seconds or milliseconds)
        value = int(ts)
        # If value looks like milliseconds ( > 1e10 for year 1970+ ),
        # divide by 1000
        if value > 10**10:
            value //= 1000
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (ValueError, OSError):
        return None


def _guess_send_file_type(filename: str) -> int:
    """Conservative send type: images -> 1, else -> 4."""
    ext = Path(filename).suffix.lower()
    mime, _ = mimetypes.guess_type(filename)
    if ext in _IMAGE_EXTS or (mime and mime.startswith("image/")):
        return QQ_FILE_TYPE_IMAGE
    return QQ_FILE_TYPE_FILE


def _strip_silent_marker(text: str) -> str:
    cleaned = text or ""
    cleaned = _SILENT_MARKER_RE.sub("", cleaned)
    cleaned = cleaned.replace(_SILENT_MARKER, "").replace(_SILENT_MARKER_ALT, "")
    return cleaned.strip()


def _make_bot_class(channel: QQChannel) -> type[botpy.Client]:
    """Create a botpy Client subclass bound to the given channel."""
    intents = botpy.Intents(public_messages=True, direct_message=True)

    class _Bot(botpy.Client):
        def __init__(self):
            # Disable botpy's file log — nanobot uses loguru; default "botpy.log" fails on read-only fs
            super().__init__(intents=intents, ext_handlers=False)

        async def on_ready(self):
            logger.info("QQ bot ready: {}", self.robot.name)
            await channel._check_greeting_trigger()

        async def on_c2c_message_create(self, message: C2CMessage):
            await channel._on_message(message, is_group=False)

        async def on_group_at_message_create(self, message: GroupMessage):
            await channel._on_message(message, is_group=True)

    return _Bot


class QQConfig(Base):
    """QQ channel configuration using botpy SDK."""

    enabled: bool = False
    app_id: str = ""
    secret: str = ""
    allow_from: list[str] = Field(default_factory=list)
    msg_format: Literal["plain", "markdown"] = "plain"

    # Optional: directory to save inbound attachments. If empty, use nanobot get_media_dir("qq").
    media_dir: str = ""

    # Download tuning
    download_chunk_size: int = 1024 * 256  # 256KB
    download_max_bytes: int = 1024 * 1024 * 200  # 200MB safety limit

    # QQ can truncate/deny oversized text payloads. Split long replies into chunks.
    text_chunk_max_len: int = 1200

    # Optional immediate acknowledgement for inbound messages. Empty disables it.
    ack_message: str = ""

    # Signature validation alert reporting
    signature_alert_enabled: bool = True
    signature_alert_chat_id: str = ""

    # botpy logs timeout and returns None instead of raising; these knobs let us
    # retry passive replies with the same msg_seq while avoiding duplicate cron pushes.
    botpy_http_timeout_sec: float = 5.0
    send_retry_on_empty_response: bool = False
    send_retry_attempts: int = 1
    send_retry_delay_sec: float = 0.8


class QQChannel(BaseChannel):
    """QQ channel using botpy SDK with WebSocket connection."""

    name = "qq"
    display_name = "QQ"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return QQConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = QQConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: QQConfig = config

        self._client: botpy.Client | None = None
        self._http: aiohttp.ClientSession | None = None

        self._processed_ids: deque[str] = deque(maxlen=1000)
        self._msg_seq: int = 1  # used to avoid QQ API dedup
        self._chat_type_cache: dict[str, str] = {}

        self._media_root: Path = self._init_media_root()

    # ---------------------------
    # Lifecycle
    # ---------------------------

    def _init_media_root(self) -> Path:
        """Choose a directory for saving inbound attachments."""
        if self.config.media_dir:
            root = Path(self.config.media_dir).expanduser()
        elif get_media_dir:
            try:
                root = Path(get_media_dir("qq"))
            except Exception:
                root = Path.home() / ".nanobot" / "media" / "qq"
        else:
            root = Path.home() / ".nanobot" / "media" / "qq"

        root.mkdir(parents=True, exist_ok=True)
        logger.info("QQ media directory: {}", str(root))
        return root

    async def start(self) -> None:
        """Start the QQ bot with auto-reconnect loop."""
        if not QQ_AVAILABLE:
            logger.error("QQ SDK not installed. Run: pip install qq-botpy")
            return

        if not self.config.app_id or not self.config.secret:
            logger.error("QQ app_id and secret not configured")
            return

        self._running = True
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))

        self._client = _make_bot_class(self)()
        logger.info("QQ bot started (C2C & Group supported)")
        await self._run_bot()

    async def _run_bot(self) -> None:
        """Run the bot connection with auto-reconnect."""
        while self._running:
            try:
                await self._client.start(appid=self.config.app_id, secret=self.config.secret)
                # Bot connected successfully - greeting is now triggered in on_ready
            except Exception as e:
                logger.warning("QQ bot error: {}", e)
            if self._running:
                logger.info("Reconnecting QQ bot in 5 seconds...")
                await asyncio.sleep(5)

    async def _check_greeting_trigger(self) -> None:
        """Check for gateway restart greeting trigger and send a greeting."""
        from pathlib import Path
        flag_file = Path("/root/.nanobot/workspace/lof_monitor/.gateway_restart_flag")
        logger.debug("check_greeting: flag exists={}", flag_file.exists())
        if not flag_file.exists():
            return
        try:
            flag_file.unlink()
        except OSError:
            pass
        # 判断时间段
        from datetime import datetime
        h = datetime.now().hour
        if 5 <= h < 12:
            greeting = "早安 ☀️"
        elif 12 <= h < 18:
            greeting = "下午好 🌤️"
        elif 18 <= h < 23:
            greeting = "晚上好 🌙"
        else:
            greeting = "夜深了，早点休息 🌛"
        from nanobot.bus.events import OutboundMessage
        logger.info("check_greeting: sending greeting '{}'", greeting)
        await self.bus.publish_outbound(OutboundMessage(
            channel="qq", chat_id="965E0CA5AB52FBFC537A2E68A7349B9E",
            content=f"gateway 已上线 · {greeting}",
        ))

    def _extract_wechat_question(self, content: str) -> str | None:
        text = (content or "").strip()
        lower = text.lower()
        if not text:
            return None
        if _CN_WECHAT not in text and _CN_OFFICIAL not in text and "wechat" not in lower and "weixin" not in lower:
            return None
        if (
            _CN_ARTICLE not in text
            and _CN_POST not in text
            and _CN_IN_ARTICLE not in text
            and "article" not in lower
            and "post" not in lower
        ):
            return None
        if any(k in text for k in _WECHAT_QUESTION_HINTS):
            parts = re.split(r"[:\uFF1A]", text, maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                return parts[1].strip()
            return text
        if "?" in text or "\uFF1F" in text:
            return text
        return None

    async def _run_sidecar_json(self, args: list[str], timeout_sec: float = 30.0) -> dict[str, Any] | None:
        cmd = [
            "python3",
            "/root/.nanobot/workspace/skills/wechat-rss-sidecar/client.py",
            *args,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except TimeoutError:
            logger.warning("qq wechat guard timeout: {}", " ".join(cmd))
            return None
        except Exception as e:
            logger.warning("qq wechat guard exec failed: {} err={}", " ".join(cmd), e)
            return None

        out = (stdout or b"").decode("utf-8", errors="ignore").strip()
        err = (stderr or b"").decode("utf-8", errors="ignore").strip()
        if proc.returncode != 0:
            logger.warning(
                "qq wechat guard non-zero: rc={} cmd={} err={}",
                proc.returncode,
                " ".join(cmd),
                err,
            )
            return None
        if not out:
            logger.warning("qq wechat guard empty output: {}", " ".join(cmd))
            return None
        try:
            return json.loads(out)
        except Exception:
            logger.warning("qq wechat guard invalid json: cmd={} out_head={}", " ".join(cmd), out[:200])
            return None

    async def _run_yage_signed(
        self,
        timeout_sec: float = 45.0,
        *,
        nth: int | None = None,
        target_date: str | None = None,
        force_latest: bool = False,
    ) -> str | None:
        """Run yage checker with selector and return raw stdout."""
        args: list[str] = []
        if force_latest:
            args.append("--latest")
        if nth and nth > 1:
            args.extend(["--nth", str(nth)])
        if target_date:
            args.extend(["--date", target_date])
        arg_str = " ".join(args).strip()
        cmd = "cd /root/.nanobot/workspace/skills/news-curator && python3 yage_check.py"
        if arg_str:
            cmd = f"{cmd} {arg_str}"
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
            if proc.returncode != 0:
                logger.warning(
                    "yage latest script failed rc={} err={}",
                    proc.returncode,
                    (stderr or b"").decode("utf-8", "ignore")[:500],
                )
                return None
            return (stdout or b"").decode("utf-8", "ignore")
        except Exception as e:
            logger.warning("yage latest script execution failed: {}", e)
            return None

    async def _run_wechat_signed(
        self,
        subscription_id: int,
        timeout_sec: float = 45.0,
        *,
        force: bool = True,
    ) -> str | None:
        """Run wechat_push script and return raw stdout."""
        if subscription_id <= 0:
            return None
        cmd = (
            "cd /root/.nanobot/workspace/skills/wechat-rss-sidecar "
            "&& WECHAT_RSS_BASE_URL=http://wechat-rss-sidecar:8091 "
            f"python3 wechat_push.py --subscription-id {subscription_id}"
        )
        if force:
            cmd += " --force"
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
            if proc.returncode != 0:
                logger.warning(
                    "wechat signed script failed rc={} sub={} err={}",
                    proc.returncode,
                    subscription_id,
                    (stderr or b"").decode("utf-8", "ignore")[:500],
                )
                return None
            return (stdout or b"").decode("utf-8", "ignore")
        except Exception as e:
            logger.warning("wechat signed script execution failed sub={} err={}", subscription_id, e)
            return None

    @staticmethod
    def _cn_num_to_int(text: str) -> int | None:
        t = (text or "").strip()
        if not t:
            return None
        if t.isdigit():
            return int(t)
        if t in _CN_NUM_MAP:
            return _CN_NUM_MAP[t]
        if len(t) == 2 and t[0] == "\u5341" and t[1] in _CN_NUM_MAP:
            return 10 + _CN_NUM_MAP[t[1]]
        if len(t) == 2 and t[1] == "\u5341" and t[0] in _CN_NUM_MAP:
            return _CN_NUM_MAP[t[0]] * 10
        return None

    def _parse_yage_selector(self, content: str) -> tuple[int | None, str | None]:
        text = (content or "").strip()
        if not text:
            return None, None

        # Explicit date: 2026-04-12 / 2026年4月12日 / 4月12号
        m = re.search(r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})", text)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return None, f"{y:04d}-{mo:02d}-{d:02d}"
        m2 = re.search(r"(\d{1,2})月(\d{1,2})[日号]?", text)
        if m2:
            mo, d = int(m2.group(1)), int(m2.group(2))
            y = datetime.now().year
            return None, f"{y:04d}-{mo:02d}-{d:02d}"
        # Fallback short date: 04-10 / 4/10
        m2b = re.search(r"(?<!\d)(\d{1,2})[/-](\d{1,2})(?!\d)", text)
        if m2b:
            mo, d = int(m2b.group(1)), int(m2b.group(2))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                y = datetime.now().year
                return None, f"{y:04d}-{mo:02d}-{d:02d}"

        # Relative date
        now = datetime.now()
        if "\u6628\u5929" in text or "\u6628\u665a" in text:
            day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            return None, day
        if "\u524d\u5929" in text:
            day = (now - timedelta(days=2)).strftime("%Y-%m-%d")
            return None, day

        # Rank selectors
        if "\u5012\u6570\u7b2c\u4e8c" in text or "\u7b2c\u4e8c\u65b0" in text or "\u4e0a\u4e00\u7bc7" in text:
            return 2, None
        m3 = re.search(r"\u7b2c([0-9一二三四五六七八九十两]+)(?:\u65b0|\u7bc7|\u6761)", text)
        if m3:
            n = self._cn_num_to_int(m3.group(1))
            if n and n > 0:
                return n, None
        if "\u6700\u65b0" in text:
            return 1, None
        return None, None

    def _extract_yage_request(self, content: str) -> bool:
        text = (content or "").strip()
        lower = text.lower()
        if not text:
            return False
        if _YAGE_HINT not in text and "yage" not in lower:
            return False
        # Avoid accidental auto-push in casual discussion.
        has_action_intent = any(k in text for k in _YAGE_ACTION_HINTS) or "send me" in lower or "show me" in lower
        has_time_intent = any(k in text for k in _YAGE_LATEST_HINTS) or any(
            k in text for k in _YAGE_RECENT_HINTS
        )
        has_article_intent = any(k in text for k in _YAGE_ARTICLE_HINTS) or "article" in lower or "post" in lower
        # Request-like patterns: explicit action OR interrogative wording.
        has_question_tone = (
            ("?" in text)
            or ("\uFF1F" in text)
            or text.endswith(("\u5417", "\u5462", "\u561b"))
            or ("\u6700\u65b0" in text and _YAGE_HINT in text)
        )
        has_date_pattern = bool(
            re.search(r"(20\d{2})[年/\-](\d{1,2})[月/\-](\d{1,2})", text)
            or re.search(r"(\d{1,2})月(\d{1,2})[日号]?", text)
            or re.search(r"(?<!\d)(\d{1,2})[/-](\d{1,2})(?!\d)", text)
        )
        # Trigger when it looks like a request and has article/time/date intent.
        if not (has_action_intent or has_question_tone):
            return False
        if (not has_article_intent) and (not has_time_intent) and (not has_date_pattern):
            return False
        return True

    async def _try_handle_yage_raw(
        self,
        chat_id: str,
        content: str,
        message_id: str | None,
    ) -> bool:
        if not self._extract_yage_request(content):
            return False
        nth, target_date = self._parse_yage_selector(content)
        raw = await self._run_yage_signed(
            timeout_sec=45.0,
            nth=nth,
            target_date=target_date,
            force_latest=bool((nth is None and target_date is None) or (nth == 1 and not target_date)),
        )
        if raw is None:
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel="qq",
                    chat_id=chat_id,
                    content="鸭哥文章抓取失败，请稍后重试。",
                    metadata={"message_id": message_id},
                )
            )
            return True
        if not raw.strip():
            not_found_hint = ""
            if target_date:
                not_found_hint = f" (date={target_date})"
            elif nth and nth > 1:
                not_found_hint = f" (nth={nth})"
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel="qq",
                    chat_id=chat_id,
                    content=f"当前未抓取到匹配的鸭哥文章内容{not_found_hint}。",
                    metadata={"message_id": message_id},
                )
            )
            return True
        await self.bus.publish_outbound(
            OutboundMessage(
                channel="qq",
                chat_id=chat_id,
                content=raw,
                metadata={"message_id": message_id},
            )
        )
        logger.info("QQ yage raw handler sent signed latest article chat_id={}", chat_id)
        return True

    def _is_wechat_title_query(self, content: str) -> bool:
        text = (content or "").strip()
        lower = text.lower()
        if not text:
            return False
        if _CN_WECHAT not in text and _CN_OFFICIAL not in text and "wechat" not in lower and "weixin" not in lower:
            return False
        return any(hint in text for hint in _WECHAT_TITLE_HINTS) or "latest title" in lower or "latest article" in lower

    async def _try_handle_wechat_grounded(self, user_id: str, chat_id: str, content: str, message_id: str) -> bool:
        if not self.is_allowed(user_id):
            return False

        title_query = self._is_wechat_title_query(content)
        question = self._extract_wechat_question(content)
        if not title_query and not question:
            return False

        if title_query and not question:
            latest = await self._run_sidecar_json(["latest", "--days", "7", "--limit", "50"])
            if not latest or latest.get("status") in {"empty", "error"}:
                reply = "已核验原文：未找到可用文章（NOT_FOUND_IN_ARTICLE）"
            else:
                reply = (
                    f"\u6700\u65b0\u6587\u7ae0\uff1a{latest.get('title') or ''}\n"
                    f"entry_id: {latest.get('entry_id') or 0}\n"
                    f"published_at: {latest.get('published_at') or ''}\n"
                    f"link: {latest.get('link') or ''}"
                )
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel="qq",
                    chat_id=chat_id,
                    content=reply,
                    metadata={"message_id": message_id},
                )
            )
            return True

        ask = await self._run_sidecar_json(
            ["ask", "--question", question or content, "--days", "7", "--limit", "50"]
        )
        if not ask:
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel="qq",
                    chat_id=chat_id,
                    content="已核验原文：未命中问题答案（NOT_FOUND_IN_ARTICLE）",
                    metadata={"message_id": message_id},
                )
            )
            return True

        status = str(ask.get("status") or "").lower()
        if status != "ok":
            reply = (
                "已核验原文：未命中问题答案（NOT_FOUND_IN_ARTICLE）\n"
                f"entry_id: {ask.get('entry_id') or 0}\n"
                f"published_at: {ask.get('published_at') or ''}\n"
                f"link: {ask.get('link') or ''}"
            )
        else:
            answer = str(ask.get("answer") or "").strip()
            reply = (
                f"entry_id: {ask.get('entry_id') or 0}\n"
                f"published_at: {ask.get('published_at') or ''}\n"
                f"link: {ask.get('link') or ''}\n\n"
                f"{answer or 'NOT_FOUND_IN_ARTICLE'}"
            )
        await self.bus.publish_outbound(
            OutboundMessage(
                channel="qq",
                chat_id=chat_id,
                content=reply,
                metadata={"message_id": message_id},
            )
        )
        return True

    async def stop(self) -> None:
        """Stop bot and cleanup resources."""
        self._running = False
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
        self._client = None

        if self._http:
            try:
                await self._http.close()
            except Exception:
                pass
        self._http = None

        logger.info("QQ bot stopped")

    # ---------------------------
    # Outbound (send)
    # ---------------------------

    def _requires_signed_payload(self, content: str) -> bool:
        """Detect high-risk article payloads that must be signed raw output."""
        text = (content or "").strip()
        if not text:
            return False
        if _YAGE_ARTICLE_RE.search(text):
            return True
        if "yage-ai.kit.com/posts/" in text:
            return True
        return False

    def _verify_and_unwrap_signed_payload(self, content: str) -> str | None:
        """Verify signed payload and return body using QQ-Sidecar-RS."""
        try:
            req = urllib.request.Request("http://172.17.0.1:8092/verify", data=json.dumps({"content": content}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("success"):
                    return data.get("body")
                return None
        except Exception as e:
            logger.error(f"QQ sidecar verify error: {e}")
            return None
    def _extract_yage_source_url(self, body: str) -> str | None:
        """Extract yage source URL from signed payload body."""
        text = (body or "").strip()
        if not text:
            return None
        m = _YAGE_URL_IN_LINK_RE.search(text)
        if m:
            return m.group(1).strip()
        m = _YAGE_URL_BARE_RE.search(text)
        if m:
            return m.group(0).strip()
        return None

    @staticmethod
    def _extract_date_from_url(url: str) -> datetime | None:
        m = _YAGE_DATE_IN_URL_RE.search(url or "")
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except Exception:
            return None

    def _should_ack_yage_url(self, previous_url: str, candidate_url: str) -> bool:
        """Only advance cache when candidate is same/newer, never roll back."""
        prev = (previous_url or "").strip()
        cand = (candidate_url or "").strip()
        if not cand:
            return False
        if not prev:
            return True
        if prev == cand:
            return False
        prev_dt = self._extract_date_from_url(prev)
        cand_dt = self._extract_date_from_url(cand)
        if prev_dt and cand_dt:
            return cand_dt >= prev_dt
        # Fallback: if date parsing fails, avoid regressing unknown state.
        return False

    async def _ack_yage_delivery(self, body: str, chat_id: str) -> None:
        """Update yage cache only after QQ send has succeeded."""
        source_url = self._extract_yage_source_url(body)
        if not source_url:
            return

        def _write_cache() -> tuple[bool, str]:
            prev = ""
            cache: dict[str, Any] = {}
            if os.path.exists(_YAGE_CACHE_FILE):
                try:
                    with open(_YAGE_CACHE_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, dict):
                            cache = data
                except Exception:
                    cache = {}
            prev = str(cache.get("last_url") or "").strip()
            if not self._should_ack_yage_url(prev, source_url):
                return False, prev
            cache["last_url"] = source_url
            tmp = f"{_YAGE_CACHE_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
            os.replace(tmp, _YAGE_CACHE_FILE)
            return True, prev

        try:
            updated, prev = await asyncio.to_thread(_write_cache)
            if updated:
                logger.info(
                    "QQ yage delivery ack cache updated chat_id={} prev={} new={}",
                    chat_id,
                    prev or "(empty)",
                    source_url,
                )
        except Exception as e:
            logger.warning("QQ yage delivery ack failed chat_id={} err={}", chat_id, e)

    def _extract_wechat_ack_marker(self, body: str) -> tuple[str, tuple[int, int] | None]:
        """Strip internal wechat ACK marker from body and return ack tuple."""
        text = body or ""
        m = _WECHAT_ACK_MARKER_RE.search(text)
        if not m:
            return text, None
        sub_id = int(m.group(1))
        entry_id = int(m.group(2))
        cleaned = _WECHAT_ACK_MARKER_RE.sub("", text).strip()
        return cleaned, (sub_id, entry_id)

    def _extract_wechat_subscription_id(self, content: str) -> int | None:
        """Extract wechat subscription id from internal ACK marker."""
        text = content or ""
        m = _WECHAT_ACK_MARKER_RE.search(text)
        if not m:
            return None
        try:
            sub_id = int(m.group(1))
            return sub_id if sub_id > 0 else None
        except Exception:
            return None

    def _extract_signed_digest(self, content: str) -> str | None:
        text = (content or "").strip()
        m = re.match(r"^NBRAW1-SHA256:([0-9a-fA-F]{64})", text)
        if not m:
            return None
        return m.group(1).lower()

    async def _recover_wechat_signed_by_digest(
        self, expected_digest: str, timeout_sec: float = 45.0
    ) -> tuple[str | None, int | None]:
        """Best-effort recovery when ACK marker is missing but signed digest is present."""
        if not expected_digest:
            return None, None
        candidate_ids: list[int] = []
        try:
            if os.path.exists(_WECHAT_CACHE_FILE):
                with open(_WECHAT_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        for k in data.keys():
                            if isinstance(k, str) and k.startswith("sub:"):
                                try:
                                    sid = int(k.split(":", 1)[1])
                                    if sid > 0:
                                        candidate_ids.append(sid)
                                except Exception:
                                    pass
        except Exception:
            pass
        for sid in (1, 2, 3):
            if sid not in candidate_ids:
                candidate_ids.append(sid)

        for sid in candidate_ids:
            recovered_raw = await self._run_wechat_signed(sid, timeout_sec=timeout_sec, force=True)
            if not recovered_raw or not recovered_raw.startswith(_SIGNED_PAYLOAD_PREFIX):
                continue
            got_digest = self._extract_signed_digest(recovered_raw) or ""
            if got_digest == expected_digest:
                return recovered_raw, sid
        return None, None

    async def _ack_wechat_delivery(
        self, ack: tuple[int, int] | None, chat_id: str
    ) -> None:
        """Update wechat sidecar cache only after QQ send has succeeded."""
        if not ack:
            return
        sub_id, entry_id = ack
        if sub_id < 0 or entry_id <= 0:
            return
        cache_key = f"sub:{sub_id}"

        def _write_cache() -> tuple[bool, int]:
            cache: dict[str, Any] = {}
            if os.path.exists(_WECHAT_CACHE_FILE):
                try:
                    with open(_WECHAT_CACHE_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, dict):
                            cache = data
                except Exception:
                    cache = {}
            prev = int(cache.get(cache_key, 0) or 0)
            if entry_id <= prev:
                return False, prev
            cache[cache_key] = entry_id
            tmp = f"{_WECHAT_CACHE_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
            os.replace(tmp, _WECHAT_CACHE_FILE)
            return True, prev

        try:
            updated, prev = await asyncio.to_thread(_write_cache)
            if updated:
                logger.info(
                    "QQ wechat delivery ack cache updated chat_id={} key={} prev={} new={}",
                    chat_id,
                    cache_key,
                    prev,
                    entry_id,
                )
        except Exception as e:
            logger.warning("QQ wechat delivery ack failed chat_id={} err={}", chat_id, e)

    async def send(self, msg: OutboundMessage) -> None:
        """Send attachments first, then text."""
        if not self._client:
            logger.warning("QQ client not initialized")
            return

        msg_id = msg.metadata.get("message_id")
        chat_type = self._chat_type_cache.get(msg.chat_id, "c2c")
        is_group = chat_type == "group"

        # 1) Send media
        for media_ref in msg.media or []:
            ok = await self._send_media(
                chat_id=msg.chat_id,
                media_ref=media_ref,
                msg_id=msg_id,
                is_group=is_group,
            )
            if not ok:
                filename = (
                    os.path.basename(urlparse(media_ref).path)
                    or os.path.basename(media_ref)
                    or "file"
                )
                await self._send_text_only(
                    chat_id=msg.chat_id,
                    is_group=is_group,
                    msg_id=msg_id,
                    content=f"[Attachment send failed: {filename}]",
                )

        # 2) Send text (chunked to avoid QQ-side truncation on long payloads)
        if msg.content and msg.content.strip():
            stripped_content = _strip_silent_marker(msg.content)
            if stripped_content != msg.content:
                msg.content = stripped_content
                if not msg.content:
                    logger.info("QQ outbound suppressed by silent marker chat_id={}", msg.chat_id)
                    return
            is_signed_payload = msg.content.startswith(_SIGNED_PAYLOAD_PREFIX)
            requires_signature = self._requires_signed_payload(msg.content)
            if requires_signature and not is_signed_payload:
                logger.warning(
                    "QQ outbound blocked: yage-like payload without signature chat_id={}",
                    msg.chat_id,
                )
                await self._report_signature_blocked(
                    source_chat_id=msg.chat_id,
                    source_is_group=is_group,
                    source_msg_id=msg_id,
                )
                return

            safe_content = msg.content
            if is_signed_payload:
                safe_content = self._verify_and_unwrap_signed_payload(msg.content)
                if safe_content is None:
                    # Cron may occasionally reconstruct signed payload incorrectly.
                    # Self-heal by fetching latest signed raw article directly and retrying once.
                    expected_digest = self._extract_signed_digest(msg.content)
                    # 1) WeChat signed payload recovery (preferred for wechat cron pushes)
                    sub_id = self._extract_wechat_subscription_id(msg.content)
                    if sub_id is not None:
                        recovered_wechat = await self._run_wechat_signed(sub_id, timeout_sec=45.0, force=True)
                        if recovered_wechat and recovered_wechat.startswith(_SIGNED_PAYLOAD_PREFIX):
                            recovered_body = self._verify_and_unwrap_signed_payload(recovered_wechat)
                            if recovered_body and recovered_body.strip():
                                logger.warning(
                                    "QQ signature mismatch self-healed via fresh wechat fetch chat_id={} sub={}",
                                    msg.chat_id,
                                    sub_id,
                                )
                                safe_content = recovered_body

                    # 1.5) Marker may be stripped by LLM/tool-call transport.
                    # Recover by signed digest across known subscription ids.
                    if safe_content is None and expected_digest:
                        recovered_wechat, recovered_sub = await self._recover_wechat_signed_by_digest(
                            expected_digest,
                            timeout_sec=45.0,
                        )
                        if recovered_wechat and recovered_wechat.startswith(_SIGNED_PAYLOAD_PREFIX):
                            recovered_body = self._verify_and_unwrap_signed_payload(recovered_wechat)
                            if recovered_body and recovered_body.strip():
                                logger.warning(
                                    "QQ signature mismatch self-healed via digest recovery chat_id={} sub={}",
                                    msg.chat_id,
                                    recovered_sub,
                                )
                                safe_content = recovered_body

                    # 2) Yage signed payload recovery (legacy path)
                    if safe_content is None:
                        recovered = await self._run_yage_signed(timeout_sec=45.0, force_latest=True)
                        if recovered and recovered.startswith(_SIGNED_PAYLOAD_PREFIX):
                            recovered_body = self._verify_and_unwrap_signed_payload(recovered)
                            if recovered_body and recovered_body.strip():
                                logger.warning(
                                    "QQ signature mismatch self-healed via fresh yage fetch chat_id={}",
                                    msg.chat_id,
                                )
                                safe_content = recovered_body
                    if safe_content is not None and safe_content.strip():
                        pass
                    else:
                        logger.warning("QQ outbound blocked by signature validation chat_id={}", msg.chat_id)
                        await self._report_signature_blocked(
                            source_chat_id=msg.chat_id,
                            source_is_group=is_group,
                            source_msg_id=msg_id,
                        )
                        return
                elif not safe_content.strip():
                    logger.warning("QQ outbound blocked by signature validation chat_id={}", msg.chat_id)
                    await self._report_signature_blocked(
                        source_chat_id=msg.chat_id,
                        source_is_group=is_group,
                        source_msg_id=msg_id,
                    )
                    return
            safe_content, wechat_ack = self._extract_wechat_ack_marker(safe_content)
            safe_content = _strip_silent_marker(safe_content)
            if not safe_content.strip():
                return

            if is_signed_payload:
                # Prefer one-shot delivery for raw signed articles.
                # Only fallback to splitting when QQ rejects oversize payload.
                try:
                    await self._send_text_only(
                        chat_id=msg.chat_id,
                        is_group=is_group,
                        msg_id=msg_id,
                        content=safe_content,
                    )
                    await self._ack_yage_delivery(safe_content, msg.chat_id)
                    await self._ack_wechat_delivery(wechat_ack, msg.chat_id)
                    return
                except Exception as e:
                    logger.warning(
                        "QQ signed payload one-shot send failed, fallback to chunking chat_id={} err={}",
                        msg.chat_id,
                        e,
                    )

            max_len = max(200, int(getattr(self.config, "text_chunk_max_len", 1200) or 1200))
            for chunk in split_message(safe_content, max_len):
                if not chunk:
                    continue
                try:
                    await self._send_text_only(
                        chat_id=msg.chat_id,
                        is_group=is_group,
                        msg_id=msg_id,
                        content=chunk,
                    )
                except Exception as e:
                    logger.error("QQ text send failed chat_id={} err={}", msg.chat_id, e)
                    return
            if is_signed_payload:
                await self._ack_yage_delivery(safe_content, msg.chat_id)
                await self._ack_wechat_delivery(wechat_ack, msg.chat_id)

    async def _report_signature_blocked(
        self,
        source_chat_id: str,
        source_is_group: bool,
        source_msg_id: str | None,
    ) -> None:
        """Send anti-tamper alert to source chat and optional alert chat."""
        if not getattr(self.config, "signature_alert_enabled", True):
            return

        content = (
            "[ALERT] 内容签名校验失败，消息已被拦截未发送。\n"
            "可能原因：输出被改写、拼接或截断。\n"
            "请检查对应脚本输出是否为 NBRAW1-SHA256 签名格式。"
        )
        await self._send_text_only(
            chat_id=source_chat_id,
            is_group=source_is_group,
            msg_id=source_msg_id,
            content=content,
        )

        alert_chat_id = (getattr(self.config, "signature_alert_chat_id", "") or "").strip()
        if not alert_chat_id or alert_chat_id == source_chat_id:
            return

        alert_is_group = self._chat_type_cache.get(alert_chat_id, "c2c") == "group"
        await self._send_text_only(
            chat_id=alert_chat_id,
            is_group=alert_is_group,
            msg_id=None,
            content=f"{content}\nsource_chat_id: {source_chat_id}",
        )

    async def _send_text_only(
        self,
        chat_id: str,
        is_group: bool,
        msg_id: str | None,
        content: str,
    ) -> None:
        """Send a plain/markdown text message."""
        if not self._client:
            return
        content = _strip_silent_marker(content)
        if not content:
            return

        self._msg_seq += 1
        use_markdown = self.config.msg_format == "markdown"
        payload: dict[str, Any] = {
            "msg_type": 2 if use_markdown else 0,
            "msg_id": msg_id,
            "msg_seq": self._msg_seq,
        }
        if use_markdown:
            payload["markdown"] = {"content": content}
        else:
            payload["content"] = content

        await self._post_text_payload(
            chat_id=chat_id,
            is_group=is_group,
            msg_id=msg_id,
            payload=payload,
        )

    def _apply_botpy_http_timeout(self) -> None:
        """Keep botpy sends from hanging longer than our QQ channel budget."""
        timeout = float(getattr(self.config, "botpy_http_timeout_sec", 0) or 0)
        if not self._client or timeout <= 0:
            return
        try:
            api = getattr(self._client, "api", None)
            http = getattr(api, "_http", None)
            if http is not None:
                http.timeout = timeout
        except Exception as e:  # pragma: no cover - defensive only
            logger.debug("QQ botpy timeout tune skipped: {}", e)

    async def _post_text_payload(
        self,
        chat_id: str,
        is_group: bool,
        msg_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        """Post text through botpy, retrying passive replies when botpy reports no response."""
        self._apply_botpy_http_timeout()
        can_retry = bool(msg_id) and bool(getattr(self.config, "send_retry_on_empty_response", False))
        retry_attempts = int(getattr(self.config, "send_retry_attempts", 0) or 0)
        attempts = 1 + max(0, retry_attempts if can_retry else 0)
        delay = max(0.0, float(getattr(self.config, "send_retry_delay_sec", 0.0) or 0.0))

        for attempt in range(1, attempts + 1):
            try:
                if is_group:
                    result = await self._client.api.post_group_message(group_openid=chat_id, **payload)
                else:
                    result = await self._client.api.post_c2c_message(openid=chat_id, **payload)
                if can_retry and result is None:
                    raise asyncio.TimeoutError("QQ API returned no response")
                return
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
                if attempt >= attempts:
                    logger.warning(
                        "QQ text send failed after {} attempt(s) chat_id={} msg_seq={} err={}",
                        attempts,
                        chat_id,
                        payload.get("msg_seq"),
                        e,
                    )
                    raise
                logger.warning(
                    "QQ text send attempt {}/{} failed chat_id={} msg_seq={} err={}; retrying same msg_seq",
                    attempt,
                    attempts,
                    chat_id,
                    payload.get("msg_seq"),
                    e,
                )
                if delay:
                    await asyncio.sleep(delay)

    async def _send_media(
        self,
        chat_id: str,
        media_ref: str,
        msg_id: str | None,
        is_group: bool,
    ) -> bool:
        """Read bytes -> base64 upload -> msg_type=7 send."""
        if not self._client:
            return False

        data, filename = await self._read_media_bytes(media_ref)
        if not data or not filename:
            return False

        try:
            file_type = _guess_send_file_type(filename)
            file_data_b64 = base64.b64encode(data).decode()

            media_obj = await self._post_base64file(
                chat_id=chat_id,
                is_group=is_group,
                file_type=file_type,
                file_data=file_data_b64,
                file_name=filename,
                srv_send_msg=False,
            )
            if not media_obj:
                logger.error("QQ media upload failed: empty response")
                return False

            self._msg_seq += 1
            if is_group:
                await self._client.api.post_group_message(
                    group_openid=chat_id,
                    msg_type=7,
                    msg_id=msg_id,
                    msg_seq=self._msg_seq,
                    media=media_obj,
                )
            else:
                await self._client.api.post_c2c_message(
                    openid=chat_id,
                    msg_type=7,
                    msg_id=msg_id,
                    msg_seq=self._msg_seq,
                    media=media_obj,
                )

            logger.info("QQ media sent: {}", filename)
            return True
        except (aiohttp.ClientError, OSError) as e:
            logger.error("QQ send media network failed filename={} err={}", filename, e)
            raise
        except Exception as e:
            logger.error("QQ send media failed filename={} err={}", filename, e)
            return False

    async def _read_media_bytes(self, media_ref: str) -> tuple[bytes | None, str | None]:
        """Read bytes from http(s) or local file path; return (data, filename)."""
        media_ref = (media_ref or "").strip()
        if not media_ref:
            return None, None

        # Local file: plain path or file:// URI
        if not media_ref.startswith("http://") and not media_ref.startswith("https://"):
            try:
                if media_ref.startswith("file://"):
                    parsed = urlparse(media_ref)
                    # Windows: path in netloc; Unix: path in path
                    raw = parsed.path or parsed.netloc
                    local_path = Path(unquote(raw))
                else:
                    local_path = Path(os.path.expanduser(media_ref))

                if not local_path.is_file():
                    logger.warning("QQ outbound media file not found: {}", str(local_path))
                    return None, None

                data = await asyncio.to_thread(local_path.read_bytes)
                return data, local_path.name
            except Exception as e:
                logger.warning("QQ outbound media read error ref={} err={}", media_ref, e)
                return None, None

        # Remote URL
        ok, err = validate_url_target(media_ref)
        if not ok:
            logger.warning("QQ outbound media URL validation failed url={} err={}", media_ref, err)
            return None, None

        if not self._http:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        try:
            async with self._http.get(media_ref, allow_redirects=True) as resp:
                if resp.status >= 400:
                    logger.warning(
                        "QQ outbound media download failed status={} url={}",
                        resp.status,
                        media_ref,
                    )
                    return None, None
                data = await resp.read()
                if not data:
                    return None, None
                filename = os.path.basename(urlparse(media_ref).path) or "file.bin"
                return data, filename
        except Exception as e:
            logger.warning("QQ outbound media download error url={} err={}", media_ref, e)
            return None, None

    # https://github.com/tencent-connect/botpy/issues/198
    # https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/send-receive/rich-media.html
    async def _post_base64file(
        self,
        chat_id: str,
        is_group: bool,
        file_type: int,
        file_data: str,
        file_name: str | None = None,
        srv_send_msg: bool = False,
    ) -> Media:
        """Upload base64-encoded file and return Media object."""
        if not self._client:
            raise RuntimeError("QQ client not initialized")

        if is_group:
            endpoint = "/v2/groups/{group_openid}/files"
            id_key = "group_openid"
        else:
            endpoint = "/v2/users/{openid}/files"
            id_key = "openid"

        payload = {
            id_key: chat_id,
            "file_type": file_type,
            "file_data": file_data,
            "srv_send_msg": srv_send_msg,
        }
        if file_type != QQ_FILE_TYPE_IMAGE and file_name:
            payload["file_name"] = file_name

        route = Route("POST", endpoint, **{id_key: chat_id})
        result = await self._client.api._http.request(route, json=payload)
        if isinstance(result, dict) and "file_info" in result:
            return {"file_info": result["file_info"]}
        return result


    def _match_personal_ops_command(self, content: str) -> str | None:
        """Map short ops questions to deterministic local dashboard commands."""
        text = (content or "").strip().lower()
        compact = re.sub(r"[\s，。！？!?、:：；;,.]+", "", text)
        if not compact:
            return None

        if any(k in compact for k in ("今天有什么要看", "今天看什么", "今日摘要", "今天摘要")):
            return "today"
        if any(k in compact for k in ("你能做什么", "能力列表", "能力菜单", "菜单", "帮助")) and len(compact) <= 16:
            return "menu"
        if "内存" in compact and len(compact) <= 24:
            return "system"
        if any(k in compact for k in ("系统状态", "服务状态", "服务健康", "服务还活着", "健康检查", "服务器状态")):
            return "system"
        if any(k in compact for k in ("定时任务", "cron", "任务状态", "任务报错", "哪些任务在跑")):
            return "tasks"
        if any(k in compact for k in ("今天先看什么", "先看什么")) and not any(k in compact for k in ("收件箱", "待读", "稍后看")):
            return "decision"
        if any(k in compact for k in ("今天怎么安排", "有什么建议", "决策建议", "下一步做什么", "现在该干嘛")):
            return "decision"
        if any(k in compact for k in ("鸭哥", "微信文章", "rss文章", "今天文章", "文章有哪些", "文章更新")):
            return "articles"
        if any(k in compact for k in ("lof", "qdii", "基金溢价", "溢价机会", "套利机会")):
            return "lof"
        return None

    async def _run_personal_ops_command(self, command: str) -> str:
        """Run the personal ops script without involving the LLM."""
        script = Path("/root/.nanobot/workspace/skills/personal-ops-assistant/ops_summary.py")
        if not script.exists():
            return "运维助手脚本不存在，暂时无法查询。"

        proc = await asyncio.create_subprocess_exec(
            "python3",
            str(script),
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return "运维查询超时了，稍后再试一下。"

        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            detail = err or out or f"exit {proc.returncode}"
            return f"运维查询失败：{detail[:500]}"
        return out or "运维查询完成，但没有输出。"

    async def _try_handle_personal_ops_query(
        self,
        *,
        chat_id: str,
        is_group: bool,
        message_id: str,
        content: str,
    ) -> bool:
        command = self._match_personal_ops_command(content)
        if not command:
            return False

        reply = await self._run_personal_ops_command(command)
        max_len = max(200, int(getattr(self.config, "text_chunk_max_len", 1200) or 1200))
        for chunk in split_message(reply, max_len):
            if chunk.strip():
                await self._send_text_only(
                    chat_id=chat_id,
                    is_group=is_group,
                    msg_id=message_id,
                    content=chunk,
                )
        logger.info("QQ personal ops fast path handled command={} message_id={}", command, message_id)
        return True

    def _match_knowledge_inbox_command(self, content: str) -> list[str] | None:
        """Map link/inbox prompts to the local knowledge inbox script."""
        text = (content or "").strip()
        compact = re.sub(r"[\s，。！？!?、:：；;,.]+", "", text.lower())
        if not compact:
            return None

        urls = [u.rstrip("。.,，、；;!！?？") for u in _GENERIC_URL_RE.findall(text)]
        if not urls:
            if any(k in compact for k in ("待读简报", "收件箱简报", "稍后看简报", "收件箱今天先看什么", "待读先看什么")):
                return ["brief", "--limit", "8"]
            if any(k in compact for k in ("收件箱", "待读列表", "链接清单", "稍后看清单")):
                return ["list", "--limit", "8"]
            return None

        url = urls[0]
        host = urlparse(url).netloc.lower()
        explicit_inbox = any(
            k in compact
            for k in (
                "收一下",
                "存一下",
                "加入收件箱",
                "放收件箱",
                "放到收件箱",
                "稍后看",
                "待读",
                "链接收件箱",
            )
        )
        decision = any(
            k in compact
            for k in (
                "值得看",
                "值不值得",
                "要不要看",
                "要不要读",
                "该不该看",
                "帮我判断",
                "帮我看看",
                "决策",
            )
        )
        only_url = text == url or text.strip(" \t\r\n。.,，、；;!！?？") == url

        # WeChat/Yage links have dedicated handlers. Do not steal them unless the
        # user explicitly asks to put the link into the generic inbox.
        if any(special in host for special in _INBOX_SPECIAL_HOSTS) and not explicit_inbox:
            return None
        if decision:
            question = _GENERIC_URL_RE.sub("", text).strip()
            return ["decide", url, "--question", question[:180] or "这个值得看吗"]
        if explicit_inbox or only_url:
            return ["capture", url]
        return None

    async def _run_knowledge_inbox_command(self, args: list[str]) -> str:
        """Run the knowledge inbox script without involving the LLM."""
        script = Path("/root/.nanobot/workspace/skills/knowledge-inbox/inbox.py")
        if not script.exists():
            return "知识收件箱脚本不存在，暂时无法处理链接。"

        proc = await asyncio.create_subprocess_exec(
            "python3",
            str(script),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=35)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return "知识收件箱抓取超时了，可能是目标网页太慢或禁止访问。"

        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            detail = err or out or f"exit {proc.returncode}"
            return f"知识收件箱失败：{detail[:500]}"
        return out or "知识收件箱处理完成，但没有输出。"

    async def _try_handle_knowledge_inbox_query(
        self,
        *,
        chat_id: str,
        is_group: bool,
        message_id: str,
        content: str,
    ) -> bool:
        args = self._match_knowledge_inbox_command(content)
        if not args:
            return False

        reply = await self._run_knowledge_inbox_command(args)
        max_len = max(200, int(getattr(self.config, "text_chunk_max_len", 1200) or 1200))
        for chunk in split_message(reply, max_len):
            if chunk.strip():
                await self._send_text_only(
                    chat_id=chat_id,
                    is_group=is_group,
                    msg_id=message_id,
                    content=chunk,
                )
        logger.info("QQ knowledge inbox fast path handled args={} message_id={}", args, message_id)
        return True


    # ---------------------------
    # Inbound (receive)
    # ---------------------------

    async def _on_message(self, data: C2CMessage | GroupMessage, is_group: bool = False) -> None:
        """Parse inbound message, download attachments, and publish to the bus."""
        if data.id in self._processed_ids:
            return
        self._processed_ids.append(data.id)

        author = getattr(data, "author", None)
        if is_group:
            chat_id = getattr(data, "group_openid", "")
            user_id = getattr(author, "member_openid", "unknown")
            if not chat_id:
                logger.warning(
                    "QQ group message missing group_openid message_id={}",
                    getattr(data, "id", "unknown"),
                )
                return
            self._chat_type_cache[chat_id] = "group"
        else:
            chat_id = str(getattr(author, "id", None) or getattr(author, "user_openid", "unknown"))
            user_id = chat_id
            self._chat_type_cache[chat_id] = "c2c"

        ack_message = (getattr(self.config, "ack_message", "") or "").strip()
        if ack_message:
            try:
                await self._send_text_only(
                    chat_id=chat_id,
                    is_group=is_group,
                    msg_id=data.id,
                    content=ack_message,
                )
            except Exception as e:
                logger.warning("QQ ack send failed message_id={} err={}", data.id, e)

        content = (getattr(data, "content", "") or "").strip()

        # the data used by tests don't contain attachments property
        # so we use getattr with a default of [] to avoid AttributeError in tests
        attachments = getattr(data, "attachments", None) or []
        if content and not attachments:
            if await self._try_handle_personal_ops_query(
                chat_id=chat_id,
                is_group=is_group,
                message_id=data.id,
                content=content,
            ):
                return
            if await self._try_handle_knowledge_inbox_query(
                chat_id=chat_id,
                is_group=is_group,
                message_id=data.id,
                content=content,
            ):
                return

        media_paths, recv_lines, att_meta = await self._handle_attachments(attachments)

        # Compose content that always contains actionable saved paths
        if recv_lines:
            tag = "[Image]" if any(_is_image_name(Path(p).name) for p in media_paths) else "[File]"
            file_block = "Received files:\n" + "\n".join(recv_lines)
            content = f"{content}\n\n{file_block}".strip() if content else f"{tag}\n{file_block}"

        if not content and not media_paths:
            return

        if content and not media_paths:
            yage_handled = await self._try_handle_yage_raw(
                chat_id=chat_id,
                content=content,
                message_id=data.id,
            )
            if yage_handled:
                logger.info("QQ yage raw handler handled message_id={}", data.id)
                return

            handled = await self._try_handle_wechat_grounded(
                user_id=user_id,
                chat_id=chat_id,
                content=content,
                message_id=data.id,
            )
            if handled:
                logger.info("QQ grounded WeChat guard handled message_id={}", data.id)
                return

        await self._handle_message(
            sender_id=user_id,
            chat_id=chat_id,
            content=content,
            media=media_paths if media_paths else None,
            metadata={
                "message_id": data.id,
                "attachments": att_meta,
            },
        )

    async def _handle_attachments(
        self,
        attachments: list[BaseMessage._Attachments],
    ) -> tuple[list[str], list[str], list[dict[str, Any]]]:
        """Extract, download (chunked), and format attachments for agent consumption."""
        media_paths: list[str] = []
        recv_lines: list[str] = []
        att_meta: list[dict[str, Any]] = []

        if not attachments:
            return media_paths, recv_lines, att_meta

        for att in attachments:
            url, filename, ctype = att.url, att.filename, att.content_type

            logger.info("Downloading file from QQ: {}", filename or url)
            local_path = await self._download_to_media_dir_chunked(url, filename_hint=filename)

            att_meta.append(
                {
                    "url": url,
                    "filename": filename,
                    "content_type": ctype,
                    "saved_path": local_path,
                }
            )

            if local_path:
                media_paths.append(local_path)
                shown_name = filename or os.path.basename(local_path)
                recv_lines.append(f"- {shown_name}\n  saved: {local_path}")
            else:
                shown_name = filename or url
                recv_lines.append(f"- {shown_name}\n  saved: [download failed]")

        return media_paths, recv_lines, att_meta

    async def _download_to_media_dir_chunked(
        self,
        url: str,
        filename_hint: str = "",
    ) -> str | None:
        """Download an inbound attachment using QQ-Sidecar-RS."""
        import time

        ts = int(time.time() * 1000)
        safe = _sanitize_filename(filename_hint)
        ext = Path(urlparse(url).path).suffix
        if not ext:
            ext = Path(filename_hint).suffix
        if not ext:
            ext = ".bin"

        if safe:
            if not Path(safe).suffix:
                safe = safe + ext
            filename = safe
        else:
            filename = f"qq_file_{ts}{ext}"

        target = self._media_root / filename
        if target.exists():
            target = self._media_root / f"{target.stem}_{ts}{target.suffix}"

        max_bytes = max(1024 * 1024, int(getattr(self.config, "download_max_bytes", 0) or (200 * 1024 * 1024)))

        if not self._http:
            import aiohttp
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))

        try:
            async with self._http.post(
                "http://172.17.0.1:8092/download",
                json={
                    "url": url,
                    "target_path": str(target),
                    "max_bytes": max_bytes
                }
            ) as resp:
                data = await resp.json()
                if data.get("success"):
                    logger.info("QQ file saved via sidecar: {}", str(target))
                    return str(target)
                else:
                    logger.error("QQ sidecar download error: {}", data.get("error"))
                    return None
        except Exception as e:
            logger.error("QQ sidecar download request error: {}", e)
            return None
