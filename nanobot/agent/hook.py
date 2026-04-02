"""Shared lifecycle hook primitives for agent runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMResponse, ToolCallRequest


@dataclass(slots=True)
class AgentHookContext:
    """Mutable per-iteration state exposed to runner hooks."""

    iteration: int
    messages: list[dict[str, Any]]
    response: LLMResponse | None = None
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    tool_events: list[dict[str, str]] = field(default_factory=list)
    final_content: str | None = None
    stop_reason: str | None = None
    error: str | None = None


class AgentHook:
    """Minimal lifecycle surface for shared runner customization."""

    def wants_streaming(self) -> bool:
        return False

    async def before_iteration(self, context: AgentHookContext) -> None:
        pass

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        pass

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        pass

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        pass

    async def after_iteration(self, context: AgentHookContext) -> None:
        pass

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        return content

    async def on_session_start(self, session_key: str) -> None:
        """Called when a new session starts."""
        pass

    async def on_session_end(self, session_key: str) -> None:
        """Called when a session ends."""
        pass

    async def after_tool_call(self, tool_name: str, args: dict, result: Any) -> None:
        """Called after each tool call completes."""
        pass


class CompositeHook(AgentHook):
    """Fan-out hook that delegates to an ordered list of hooks.

    Error isolation: async methods catch and log per-hook exceptions
    so a faulty custom hook cannot crash the agent loop.
    ``finalize_content`` is a pipeline (no isolation — bugs should surface).
    """

    __slots__ = ("_hooks",)

    def __init__(self, hooks: list[AgentHook]) -> None:
        self._hooks = list(hooks)

    def wants_streaming(self) -> bool:
        return any(h.wants_streaming() for h in self._hooks)

    async def before_iteration(self, context: AgentHookContext) -> None:
        for h in self._hooks:
            try:
                await h.before_iteration(context)
            except Exception:
                logger.exception("AgentHook.before_iteration error in {}", type(h).__name__)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        for h in self._hooks:
            try:
                await h.on_stream(context, delta)
            except Exception:
                logger.exception("AgentHook.on_stream error in {}", type(h).__name__)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        for h in self._hooks:
            try:
                await h.on_stream_end(context, resuming=resuming)
            except Exception:
                logger.exception("AgentHook.on_stream_end error in {}", type(h).__name__)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for h in self._hooks:
            try:
                await h.before_execute_tools(context)
            except Exception:
                logger.exception("AgentHook.before_execute_tools error in {}", type(h).__name__)

    async def after_iteration(self, context: AgentHookContext) -> None:
        for h in self._hooks:
            try:
                await h.after_iteration(context)
            except Exception:
                logger.exception("AgentHook.after_iteration error in {}", type(h).__name__)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        for h in self._hooks:
            content = h.finalize_content(context, content)
        return content

    async def on_session_start(self, session_key: str) -> None:
        for h in self._hooks:
            try:
                await h.on_session_start(session_key)
            except Exception:
                logger.exception("AgentHook.on_session_start error in {}", type(h).__name__)

    async def on_session_end(self, session_key: str) -> None:
        for h in self._hooks:
            try:
                await h.on_session_end(session_key)
            except Exception:
                logger.exception("AgentHook.on_session_end error in {}", type(h).__name__)

    async def after_tool_call(self, tool_name: str, args: dict, result: Any) -> None:
        for h in self._hooks:
            try:
                await h.after_tool_call(tool_name, args, result)
            except Exception:
                logger.exception("AgentHook.after_tool_call error in {}", type(h).__name__)
