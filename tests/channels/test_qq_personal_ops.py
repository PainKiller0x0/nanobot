from nanobot.bus.queue import MessageBus
from nanobot.channels.qq import QQChannel, QQConfig


def _channel() -> QQChannel:
    return QQChannel(QQConfig(app_id="app", secret="secret"), MessageBus())


def test_memory_query_is_not_captured_by_qq_ops_fast_path() -> None:
    channel = _channel()

    assert channel._match_personal_ops_command("内存怎么样") is None


def test_system_status_still_uses_qq_ops_fast_path() -> None:
    channel = _channel()

    assert channel._match_personal_ops_command("系统状态") == "system"
