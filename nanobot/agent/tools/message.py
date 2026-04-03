"""Message tool for sending messages to users."""

import re
from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import DangerLevel, Permission, Tool
from nanobot.bus.events import OutboundMessage


# Patterns that indicate internal agent reasoning steps — strip these from
# messages before sending so users don't see the agent's "thinking out loud".
# These are common self-referential internal monologue patterns.
_INTERNAL_PATTERNS = [
    re.compile(r"^\[Runtime Context[^\]]*\]\s*", re.MULTILINE),
    re.compile(r"^我(来|先|正在|准备|先读).*?。\s*", re.MULTILINE),
    re.compile(r"^好(的)?，?调用.*?。\s*", re.MULTILINE),
    re.compile(r"^现在(把|发送给|发送给用户|回复用户).*?。\s*", re.MULTILINE),
    re.compile(r"^查询.*?完成.*?信息如下.*?。?\s*", re.MULTILINE),
    re.compile(r"^天气查询完成.*?$", re.MULTILINE),
    re.compile(r"^正在.*?中.*?。\s*", re.MULTILINE),
    re.compile(r"^读取.*?技能.*?。\s*", re.MULTILINE),
    re.compile(r"^正在读取.*?$", re.MULTILINE),
]


def _clean_content(content: str) -> str:
    """Remove internal monologue patterns and metadata blocks from message content."""
    result = content

    # Remove [Runtime Context ...] metadata blocks (everything up to first blank line)
    result = re.sub(
        r"\[Runtime Context[^\]]*\](?:\n[^\n]+)*\n\n",
        "",
        result,
    )

    for pat in _INTERNAL_PATTERNS:
        result = pat.sub("", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


class MessageTool(Tool):
    danger_level = DangerLevel.LOW
    permission = Permission.ALLOW
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
        should_send: Callable[[], bool] | None = None,
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._default_message_id = default_message_id
        self._sent_in_turn: bool = False
        self._should_send = should_send or (lambda: True)

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Set the current message context."""
        self._default_channel = channel
        self._default_chat_id = chat_id
        self._default_message_id = message_id

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def start_turn(self) -> None:
        """Reset per-turn send tracking."""
        self._sent_in_turn = False

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return (
            "Send a message to the user, optionally with file attachments. "
            "This is the ONLY way to deliver files (images, documents, audio, video) to the user. "
            "Use the 'media' parameter with file paths to attach files. "
            "Do NOT use read_file to send files — that only reads content for your own analysis."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send"
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)"
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID"
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: list of file paths to attach (images, audio, documents)"
                }
            },
            "required": ["content"]
        }

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        **kwargs: Any
    ) -> str:
        channel = channel or self._default_channel
        chat_id = chat_id or self._default_chat_id
        message_id = message_id or self._default_message_id

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        # Strip internal monologue before sending so users don't see the agent's
        # thinking steps (e.g. "我来查询天气", "现在发送给用户").
        content = _clean_content(content)

        if not content:
            return "Message suppressed (internal reasoning only)"

        if not self._send_callback or not self._should_send():
            return "Error: Message sending not configured"

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=media or [],
            metadata={
                "message_id": message_id,
            },
        )

        try:
            await self._send_callback(msg)
            if channel == self._default_channel and chat_id == self._default_chat_id:
                self._sent_in_turn = True
            media_info = f" with {len(media)} attachments" if media else ""
            return f"Message sent to {channel}:{chat_id}{media_info}"
        except Exception as e:
            return f"Error sending message: {str(e)}"
