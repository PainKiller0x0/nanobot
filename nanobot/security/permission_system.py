"""
Permission system with three-tier model and denial-tracking.

Inspired by Claude Code's permission model:
  - allow:  auto-execute without prompting
  - ask:    prompt user before executing (default for most tools)
  - deny:   never auto-execute

Permission decisions are persisted to permissions.json so the user is not
asked the same question repeatedly.  Denial-tracking increments a counter
so repeatedly denied tools are automatically rejected without re-prompting.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from loguru import logger


class Decision(Enum):
    """Result of a permission check."""

    ALLOWED = "allowed"
    DENIED = "denied"
    ASK = "ask"


class PermissionSystem:
    """
    Three-tier permission system with persistent storage and denial-tracking.

    Tools declare their default permission level via ``tool.permission``.
    The system then checks stored decisions in ``permissions.json`` before
    deciding whether to ALLOW, DENY, or ASK the user.
    """

    def __init__(
        self,
        storage_path: Path | str,
        max_denials: int = 3,
    ):
        """
        Args:
            storage_path: Path to permissions.json
            max_denials: Auto-deny after this many consecutive denials (default 3)
        """
        self._path = Path(storage_path) if isinstance(storage_path, str) else storage_path
        self._max_denials = max_denials
        self._data = self._load()
        self._denial_counts: dict[str, int] = self._data.get("denial_counts", {})

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def check(self, tool_name: str, params: dict[str, Any] | None = None) -> Decision:
        """
        Check whether a tool call is allowed.

        Resolution order:
          1. If tool key is in denied-tools → DENIED
          2. If tool key is in allowed-tools → ALLOWED
          3. If denial count >= max_denials → DENIED
          4. Otherwise → ASK
        """
        key = self._tool_key(tool_name, params)

        if key in self._data.get("denied-tools", []):
            return Decision.DENIED

        if key in self._data.get("allowed-tools", []):
            return Decision.ALLOWED

        if self._denial_counts.get(key, 0) >= self._max_denials:
            logger.info("PermissionSystem: auto-deny {} (denied {} times)", key, self._denial_counts[key])
            return Decision.DENIED

        return Decision.ASK

    def record(self, tool_name: str, allowed: bool, params: dict[str, Any] | None = None) -> None:
        """
        Record a user's decision so future calls are handled automatically.

        Args:
            allowed: True if user allowed, False if denied
            tool_name: Name of the tool
            params: Tool parameters (used to form the key)
        """
        key = self._tool_key(tool_name, params)
        denied = self._data.setdefault("denied-tools", [])
        allowed_list = self._data.setdefault("allowed-tools", [])

        if allowed:
            if key in denied:
                denied.remove(key)
            if key not in allowed_list:
                allowed_list.append(key)
            self._denial_counts.pop(key, None)
            logger.info("PermissionSystem: permanently allowed {}", key)
        else:
            if key in allowed_list:
                allowed_list.remove(key)
            if key not in denied:
                denied.append(key)
            self._denial_counts[key] = self._denial_counts.get(key, 0) + 1
            logger.info(
                "PermissionSystem: permanently denied {} (count={})",
                key,
                self._denial_counts[key],
            )

        self._data["denial_counts"] = self._denial_counts
        self._save()

    def is_allowed(self, tool_name: str, params: dict[str, Any] | None = None) -> bool:
        """Convenience: returns True if the tool would auto-execute."""
        return self.check(tool_name, params) == Decision.ALLOWED

    def summary(self) -> dict[str, Any]:
        """Return current permission state for display/debugging."""
        return {
            "allowed": self._data.get("allowed-tools", []),
            "denied": self._data.get("denied-tools", []),
            "denial_counts": dict(self._denial_counts),
        }

    # -------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------

    def _tool_key(self, tool_name: str, params: dict[str, Any] | None) -> str:
        """
        Build a stable key for this tool+params combination.

        For tools with dangerous params (e.g. 'command' for exec), the
        key includes the first 32 chars of the dangerous value so that
        'rm -rf /tmp' and 'rm -rf /home' are tracked separately.
        """
        if not params:
            return tool_name

        # Keys on known dangerous param names only
        dangerous = {"command", "path", "file", "code", "script"}
        parts = [tool_name]
        for k, v in params.items():
            if k in dangerous and isinstance(v, str):
                # Truncate to avoid leaking sensitive values in storage
                parts.append(f"{k}={v[:32]}")

        return "|".join(parts)

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"allowed-tools": [], "denied-tools": [], "denial_counts": {}}
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"allowed-tools": [], "denied-tools": [], "denial_counts": {}}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.rename(self._path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
