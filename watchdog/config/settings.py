"""Watchdog configuration — loads from watchdog.yaml in the workspace."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG = """
# Watchdog configuration
# All paths are relative to this file's directory unless absolute.

workspace: ~/.nanobot

# How often to poll monitors (seconds)
poll_interval: 1.0

# Log level
log_level: INFO

# Rules: monitor_name -> action_name
# Supports exact match (gateway.main_dead) or prefix (gateway → gateway.*)
rules:
  gateway: ark.activate_shadow
  ark: ark.restart_ark
  qq: ark.notify

# Monitor-specific settings
monitors:
  gateway:
    main_pid_file: ~/.nanobot/gateway_main.pid
    shadow_pid_file: ~/.nanobot/gateway_shadow.pid
    check_interval: 5.0
    grace_period: 60.0    # don't alert if main died within this many seconds of start

  ark:
    check_interval: 30.0
    restart_command:
      - python3
      - -m
      - nanobot
      - ark
      - start

  qq:
    log_file: ~/.nanobot/workspace/lof_monitor/nanobot_gateway.log
    check_interval: 10.0
    reconnect_keywords:
      - "4009"
      - "reconnect"
      - "Session timed out"
      - "QQ bot disconnected"
"""


def _default_config_path() -> Path:
    return Path.home() / ".nanobot" / "watchdog.yaml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load watchdog config from YAML file, falling back to defaults."""
    path = path or _default_config_path()

    if path.exists():
        with open(path) as f:
            config = yaml.safe_load(f)
        return _resolve_paths(config, path.parent)

    # No config file — create from defaults
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG.strip())
    return load_config(path)


def _resolve_paths(config: dict, base: Path) -> dict:
    """Expand ~ in path fields."""
    def expand(v):
        if isinstance(v, str):
            return os.path.expanduser(v)
        if isinstance(v, list):
            return [expand(x) for x in v]
        if isinstance(v, dict):
            return {k: expand(val) for k, val in v.items()}
        return v

    return expand(config)
