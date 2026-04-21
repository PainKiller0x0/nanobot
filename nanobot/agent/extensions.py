"""Optional extension loading helpers for nanobot core."""

from __future__ import annotations

import importlib
import os
from typing import Any

from loguru import logger

from nanobot.agent.hook import AgentHook


def _iter_extension_modules() -> list[str]:
    raw = os.environ.get("NANOBOT_EXTENSION_MODULES", "")
    if not raw.strip():
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _load_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception:
        logger.exception("Failed to load extension module: {}", module_name)
        return None


def load_agent_hooks(bus: Any) -> list[AgentHook]:
    hooks: list[AgentHook] = []
    for module_name in _iter_extension_modules():
        mod = _load_module(module_name)
        if mod is None:
            continue

        builder = getattr(mod, "build_agent_hooks", None)
        if callable(builder):
            try:
                built = builder(bus=bus)
            except TypeError:
                built = builder()
            except Exception:
                logger.exception("Extension hook builder failed: {}", module_name)
                continue

            if isinstance(built, AgentHook):
                hooks.append(built)
            elif isinstance(built, list):
                hooks.extend([h for h in built if isinstance(h, AgentHook)])
            continue

        single_builder = getattr(mod, "build_agent_hook", None)
        if callable(single_builder):
            try:
                single = single_builder(bus=bus)
            except TypeError:
                single = single_builder()
            except Exception:
                logger.exception("Extension single hook builder failed: {}", module_name)
                continue
            if isinstance(single, AgentHook):
                hooks.append(single)

    return hooks


def build_context_blocks(current_message: str) -> list[str]:
    blocks: list[str] = []
    for module_name in _iter_extension_modules():
        mod = _load_module(module_name)
        if mod is None:
            continue
        fn = getattr(mod, "build_context_block", None)
        if not callable(fn):
            continue
        try:
            result = fn(current_message=current_message)
        except TypeError:
            result = fn(current_message)
        except Exception:
            logger.exception("Extension context block failed: {}", module_name)
            continue

        if isinstance(result, str) and result.strip():
            blocks.append(result.strip())
        elif isinstance(result, list):
            blocks.extend([str(item).strip() for item in result if str(item).strip()])

    return blocks
