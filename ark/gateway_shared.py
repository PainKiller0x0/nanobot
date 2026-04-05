"""
共享的 gateway 组件初始化逻辑

commands.gateway() 和 ShadowGateway._run_gateway() 都用这个，
避免重复维护两份相同的初始化代码。
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config
from loguru import logger
from rich.console import Console

# Shared console instance (used by _make_provider for error messages)
_console = Console()
from nanobot.cron.service import CronService
from nanobot.heartbeat.service import HeartbeatService
from nanobot.session.manager import SessionManager


def _load_runtime_config(config_arg: str | None, workspace_arg: str | None) -> Config:
    """
    加载 runtime config（直接复用 commands._load_runtime_config）。

    参数优先级: config_arg > workspace_arg > 默认 config.json
    """
    from nanobot.cli.commands import _load_runtime_config as _original
    return _original(config_arg, workspace_arg)


def _make_provider(config: Config) -> Any:
    """Create the appropriate LLM provider from config."""
    import os
    from nanobot.providers.base import GenerationSettings
    from nanobot.providers.registry import find_by_name

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)
    spec = find_by_name(provider_name) if provider_name else None
    backend = spec.backend if spec else "openai_compat"

    # Env var names by backend
    _ENV_MAP = {
        "openai_compat": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "azure_openai": "AZURE_OPENAI_API_KEY",
        "openai_codex": "OPENAI_API_KEY",
    }
    env_var = _ENV_MAP.get(backend, "NANOBOT_API_KEY")

    def resolve_key(cfg_key: str | None) -> str | None:
        # Env var takes precedence over config (config may be read by agent via tools)
        env_key = os.environ.get(env_var) if env_var else None
        if env_key:
            return env_key
        generic_key = os.environ.get("NANOBOT_API_KEY")
        if generic_key:
            return generic_key
        return cfg_key

    # --- validation ---
    if backend == "azure_openai":
        if not p or not (p.api_key or os.environ.get(env_var)) or not p.api_base:
            _console.print("[red]Error: Azure OpenAI requires api_key and api_base.[/red]")
            _console.print("Set api_key via AZURE_OPENAI_API_KEY env var or config.")
            _console.print("Set api_base in ~/.nanobot/config.json under providers.azure_openai section")
            raise SystemExit(1)
    elif backend == "openai_compat" and not model.startswith("bedrock/"):
        needs_key = not (p and p.api_key) and not os.environ.get(env_var)
        exempt = p and p.api_base and "localhost" in p.api_base
        if needs_key and not exempt:
            _console.print("[red]Error: No API key configured.[/red]")
            _console.print("Set one via environment variable (e.g. OPENAI_API_KEY) or")
            _console.print("in ~/.nanobot/config.json under providers section.")
            raise SystemExit(1)

    # --- instantiation by backend ---
    if backend == "openai_codex":
        from nanobot.providers.openai_codex_provider import OpenAICodexProvider
        provider = OpenAICodexProvider(default_model=model)
    elif backend == "azure_openai":
        from nanobot.providers.azure_openai_provider import AzureOpenAIProvider
        provider = AzureOpenAIProvider(
            api_key=resolve_key(p.api_key),
            api_base=p.api_base,
            default_model=model,
        )
    elif backend == "anthropic":
        from nanobot.providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider(
            api_key=resolve_key(p.api_key if p else None),
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )
    else:
        from nanobot.providers.openai_compat_provider import OpenAICompatProvider
        provider = OpenAICompatProvider(
            api_key=resolve_key(p.api_key if p else None),
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
            spec=spec,
        )

    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )
    return provider


async def build_gateway(
    config: Config,
    console: Any = None,
    shutdown_event: asyncio.Event | None = None,
) -> dict[str, Any]:
    """
    构建 gateway 所有组件，返回 dict 包含:

    - bus: MessageBus
    - provider: LLM provider
    - session_manager: SessionManager
    - agent: AgentLoop
    - channels: ChannelManager
    - cron: CronService
    - heartbeat: HeartbeatService
    - shutdown_event: asyncio.Event (caller负责set)
    """
    import os
    from rich.console import Console

    if console is None:
        console = Console()

    if shutdown_event is None:
        shutdown_event = asyncio.Event()

    # ARK shadow gateway：使用 standby slot 的 workspace（通过环境变量传入）
    workspace_override = os.environ.get("ARK_SLOT_WORKSPACE")
    if workspace_override:
        config.agents.defaults.workspace = str(Path(workspace_override).expanduser())
        logger.debug(f"Using ARK slot workspace: {workspace_override}")

    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    # Cron service: workspace-scoped store
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Agent loop
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        timezone=config.agents.defaults.timezone,
    )

    channels = ChannelManager(config, bus)

    # Heartbeat service
    hb_cfg = config.gateway.heartbeat
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=agent.model,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
        timezone=config.agents.defaults.timezone,
    )

    loop = asyncio.get_running_loop()

    def _sigterm_handler():
        console.print("[yellow]Received SIGTERM[/yellow]")
        asyncio.create_task(_gs())

    try:
        loop.add_signal_handler(signal.SIGTERM, _sigterm_handler)
    except NotImplementedError:
        pass  # Windows

    async def _gs():
        """Shared graceful shutdown sequence."""
        console.print("[yellow]Graceful shutdown in progress...[/yellow]")
        agent.stop()
        heartbeat.stop()
        cron.stop()
        try:
            for key, session in session_manager._cache.items():
                session_manager.save(session)
            console.print(f"[green]✓[/green] Sessions saved ({len(session_manager._cache)})")
        except Exception:
            console.print("[red]✗[/red] Session save failed")
        await channels.stop_all()
        await agent.close_mcp()
        shutdown_event.set()

    return {
        "bus": bus,
        "provider": provider,
        "session_manager": session_manager,
        "agent": agent,
        "channels": channels,
        "cron": cron,
        "heartbeat": heartbeat,
        "shutdown_event": shutdown_event,
        "console": console,
        "graceful_shutdown": _gs,
    }
