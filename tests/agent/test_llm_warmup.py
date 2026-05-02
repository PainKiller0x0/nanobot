from nanobot.agent.warmup import select_warmup_sessions, split_session_key


def test_select_warmup_sessions_prefers_interactive_recent_sessions() -> None:
    infos = [
        {"key": "cron:abc", "updated_at": "2026-05-02T22:00:00"},
        {"key": "weixin:wx-user", "updated_at": "2026-05-02T21:59:00"},
        {"key": "qq:qq-user", "updated_at": "2026-05-02T21:58:00"},
        {"key": "cli:direct", "updated_at": "2026-05-02T21:57:00"},
    ]

    assert select_warmup_sessions(infos, limit=2) == ["qq:qq-user", "weixin:wx-user"]


def test_select_warmup_sessions_falls_back_to_other_human_sessions() -> None:
    infos = [
        {"key": "matrix:room", "updated_at": "2026-05-02T22:00:00"},
        {"key": "cron:abc", "updated_at": "2026-05-02T21:59:00"},
    ]

    assert select_warmup_sessions(infos, limit=1) == ["matrix:room"]


def test_split_session_key_keeps_colons_in_chat_id() -> None:
    assert split_session_key("slack:room:thread") == ("slack", "room:thread")
