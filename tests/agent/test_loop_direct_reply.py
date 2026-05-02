from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus


def _provider():
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = 4096
    provider.chat_with_retry = AsyncMock()
    return provider


@pytest.mark.asyncio
async def test_direct_reply_is_persisted_for_followup_context(tmp_path) -> None:
    provider = _provider()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")

    out = await loop._process_message(
        InboundMessage(
            channel="qq",
            sender_id="user",
            chat_id="chat",
            content="\u5185\u5b58\u600e\u4e48\u6837",
        )
    )

    assert out is not None
    assert "\u672a\u8c03\u7528 LLM" in out.content
    provider.chat_with_retry.assert_not_awaited()
    session = loop.sessions.get_or_create("qq:chat")
    assert session.messages[-2]["role"] == "user"
    assert session.messages[-2]["content"] == "\u5185\u5b58\u600e\u4e48\u6837"
    assert session.messages[-1]["role"] == "assistant"
    assert "\u672a\u8c03\u7528 LLM" in session.messages[-1]["content"]


@pytest.mark.asyncio
async def test_ack_following_direct_status_does_not_call_llm(tmp_path) -> None:
    provider = _provider()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    session = loop.sessions.get_or_create("qq:chat")
    session.add_message("assistant", "\u5185\u5b58\u76f4\u67e5\uff08\u672a\u8c03\u7528 LLM\uff09")
    loop.sessions.save(session)

    out = await loop._process_message(
        InboundMessage(
            channel="qq",
            sender_id="user",
            chat_id="chat",
            content="\u597d\uff0c\u53ef\u4ee5\uff0c",
        )
    )

    assert out is not None
    assert out.content == "\u597d\uff0c\u6211\u5728\u3002"
    provider.chat_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_low_information_chitchat_uses_direct_reply(tmp_path) -> None:
    provider = _provider()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")

    out = await loop._process_message(
        InboundMessage(
            channel="qq",
            sender_id="user",
            chat_id="chat",
            content="\u6709\u70b9\u610f\u601d\u7684\u3002",
        )
    )

    assert out is not None
    assert out.content == "\u6709\u70b9\u610f\u601d\uff0c\u5c55\u5f00\u8bf4\u8bf4\uff1f"
    provider.chat_with_retry.assert_not_awaited()
