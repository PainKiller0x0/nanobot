"""Configuration loading utilities."""

import json
import os
from pathlib import Path

import pydantic
from loguru import logger

from nanobot.config.schema import Config

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".nanobot" / "config.json"


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            return Config.model_validate(data)
        except (json.JSONDecodeError, ValueError, pydantic.ValidationError) as e:
            logger.warning(f"Failed to load config from {path}: {e}")
            logger.warning("Using default configuration.")

    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(mode="json", by_alias=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    return data


# ---------------------------------------------------------------------------
# Feature Flags — environment variable overrides
# ---------------------------------------------------------------------------
# Allow runtime overrides of config values via environment variables.
# Format: NANOBOT_<section>_<key> (uppercase, underscores).
# Example: NANOBOT_AGENTS_CONTEXT_WINDOW_TOKENS=100000
#          NANOBOT_TOOLS_EXEC_TIMEOUT=30
#          NANOBOT_TOOLS_RESTRICT_TO_WORKSPACE=false


def _str_to_bool(v: str) -> bool:
    return v.lower() in ("true", "1", "yes", "on")


_FEATURE_FLAG_MAPPINGS: list[tuple[str, str, type]] = [
    # (env_var_name, dot_path_in_config, type_converter)
    ("NANOBOT_AGENTS_DEFAULTS_MODEL", "agents.defaults.model", str),
    ("NANOBOT_AGENTS_DEFAULTS_CONTEXT_WINDOW_TOKENS", "agents.defaults.context_window_tokens", int),
    ("NANOBOT_AGENTS_DEFAULTS_MAX_TOKENS", "agents.defaults.max_tokens", int),
    ("NANOBOT_TOOLS_EXEC_TIMEOUT", "tools.exec.timeout", int),
    ("NANOBOT_TOOLS_EXEC_ENABLE", "tools.exec.enable", _str_to_bool),
    ("NANOBOT_TOOLS_RESTRICT_TO_WORKSPACE", "tools.restrict_to_workspace", _str_to_bool),
    ("NANOBOT_COMPACTION_THRESHOLD", "agents.defaults.compaction.compaction_threshold", float),
    ("NANOBOT_COMPACTION_TARGET", "agents.defaults.compaction.compaction_target", float),
    ("NANOBOT_MAX_CONCURRENT_REQUESTS", "max_concurrent_requests", int),
]


def apply_feature_flags(config: Config) -> Config:
    """
    Override config values with environment variables (Feature Flags).

    This enables runtime configuration without editing config.json.
    Env vars take precedence.  Keys not set in env are untouched.

    Usage examples:
        NANOBOT_AGENTS_DEFAULTS_CONTEXT_WINDOW_TOKENS=100000
        NANOBOT_TOOLS_EXEC_TIMEOUT=30
        NANOBOT_COMPACTION_THRESHOLD=0.8
    """
    for env_var, dot_path, converter in _FEATURE_FLAG_MAPPINGS:
        val_str = os.environ.get(env_var)
        if val_str is None:
            continue
        try:
            val = converter(val_str)
            _set_by_dot_path(config, dot_path, val)
            logger.info("Feature flag active: {}={}", env_var, val)
        except Exception:
            logger.warning("Ignoring invalid {}={!r}", env_var, val_str)
    return config


def _set_by_dot_path(obj: object, dot_path: str, value: object) -> None:
    """Set a nested attribute on obj using dot-notation path."""
    parts = dot_path.split(".")
    target = obj
    for part in parts[:-1]:
        target = getattr(target, part)
    setattr(target, parts[-1], value)
