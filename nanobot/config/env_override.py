"""Environment variable override for flexible configuration."""

from __future__ import annotations

import ast
import os
from typing import Any


class EnvOverride:
    """Environment variable override - environment variables take precedence over config files.

    Usage:
        # Define config
        config = {"model": "gpt-4", "max_tokens": 1000}

        # Apply env overrides
        EnvOverride.apply(config)
        # If NANOBOT_MODEL is set, config["model"] will be overridden

        # Or read a single key
        model = EnvOverride.get("model", default="gpt-3.5")
        # Returns NANOBOT_MODEL env var if set, otherwise default
    """

    PREFIX = "NANOBOT_"

    @staticmethod
    def get(key: str, default: Any = None) -> Any:
        """Get a config value, with environment variable taking precedence."""
        env_key = EnvOverride._env_key(key)
        return os.environ.get(env_key, default)

    @staticmethod
    def apply(config: dict) -> dict:
        """Apply environment variable overrides to a config dict."""
        result = dict(config)
        for key, config_value in config.items():
            env_key = EnvOverride._env_key(key)
            env_value = os.environ.get(env_key)
            if env_value is None:
                continue
            result[key] = EnvOverride._coerce(env_value, config_value)
        return result

    @staticmethod
    def _env_key(key: str) -> str:
        """Convert a config key to an env var name. Handles camelCase/snake_case."""
        # Normalize: maxTokens -> MAXTOKENS, max_tokens -> MAX_TOKENS
        normalized = key.upper().replace("_", "")
        return f"{EnvOverride.PREFIX}{normalized}"

    @staticmethod
    def _coerce(env_value: str, config_value: Any) -> Any:
        """Coerce env string to match the type of config_value."""
        # JSON for complex types
        if env_value.startswith("{") or env_value.startswith("["):
            try:
                return ast.literal_eval(env_value)
            except (ValueError, SyntaxError):
                pass

        # bool
        if isinstance(config_value, bool):
            return env_value.lower() in ("true", "1", "yes", "on")

        # int
        if isinstance(config_value, int):
            try:
                return int(env_value)
            except ValueError:
                pass

        # float
        if isinstance(config_value, float):
            try:
                return float(env_value)
            except ValueError:
                pass

        # string (default)
        return env_value
