"""Tests for token-based memory consolidation."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import store as memory_store_module
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse


def _make_loop(tmp_path, *, estimated_tokens: int, context_window_tokens: int, preserve_recent: int = 0) -> AgentLoop:
    """Create a test AgentLoop with mocked provider and configurable compaction."""
    from nanobot.providers.base import GenerationSettings
    from nanobot.config.schema import CompactionConfig

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (estimated_tokens, "test-counter")
    _response = LLMResponse(content="ok", tool_calls=[])
    provider.chat_with_retry = AsyncMock(return_value=_response)
    provider.chat_stream_with_retry = AsyncMock(return_value=_response)

    compaction_config = CompactionConfig(
        threshold=0.75,
        target=0.35,
        preserve_recent=preserve_recent,
        safety_buffer=0,
    )

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=context_window_tokens,
        compaction_config=compaction_config,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop


@pytest.mark.asyncio
async def test_prompt_below_threshold_does_not_consolidate(tmp_path) -> None:
    """Verify consolidation doesn't trigger when prompt is below threshold."""
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)
    loop.memory_consolidator.consolidate_messages = AsyncMock(return_value=True)

    await loop.process_direct("hello", session_key="cli:test")

    loop.memory_consolidator.consolidate_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_prompt_above_threshold_triggers_consolidation(tmp_path, monkeypatch) -> None:
    """Verify consolidation triggers when prompt exceeds threshold."""
    loop = _make_loop(tmp_path, estimated_tokens=1000, context_window_tokens=200)
    loop.memory_consolidator.consolidate_messages = AsyncMock(return_value=True)
    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
    ]
    loop.sessions.save(session)
    monkeypatch.setattr(memory_store_module, "estimate_message_tokens", lambda _message: 500)

    await loop.process_direct("hello", session_key="cli:test")

    assert loop.memory_consolidator.consolidate_messages.await_count >= 1


@pytest.mark.asyncio
async def test_preflight_consolidation_before_llm_call(tmp_path, monkeypatch) -> None:
    """Verify preflight consolidation runs before the LLM call in process_direct."""
    order: list[str] = []

    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)

    async def track_consolidate(messages):
        order.append("consolidate")
        return True
    loop.memory_consolidator.consolidate_messages = track_consolidate

    async def track_llm(*args, **kwargs):
        order.append("llm")
        return LLMResponse(content="ok", tool_calls=[])
    loop.provider.chat_with_retry = track_llm
    loop.provider.chat_stream_with_retry = track_llm

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
    ]
    loop.sessions.save(session)
    monkeypatch.setattr(memory_store_module, "estimate_message_tokens", lambda _m: 500)

    call_count = [0]
    def mock_estimate(_session):
        call_count[0] += 1
        return (1000 if call_count[0] <= 1 else 80, "test")
    loop.memory_consolidator.estimate_session_prompt_tokens = mock_estimate

    await loop.process_direct("hello", session_key="cli:test")

    assert "consolidate" in order
    assert "llm" in order
    assert order.index("consolidate") < order.index("llm")


@pytest.mark.asyncio
async def test_preserve_recent_boundary(tmp_path, monkeypatch) -> None:
    """Verify preserve_recent correctly sets the compactable boundary."""
    loop = _make_loop(tmp_path, estimated_tokens=1000, context_window_tokens=200, preserve_recent=1)

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    # With preserve_recent=1, only the last user (u2 at index 2) is protected
    # So u1 and a1 should be compactable, boundary at index 2
    result = loop.memory_consolidator.pick_consolidation_boundary(session, tokens_to_remove=100)

    assert result is not None
    # Boundary should be at index 2 (after a1, before u2)
    assert result[0] == 2


@pytest.mark.asyncio
async def test_preserve_recent_zero_allows_full_compaction(tmp_path, monkeypatch) -> None:
    """Verify preserve_recent=0 allows all messages to be compacted."""
    loop = _make_loop(tmp_path, estimated_tokens=1000, context_window_tokens=200, preserve_recent=0)

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    # With preserve_recent=0, all messages are compactable
    # Boundary at index 2 (first protected user)
    result = loop.memory_consolidator.pick_consolidation_boundary(session, tokens_to_remove=100)

    assert result is not None
    assert result[0] == 2
