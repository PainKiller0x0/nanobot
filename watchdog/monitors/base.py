"""Monitor base class — all health checks inherit from this."""

from __future__ import annotations

import time
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar


@dataclass
class HealthStatus:
    """Result of a single health check."""

    name: str
    healthy: bool
    reason: str = ""
    details: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.healthy


class Monitor(ABC):
    """Base class for all monitors.

    Subclass must implement `check()`.
    Override `interval` to change check frequency (seconds).
    Override `cooldown` to prevent alert flooding after recovery (seconds).
    """

    # Override in subclass
    interval: ClassVar[float] = 5.0      # seconds between checks
    cooldown: ClassVar[float] = 30.0    # seconds to wait after recovery before alerting again

    def __init__(self):
        self._last_check_time: float = 0.0
        self._last_unhealthy_time: float = 0.0
        self._healthy: bool = True

    @property
    def name(self) -> str:
        """Human-readable name, defaults to class name."""
        return self.__class__.__name__

    @abstractmethod
    def check(self) -> HealthStatus:
        """Perform one health check. Must be fast (sync or async)."""

    def should_check(self) -> bool:
        """True if enough time has passed since last check."""
        return (time.monotonic() - self._last_check_time) >= self.interval

    def should_alert(self, status: HealthStatus) -> bool:
        """True if we should fire an alert (not in cooldown)."""
        if status.healthy:
            self._healthy = True
            self._last_unhealthy_time = 0.0
            return False

        # Don't spam alerts — cooldown after first alert
        now = time.monotonic()
        if self._healthy:
            # Was healthy, now unhealthy — alert immediately
            self._last_unhealthy_time = now
            self._healthy = False
            return True

        # Still unhealthy — check cooldown
        return (now - self._last_unhealthy_time) >= self.cooldown

    def record_check(self):
        """Call after each check."""
        self._last_check_time = time.monotonic()

    async def async_check(self) -> HealthStatus:
        """Wrapper — calls sync check() in an async context."""
        return self.check()
