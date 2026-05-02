from unittest.mock import MagicMock

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import AgentDefaults


def _provider(max_tokens: int = 4096):
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = max_tokens
    return provider


def test_agent_defaults_include_soft_replay_budget() -> None:
    defaults = AgentDefaults()

    assert defaults.history_replay_tokens == 16_000
    assert defaults.eager_compact_tokens == 0
    assert defaults.startup_warm_sessions == 0


def test_replay_budget_uses_soft_cap(tmp_path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_provider(max_tokens=4096),
        workspace=tmp_path,
        context_window_tokens=200_000,
        history_replay_tokens=12_000,
    )

    assert loop._replay_token_budget() == 12_000


def test_replay_budget_can_be_disabled(tmp_path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_provider(max_tokens=4096),
        workspace=tmp_path,
        context_window_tokens=32_000,
        history_replay_tokens=0,
    )

    assert loop._replay_token_budget() == 32_000 - 4096 - 1024
