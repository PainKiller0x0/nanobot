"""Warm up the configured LLM provider without touching live sessions."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import Any, Iterator

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.session.manager import Session, SessionManager

_DEFAULT_PROMPT = "Warm-up request. Do not use tools. Reply with OK only."
_SKIP_PREFIXES = ("cron:", "cli:", "system:", "test:", "heartbeat")
_DEFAULT_PREFER = ("qq:", "weixin:")


def select_warmup_sessions(
    session_infos: list[dict[str, Any]],
    *,
    limit: int = 1,
    prefer_prefixes: tuple[str, ...] = _DEFAULT_PREFER,
) -> list[str]:
    """Pick recent human-facing sessions, preferring interactive channels."""
    if limit <= 0:
        return []

    candidates: list[tuple[int, int, str]] = []
    for index, info in enumerate(session_infos):
        key = str(info.get("key") or "")
        if not key or key.startswith(_SKIP_PREFIXES):
            continue
        if ":" not in key:
            continue
        rank = len(prefer_prefixes)
        for pos, prefix in enumerate(prefer_prefixes):
            if key.startswith(prefix):
                rank = pos
                break
        candidates.append((rank, index, key))

    candidates.sort(key=lambda item: (item[0], item[1]))
    return [key for _, _, key in candidates[:limit]]


def split_session_key(key: str) -> tuple[str, str]:
    if ":" not in key:
        return "cli", key
    channel, chat_id = key.split(":", 1)
    return channel or "cli", chat_id or "direct"


def _history_for(loop: AgentLoop, session: Session) -> list[dict[str, Any]]:
    return session.get_history(
        max_messages=loop._max_messages,
        max_tokens=loop._replay_token_budget(),
        include_timestamps=True,
    )


async def warm_session(
    loop: AgentLoop,
    session: Session,
    *,
    timeout_s: float,
    max_tokens: int,
    prompt: str = _DEFAULT_PROMPT,
) -> dict[str, Any]:
    """Send one low-output request with the same prompt shape as normal chat."""
    channel, chat_id = split_session_key(session.key)
    messages = loop.context.build_messages(
        history=_history_for(loop, session),
        current_message=prompt,
        channel=channel,
        chat_id=chat_id,
    )
    kwargs: dict[str, Any] = {
        "messages": messages,
        "tools": loop.tools.get_definitions(),
        "model": loop.model,
        "retry_mode": loop.provider_retry_mode,
        "max_tokens": max_tokens,
    }
    generation = getattr(loop.provider, "generation", None)
    temperature = getattr(generation, "temperature", None)
    reasoning_effort = getattr(generation, "reasoning_effort", None)
    if temperature is not None:
        kwargs["temperature"] = temperature
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort

    started = time.perf_counter()
    response = await asyncio.wait_for(
        loop.provider.chat_with_retry(**kwargs),
        timeout=timeout_s,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    usage = getattr(response, "usage", None) or {}
    return {
        "session": session.key,
        "elapsed_ms": elapsed_ms,
        "usage": usage,
        "finish_reason": getattr(response, "finish_reason", None),
        "content_preview": (getattr(response, "content", "") or "")[:80],
    }


def make_loop(config: Any) -> AgentLoop:
    from nanobot.cli.commands import _make_provider

    provider = _make_provider(config)
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=1,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        history_replay_tokens=config.agents.defaults.history_replay_tokens,
        eager_compact_tokens=0,
        startup_warm_sessions=0,
        context_block_limit=config.agents.defaults.context_block_limit,
        max_tool_result_chars=config.agents.defaults.max_tool_result_chars,
        provider_retry_mode=config.agents.defaults.provider_retry_mode,
        web_config=config.tools.web,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=SessionManager(config.workspace_path),
        mcp_servers={},
        channels_config=config.channels,
        timezone=config.agents.defaults.timezone,
        unified_session=config.agents.defaults.unified_session,
        disabled_skills=config.agents.defaults.disabled_skills,
        session_ttl_minutes=0,
        max_messages=config.agents.defaults.max_messages,
        consolidation_ratio=config.agents.defaults.consolidation_ratio,
        tools_config=config.tools,
    )


@contextlib.contextmanager
def _single_process_lock(path: str) -> Iterator[bool]:
    """Best-effort non-blocking lock; no-op on platforms without fcntl."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        try:
            import fcntl  # type: ignore
        except Exception:
            yield True
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


async def run_warmup(args: argparse.Namespace) -> int:
    from nanobot.cli.commands import _load_runtime_config

    with _single_process_lock(args.lock_file) as acquired:
        if not acquired:
            if not args.quiet:
                print("LLM warmup skipped: another warmup is running")
            return 0

        config = _load_runtime_config(args.config, args.workspace)
        loop = make_loop(config)
        session_manager = loop.sessions
        keys = [args.session] if args.session else select_warmup_sessions(
            session_manager.list_sessions(),
            limit=args.limit,
            prefer_prefixes=tuple(args.prefer),
        )
        if not keys:
            if not args.quiet:
                print("LLM warmup skipped: no eligible sessions")
            return 0

        results = []
        failures = []
        for key in keys:
            session = session_manager.get_or_create(key)
            if not session.messages:
                continue
            try:
                results.append(await warm_session(
                    loop,
                    session,
                    timeout_s=args.timeout,
                    max_tokens=args.max_tokens,
                    prompt=args.prompt,
                ))
            except Exception as exc:
                failures.append(f"{key}: {exc}")
        await loop.close_mcp()

        if not args.quiet:
            for item in results:
                print(
                    "LLM warmup ok: "
                    f"session={item['session']} elapsed_ms={item['elapsed_ms']} "
                    f"usage={item['usage']}"
                )
            for item in failures:
                print(f"LLM warmup failed: {item}")
        return 1 if failures and not results else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Warm the configured nanobot LLM provider.")
    parser.add_argument("--config", default=None)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--session", default=os.environ.get("NANOBOT_LLM_WARMUP_SESSION"))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("NANOBOT_LLM_WARMUP_SESSIONS", "1")))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("NANOBOT_LLM_WARMUP_TIMEOUT_S", "120")))
    parser.add_argument("--max-tokens", type=int, default=int(os.environ.get("NANOBOT_LLM_WARMUP_MAX_TOKENS", "24")))
    parser.add_argument("--prompt", default=os.environ.get("NANOBOT_LLM_WARMUP_PROMPT", _DEFAULT_PROMPT))
    parser.add_argument("--prefer", action="append", default=[])
    parser.add_argument("--lock-file", default=os.environ.get("NANOBOT_LLM_WARMUP_LOCK", "/tmp/nanobot-llm-warmup.lock"))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    if not args.prefer:
        args.prefer = list(_DEFAULT_PREFER)
    return args


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run_warmup(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
