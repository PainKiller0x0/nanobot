from nanobot.agent import direct_reply
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


def test_capability_menu_uses_registry_without_llm(monkeypatch) -> None:
    monkeypatch.setattr(
        direct_reply,
        "_load_capabilities",
        lambda: [
            {
                "id": "ops-health",
                "name": "\u8fd0\u7ef4\u95ee\u7b54",
                "description": "\u5185\u5b58\u3001\u670d\u52a1\u3001cron \u72b6\u6001\u76f4\u67e5",
                "category": "\u8fd0\u7ef4",
                "enabled": True,
                "trigger_phrases": ["\u5185\u5b58\u600e\u4e48\u6837"],
            },
            {
                "id": "lof-monitor",
                "name": "LOF \u4f30\u503c\u96f7\u8fbe",
                "description": "LOF/QDII \u5b9e\u65f6\u4f30\u503c\u548c\u63a8\u9001\u62a5\u544a",
                "category": "\u6295\u8d44",
                "enabled": True,
                "trigger_phrases": ["LOF \u6709\u673a\u4f1a\u5417"],
            },
        ],
    )

    out = build_direct_reply(_msg("\u4f60\u4f1a\u4ec0\u4e48"), model="test-model", start_time=0)

    assert out is not None
    assert "\u672a\u8c03\u7528 LLM" in out.content
    assert "\u8fd0\u7ef4\u95ee\u7b54" in out.content
    assert "LOF \u4f30\u503c\u96f7\u8fbe" in out.content
    assert out.metadata["_direct_reply"] is True


def test_capability_status_uses_dashboard_api_without_llm(monkeypatch) -> None:
    def fake_fetch(path: str, default):
        if path == "/api/capabilities":
            return {
                "summary": {"total": 11, "enabled": 11, "healthy": 11},
                "items": [],
            }
        if path == "/api/sidecars":
            return {
                "summary": {"total": 9, "healthy": 9},
                "items": [],
            }
        return default

    monkeypatch.setattr(direct_reply, "_fetch_dashboard_json", fake_fetch)

    out = build_direct_reply(_msg("\u670d\u52a1\u72b6\u6001"), model="test-model", start_time=0)

    assert out is not None
    assert "\u80fd\u529b\uff1a11 / 11" in out.content
    assert "\u670d\u52a1\uff1a9 / 9" in out.content
    assert "\u6682\u65e0" in out.content


def test_today_brief_uses_dashboard_data_without_llm(monkeypatch) -> None:
    def fake_fetch(path: str, default):
        if path == "/api/system":
            return {"memory": {"used_mb": 410, "total_mb": 1966}}
        if path == "/api/sidecars":
            return {"summary": {"total": 9, "healthy": 9}, "items": []}
        if path == "/api/capabilities":
            return {"summary": {"total": 11, "healthy": 11}, "items": []}
        if path == "/api/notify-jobs":
            return {"job_details": [{"id": "weather", "status": {"last_status": "ok"}}]}
        if path == "/rss/api/entries?days=1&limit=5":
            return {"items": [{"title": "\u6d4b\u8bd5\u6587\u7ae0"}]}
        if path == "/api/status":
            return {"last_board": {"rows": [{"code": "161129", "name": "\u539f\u6cb9", "rt_premium_pct": 6.2}]}}
        return default

    monkeypatch.setattr(direct_reply, "_fetch_dashboard_json", fake_fetch)

    out = build_direct_reply(_msg("\u4eca\u5929\u5148\u770b\u4ec0\u4e48"), model="test-model", start_time=0)

    assert out is not None
    assert "\u4eca\u65e5\u6458\u8981" in out.content
    assert "410 / 1966 MB" in out.content
    assert "161129" in out.content
    assert "\u6d4b\u8bd5\u6587\u7ae0" in out.content


def test_non_status_message_falls_through() -> None:
    assert build_direct_reply(_msg("\u5e2e\u6211\u5199\u4e00\u6bb5\u603b\u7ed3"), model="test-model", start_time=0) is None
