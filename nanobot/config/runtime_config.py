"""Runtime configuration manager with ENV support (REPAIRED FULL - NO ESCAPES)."""
from __future__ import annotations
import os, threading, re, yaml
from pathlib import Path
from typing import Any
from loguru import logger
from dotenv import load_dotenv

_DEFAULTS = {
    "llm.auto_switch": True, "llm.active_provider": "normal",
    "llm.providers.normal.provider": "oneapi", "llm.providers.normal.model": "LongCat-Flash-Lite",
    "llm.providers.hard.provider": "minimax", "llm.providers.hard.model": "MiniMax-M2.7"
}
_CONFIG_PATH = Path.home() / ".nanobot/runtime_config.yaml"

def _replace_env_vars(data: Any) -> Any:
    if isinstance(data, dict): return {k: _replace_env_vars(v) for k, v in data.items()}
    if isinstance(data, list): return [_replace_env_vars(v) for v in data]
    if isinstance(data, str):
        for var in re.findall(r'\$\{(.*?)\}', data):
            data = data.replace(f"${{{var}}}", os.getenv(var, f"${{{var}}}"))
        return data
    return data

class RuntimeConfigManager:
    _instance = None
    def __init__(self, config_path=None):
        self.config_path = config_path or _CONFIG_PATH
        self._overrides = {}; self._listeners = []
        load_dotenv(Path("/root/nanobot/.env"))
        self._load()
    @classmethod
    def get_instance(cls, path=None):
        if not cls._instance: cls._instance = cls(path)
        return cls._instance
    def _load(self):
        if not self.config_path.exists(): return
        try:
            with open(self.config_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            self._overrides = self._flatten(_replace_env_vars(data))
        except: self._overrides = {}
    def _flatten(self, data, prefix=""):
        res = {}
        for k, v in data.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict): res.update(self._flatten(v, key))
            else: res[key] = v
        return res
    def get(self, key, default=None):
        return self._overrides.get(key, _DEFAULTS.get(key, default))
    def start_polling(self): pass

runtime_config = RuntimeConfigManager.get_instance()
