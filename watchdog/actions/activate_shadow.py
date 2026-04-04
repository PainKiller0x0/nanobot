"""Activate shadow action — tells shadow gateway to take over."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .base import Action

logger = logging.getLogger("watchdog.actions")


class ActivateShadowAction(Action):
    """Send ACTIVATE command to shadow gateway via TCP."""

    name = "ark.activate_shadow"
    lockout = 60.0  # don't retry within 60 seconds

    def __init__(self, shadow_host: str = "localhost", shadow_port: int = 8081):
        super().__init__()
        self.shadow_host = shadow_host
        self.shadow_port = shadow_port

    def execute(self, status) -> bool:
        """Send ACTIVATE to shadow gateway socket."""
        import socket

        try:
            reader, writer = asyncio.run(self._send_activate())
            return True
        except Exception as e:
            logger.error(f"ActivateShadowAction failed: {e}")
            return False

    async def _send_activate(self):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.shadow_host, self.shadow_port),
            timeout=5.0,
        )
        writer.write(b"ACTIVATE\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.readline(), timeout=5.0)
        logger.info(f"Shadow response: {response.strip()}")
        writer.close()
        await writer.wait_closed()
        return reader, writer
