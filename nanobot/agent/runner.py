"""Shared execution loop for tool-using agents (REPAIRED FULL - NO ESCAPES)."""
from __future__ import annotations
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from nanobot.agent.hook import AgentHook, AgentHookContext     
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider, ToolCallRequest
from nanobot.utils.helpers import build_assistant_message

logger = logging.getLogger(__name__)
_DEFAULT_MAX_ITERATIONS_MESSAGE = "Reached maximum iterations ({max_iterations})."
_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."

@dataclass(slots=True)
class AgentRunSpec:
    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    model: str
    max_iterations: int
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False

@dataclass(slots=True)
class AgentRunResult:
    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)

class AgentRunner:
    def __init__(self, provider: LLMProvider):
        self.provider = provider
    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        hook = spec.hook or AgentHook()
        messages = list(spec.initial_messages)
        final_content, tools_used, usage, error = None, [], {"prompt_tokens": 0, "completion_tokens": 0}, None
        stop_reason = "completed"
        tool_events = []
        for iteration in range(spec.max_iterations):
            context = AgentHookContext(iteration=iteration, messages=messages)
            await hook.before_iteration(context)
            kwargs = {"messages": messages, "tools": spec.tools.get_definitions(), "model": spec.model}
            if spec.temperature is not None: kwargs["temperature"] = spec.temperature
            if spec.max_tokens is not None: kwargs["max_tokens"] = spec.max_tokens
            try:
                if hook.wants_streaming():
                    async def _stream(delta: str): await hook.on_stream(context, delta)
                    response = await self.provider.chat_stream_with_retry(**kwargs, on_content_delta=_stream)
                else: response = await self.provider.chat_with_retry(**kwargs)
            except Exception as e:
                err_str = str(e).lower()
                if "429" in err_str or "rate limit" in err_str:
                    logger.warning("429 detected! Writing pending_switch...")
                    try: (Path.home() / ".nanobot/pending_switch").write_text(json.dumps({"event": "429", "at": datetime.now().isoformat()}))
                    except: pass
                raise e
            context.response, context.usage, context.tool_calls = response, response.usage, list(response.tool_calls)
            if response.has_tool_calls:
                if hook.wants_streaming(): await hook.on_stream_end(context, resuming=True)
                await hook.before_execute_tools(context)
                messages.append(build_assistant_message(response.content or "", tool_calls=[tc.to_openai_tool_call() for tc in context.tool_calls]))
                tools_used.extend(tc.name for tc in context.tool_calls)
                results, new_events, fatal_error = await self._execute_tools(spec, context.tool_calls)
                tool_events.extend(new_events)
                for tool_call, result in zip(context.tool_calls, results):
                    try: await hook.after_tool_call(tool_call.name, tool_call.arguments, result)
                    except: pass
                if fatal_error:
                    error = f"Error: {fatal_error}"; stop_reason = "tool_error"; break
                for tool_call, result in zip(context.tool_calls, results):
                    messages.append({"role": "tool", "tool_call_id": tool_call.id, "name": tool_call.name, "content": str(result)})
                continue
            if hook.wants_streaming(): await hook.on_stream_end(context, resuming=False)
            final_content = response.content; messages.append(build_assistant_message(final_content)); break
        return AgentRunResult(final_content=final_content, messages=messages, tools_used=tools_used, usage=usage, stop_reason=stop_reason, error=error, tool_events=tool_events)
    async def _execute_tools(self, spec, tool_calls):
        results, events, fatal_error = [], [], None
        for tc in tool_calls:
            try:
                res = await spec.tools.execute(tc.name, tc.arguments)
                results.append(res); events.append({"name": tc.name, "status": "ok"})
            except Exception as e:
                results.append(f"Error: {e}"); events.append({"name": tc.name, "status": "error"}); fatal_error = e
        return results, events, fatal_error
