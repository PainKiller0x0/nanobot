"""Action base class — all recovery actions inherit from this."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import ClassVar

logger = logging.getLogger("watchdog.actions")


class Action(ABC):
    """Base class for all actions.

    Subclass must implement `execute()`.
    Set `name` to match the rule key in watchdog.yaml.
    """

    name: ClassVar[str] = "base_action"

    # How long to suppress repeat triggers (seconds)
    lockout: ClassVar[float] = 60.0

    def __init__(self):
        self._last_run_time: float = 0.0

    def can_run(self) -> bool:
        """True if not in lockout period."""
        import time
        return (time.monotonic() - self._last_run_time) >= self.lockout

    def record_run(self):
        """Call after successful execution."""
        import time
        self._last_run_time = time.monotonic()

    @abstractmethod
    def execute(self, status) -> bool:
        """Run the action. Returns True on success.

        Args:
            status: HealthStatus that triggered this action.
        Returns:
            True if action succeeded, False otherwise.
        """

    async def async_execute(self, status) -> bool:
        """Wrapper — calls sync execute() in an async context."""
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return await loop.run_in_executor(None, self.execute, status)
        return self.execute(status)
