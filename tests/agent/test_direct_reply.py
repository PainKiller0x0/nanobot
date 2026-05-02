from nanobot.agent.direct_reply import build_direct_reply
from nanobot.bus.events import InboundMessage


def test_memory_query_returns_direct_reply_without_llm() -> None:
    msg = InboundMessage(
        channel="qq",
        sender_id="user",
        chat_id="chat",
        content="内存怎么样",
    )

    out = build_direct_reply(
        msg,
        model="test-model",
        start_time=0,
        last_usage={"prompt_tokens": 10, "cached_tokens": 5, "completion_tokens": 2},
    )

    assert out is not None
    assert out.channel == "qq"
    assert out.chat_id == "chat"
    assert "未调用 LLM" in out.content
    assert "test-model" in out.content
    assert out.metadata["_direct_reply"] is True


def test_non_status_message_falls_through() -> None:
    msg = InboundMessage(
        channel="qq",
        sender_id="user",
        chat_id="chat",
        content="帮我写一段总结",
    )

    assert build_direct_reply(msg, model="test-model", start_time=0) is None
