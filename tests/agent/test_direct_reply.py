from nanobot.agent.direct_reply import build_direct_reply
from nanobot.bus.events import InboundMessage


def _msg(content: str) -> InboundMessage:
    return InboundMessage(channel="qq", sender_id="user", chat_id="chat", content=content)


def test_memory_query_returns_direct_reply_without_llm() -> None:
    out = build_direct_reply(
        _msg("\u5185\u5b58\u600e\u4e48\u6837"),
        model="test-model",
        start_time=0,
        last_usage={"prompt_tokens": 10, "cached_tokens": 5, "completion_tokens": 2},
    )

    assert out is not None
    assert out.channel == "qq"
    assert out.chat_id == "chat"
    assert "\u672a\u8c03\u7528 LLM" in out.content
    assert "test-model" in out.content
    assert out.metadata["_direct_reply"] is True


def test_ack_returns_direct_reply_when_previous_turn_is_not_actionable() -> None:
    out = build_direct_reply(
        _msg("\u597d\uff0c\u53ef\u4ee5\uff0c"),
        model="test-model",
        start_time=0,
        history=[{"role": "assistant", "content": "\u5185\u5b58\u76f4\u67e5\uff08\u672a\u8c03\u7528 LLM\uff09"}],
    )

    assert out is not None
    assert out.content == "\u597d\uff0c\u6211\u5728\u3002"
    assert out.metadata["_direct_reply"] is True


def test_ack_does_not_swallow_action_confirmation() -> None:
    out = build_direct_reply(
        _msg("\u597d\uff0c\u53ef\u4ee5\uff0c"),
        model="test-model",
        start_time=0,
        history=[{"role": "assistant", "content": "\u8981\u4e0d\u8981\u6211\u5e2e\u4f60\u91cd\u542f\u670d\u52a1\uff1f"}],
    )

    assert out is None


def test_ack_after_generic_question_stays_fast() -> None:
    out = build_direct_reply(
        _msg("\u597d\uff0c\u53ef\u4ee5"),
        model="test-model",
        start_time=0,
        history=[{"role": "assistant", "content": "\u6536\u5230\uff0c\u6709\u4e8b\u53eb\u6211\uff1f"}],
    )

    assert out is not None
    assert out.content == "\u597d\uff0c\u6211\u5728\u3002"


def test_non_status_message_falls_through() -> None:
    assert build_direct_reply(_msg("\u5e2e\u6211\u5199\u4e00\u6bb5\u603b\u7ed3"), model="test-model", start_time=0) is None
