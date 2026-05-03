from nanobot.agent import capability_reply, inbox_reply, memory_reply
from nanobot.agent.direct_reply import build_direct_reply
from nanobot.bus.events import InboundMessage


def _msg(content: str) -> InboundMessage:
    return InboundMessage(channel="qq", sender_id="user", chat_id="chat", content=content)


def test_knowledge_inbox_capture_uses_skill_without_llm(monkeypatch) -> None:
    calls = []

    def fake_run(args, *, user_id=None):
        calls.append((args, user_id))
        return "📥 已入收件箱：Example"

    monkeypatch.setattr(inbox_reply, "_run_tool", fake_run)

    out = build_direct_reply(_msg("收一下 https://example.com/a"), model="test-model", start_time=0)

    assert out is not None
    assert "已入收件箱" in out.content
    assert "未调用 LLM" in out.content
    assert "8093/inbox" in out.content
    assert calls == [(["capture", "https://example.com/a"], "user")]
    assert out.metadata["_direct_reply"] is True


def test_knowledge_inbox_decide_uses_skill_without_llm(monkeypatch) -> None:
    calls = []

    def fake_run(args, *, user_id=None):
        calls.append((args, user_id))
        return "🧠 决策包：Example"

    monkeypatch.setattr(inbox_reply, "_run_tool", fake_run)

    out = build_direct_reply(_msg("这个值得看吗 https://example.com/a"), model="test-model", start_time=0)

    assert out is not None
    assert "决策包" in out.content
    assert calls[0][0][:2] == ["decide", "https://example.com/a"]
    assert "--question" in calls[0][0]


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
        capability_reply,
        "load_capabilities",
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

    monkeypatch.setattr(capability_reply, "dashboard_json", fake_fetch)

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

    monkeypatch.setattr(capability_reply, "dashboard_json", fake_fetch)

    out = build_direct_reply(_msg("\u4eca\u5929\u5148\u770b\u4ec0\u4e48"), model="test-model", start_time=0)

    assert out is not None
    assert "\u4eca\u65e5\u6458\u8981" in out.content
    assert "410 / 1966 MB" in out.content
    assert "161129" in out.content
    assert "\u6d4b\u8bd5\u6587\u7ae0" in out.content


def test_evolution_query_uses_evolution_api_without_llm(monkeypatch) -> None:
    def fake_fetch(path: str, default):
        if path == "/api/evolution":
            return {
                "summary": {"total": 2, "recent_7d": 2},
                "items": [
                    {
                        "date": "2026-05-03",
                        "title": "\u76f4\u8fde\u56de\u590d\u63d0\u901f",
                        "impact": "\u72b6\u6001\u95ee\u9898\u4e0d\u518d\u8d70 LLM",
                        "metrics": [{"label": "\u5ef6\u8fdf", "after": "0.3s"}],
                    }
                ],
            }
        return default

    monkeypatch.setattr(capability_reply, "dashboard_json", fake_fetch)

    out = build_direct_reply(_msg("\u4f60\u6700\u8fd1\u8fdb\u5316\u4e86\u5417"), model="test-model", start_time=0)

    assert out is not None
    assert "\u8fdb\u5316\u62a5\u544a" in out.content
    assert "\u76f4\u8fde\u56de\u590d\u63d0\u901f" in out.content
    assert out.metadata["_direct_reply"] is True


def test_remember_query_writes_local_memory_without_llm(monkeypatch) -> None:
    def fake_post(path: str, payload, default):
        assert path == "/reflexio/api/memories"
        assert payload["content"] == "我喜欢 Rust sidecar"
        assert payload["category"] == "preference"
        return {"success": True, "id": 7}

    monkeypatch.setattr(memory_reply, "post_json", fake_post)

    out = build_direct_reply(_msg("记住 我喜欢 Rust sidecar"), model="test-model", start_time=0)

    assert out is not None
    assert "记住了" in out.content
    assert "未调用 LLM" in out.content
    assert "#7" in out.content


def test_memory_status_uses_reflexio_without_llm(monkeypatch) -> None:
    def fake_get(path: str, default):
        if path == "/reflexio/api/stats":
            return {"total_memories": 2, "latest_memory_at": "2026-05-03 10:00:00", "total_interactions": 37, "total_facts": 0}
        if path == "/reflexio/api/memories?limit=5":
            return [{"content": "网页尽量纯中文", "category": "preference"}]
        return default

    monkeypatch.setattr(memory_reply, "get_json", fake_get)

    out = build_direct_reply(_msg("记忆状态"), model="test-model", start_time=0)

    assert out is not None
    assert "本地记忆：2 条" in out.content
    assert "网页尽量纯中文" in out.content


def test_memory_search_uses_reflexio_without_llm(monkeypatch) -> None:
    def fake_post(path: str, payload, default):
        assert path == "/reflexio/api/memory/search"
        assert payload["query"] == "Rust"
        return {"results": [{"id": 3, "content": "优先 Rust sidecar", "category": "preference", "created_at": "2026-05-03 10:00:00"}]}

    monkeypatch.setattr(memory_reply, "post_json", fake_post)

    out = build_direct_reply(_msg("查记忆 Rust"), model="test-model", start_time=0)

    assert out is not None
    assert "本地记忆搜索" in out.content
    assert "优先 Rust sidecar" in out.content


def test_non_status_message_falls_through() -> None:
    assert build_direct_reply(_msg("\u5e2e\u6211\u5199\u4e00\u6bb5\u603b\u7ed3"), model="test-model", start_time=0) is None
