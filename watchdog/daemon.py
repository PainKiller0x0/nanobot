"""Watchdog daemon — the main polling loop.

Runs all monitors at their configured intervals and triggers actions on failure.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .monitors.base import HealthStatus, Monitor
    from .actions.base import Action

logger = logging.getLogger("watchdog.daemon")


class WatchdogDaemon:
    """Main watchdog daemon — polls monitors, triggers actions."""

    def __init__(self, config: dict):
        self.config = config
        self.workspace = Path(os.path.expanduser(config.get("workspace", "~/.nanobot")))
        self.poll_interval = config.get("poll_interval", 1.0)
        self._running = True

        # Built-in monitor registry: monitor instance keyed by all status names it can return
        _gw = _load_gateway_monitor(config)
        self._monitor_registry: dict[str, Monitor] = {
            "gateway.main_dead": _gw,
            "gateway.shadow_dead": _gw,   # same monitor, different status name
            "ark.both_dead": _load_ark_monitor(config),
            "qq.disconnected": _load_qq_monitor(config),
        }

        # Action registry: name -> instance
        self._action_registry: dict[str, Action] = {
            "ark.activate_shadow": _load_activate_shadow(config),
            "ark.start_shadow": _load_start_shadow(config),
            "ark.restart_ark": _load_restart_ark(config),
            "ark.notify": _load_notify(config),
        }

        # Rule registry: monitor_name -> action_name
        self._rules: dict[str, str] = config.get("rules", {})

        # Track last alert time per monitor to avoid spam
        self._last_alert: dict[str, float] = {}

    def _get_active_monitors(self) -> list[Monitor]:
        """Return all monitors that should run now (based on interval)."""
        return [m for m in self._monitor_registry.values() if m.should_check()]

    def _match_rule(self, status: HealthStatus) -> str | None:
        """Find matching action for this status. Checks exact name then prefix."""
        # Exact match first
        if status.name in self._rules:
            return self._rules[status.name]
        # Prefix match: "gateway.main_dead" matches "gateway.*"
        prefix = status.name.split(".")[0]
        if prefix in self._rules:
            return self._rules[prefix]
        return None

    def _lookup_action(self, action_name: str) -> Action | None:
        return self._action_registry.get(action_name)

    async def _run_cycle(self):
        """Run one polling cycle."""
        active = self._get_active_monitors()
        for monitor in active:
            status = await monitor.async_check()
            monitor.record_check()

            if status.healthy:
                logger.debug(f"[{monitor.name}] OK")
                continue

            # Unhealthy — check if we should alert (cooldown)
            if not monitor.should_alert(status):
                logger.debug(f"[{monitor.name}] unhealthy but in cooldown")
                continue

            # Find and run matching action
            action_name = self._match_rule(status)
            if not action_name:
                logger.warning(f"[{monitor.name}] unhealthy but no action configured: {status.reason}")
                continue

            action = self._lookup_action(action_name)
            if not action:
                logger.error(f"[{monitor.name}] action not found: {action_name}")
                continue

            if not action.can_run():
                logger.debug(f"[{monitor.name}] action {action_name} in lockout")
                continue

            logger.warning(f"[{monitor.name}] {status.reason} → executing {action_name}")
            try:
                ok = await action.async_execute(status)
                if ok:
                    action.record_run()
                    self._last_alert[monitor.name] = time.monotonic()
                    logger.info(f"[{monitor.name}] action {action_name} succeeded")
                else:
                    logger.error(f"[{monitor.name}] action {action_name} failed")
            except Exception as e:
                logger.exception(f"[{monitor.name}] action {action_name} raised: {e}")

    async def run(self):
        """Main loop."""
        logger.info(f"Watchdog started — workspace={self.workspace}, poll={self.poll_interval}s")
        logger.info(f"Monitors: {list(self._monitor_registry.keys())}")
        logger.info(f"Rules: {self._rules}")

        while self._running:
            try:
                await self._run_cycle()
            except Exception as e:
                logger.exception(f"Cycle error: {e}")

            await asyncio.sleep(self.poll_interval)

    def stop(self):
        logger.info("Watchdog shutting down")
        self._running = False


# ── Registry helpers ──────────────────────────────────────────────────────────


def _load_gateway_monitor(config: dict) -> "Monitor":
    from .monitors.gateway import GatewayMonitor
    mcfg = config.get("monitors", {}).get("gateway", {})
    return GatewayMonitor(
        main_pid_file=mcfg.get("main_pid_file", "~/.nanobot/gateway_main.pid"),
        shadow_pid_file=mcfg.get("shadow_pid_file", "~/.nanobot/gateway_shadow.pid"),
        grace_period=mcfg.get("grace_period", 60.0),
    )


def _load_ark_monitor(config: dict) -> "Monitor":
    from .monitors.ark import ArkMonitor
    mcfg = config.get("monitors", {}).get("ark", {})
    return ArkMonitor(
        workspace=config.get("workspace", "~/.nanobot"),
    )


def _load_qq_monitor(config: dict) -> "Monitor":
    from .monitors.qq import QqMonitor
    mcfg = config.get("monitors", {}).get("qq", {})
    return QqMonitor(
        log_file=mcfg.get("log_file", "~/.nanobot/slot_b/workspace/nanobot_gateway.log"),
        keywords=mcfg.get("reconnect_keywords"),
    )


def _load_activate_shadow(config: dict) -> "Action":
    from .actions.activate_shadow import ActivateShadowAction
    return ActivateShadowAction()


def _load_restart_ark(config: dict) -> "Action":
    from .actions.restart_ark import RestartArkAction
    acfg = config.get("monitors", {}).get("ark", {})
    return RestartArkAction(
        restart_command=acfg.get("restart_command"),
        workspace=config.get("workspace", "~/.nanobot"),
    )


def _load_notify(config: dict) -> "Action":
    from .actions.notify import NotifyAction
    return NotifyAction()


def _load_start_shadow(config: dict) -> "Action":
    from .actions.start_shadow import StartShadowAction
    return StartShadowAction(
        workspace=config.get("workspace", "~/.nanobot"),
    )
