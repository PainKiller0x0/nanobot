"""LLM Provider Hot-Switch Module (REPAIRED FULL v2 - FIX METHODS)."""
from __future__ import annotations
import asyncio, re, threading, time
from datetime import datetime, timedelta
from typing import Literal
from loguru import logger
from nanobot.config.runtime_config import runtime_config

class LLMProviderSwitcher:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._lock = threading.Lock()
        self._active_provider = runtime_config.get("llm.active_provider", "normal")
        self._failed_provider = None
        self._recovery_time = None
        self._recovery_timer = None

    @classmethod
    def get_instance(cls) -> "LLMProviderSwitcher":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None: cls._instance = cls()
        return cls._instance

    def get_active_provider(self) -> str:
        config_val = runtime_config.get("llm.active_provider", "normal")
        if config_val != self._active_provider and not self._failed_provider:
            self._active_provider = config_val
        return self._active_provider

    def get_model(self) -> str:
        p = self.get_active_provider()
        return runtime_config.get(f"llm.providers.{p}.model", "MiniMax-M2.7")

    def get_provider_name(self) -> str:
        p = self.get_active_provider()
        return runtime_config.get(f"llm.providers.{p}.provider", "minimax")

    def should_disable_tools(self) -> bool:
        """FIXED: Added missing attribute required by AgentLoop."""
        p = self.get_active_provider()
        return runtime_config.get(f"llm.providers.{p}.disable_tools", False)

    def switch_to(self, provider: str, notify_callback=None) -> bool:
        with self._lock:
            if provider not in ("normal", "hard"): return False
            self._active_provider = provider
            self._failed_provider = None
            logger.info(f"LLM provider manual switch: {provider}")
            if notify_callback: notify_callback(f"Successfully switched to {provider} mode")
            return True

    def on_429_error(self, notify_callback=None) -> bool:
        with self._lock:
            if not runtime_config.get("llm.auto_switch", False) or self._active_provider == "hard":
                return False
            self._failed_provider = self._active_provider
            self._active_provider = "hard"
            logger.warning("429 detected, failing over to hard provider")
            if notify_callback: notify_callback("429 Error: Auto-switching to HARD mode")
            return True

    def get_status(self) -> dict:
        return {"active_provider": self.get_active_provider(), "auto_switch": runtime_config.get("llm.auto_switch", False)}

def parse_switch_command(message: str) -> Literal["normal", "hard", None]:
    m = message.lower()
    if any(k in m for k in ["困难模式", "hard", "认真模式"]): return "hard"
    if any(k in m for k in ["休闲模式", "normal", "摸鱼模式"]): return "normal"
    return None

llm_switcher = LLMProviderSwitcher.get_instance()
