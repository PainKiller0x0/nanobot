"""Notify action — logs the alert. Extend later to send QQ/email/webhook."""

from __future__ import annotations

import logging

from .base import Action

logger = logging.getLogger("watchdog.actions")


class NotifyAction(Action):
    """Log an alert for human attention.

    Currently just logs. Extend to send QQ message / webhook / email.
    """

    name = "ark.notify"
    lockout = 30.0

    def execute(self, status) -> bool:
        logger.warning(
            f"[WATCHDOG ALERT] {status.name}: {status.reason} | "
            f"details={status.details}"
        )
        return True
