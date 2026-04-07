"""Standalone shadow gateway (REPAIRED v2).
- Fixed BUG-01: Support multiple activations (DEACTIVATE command).
- Fixed BUG-03: Proper subprocess management & PID tracking.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("shadow")

class ShadowGateway:
    def __init__(self, port: int = 8081, pid_file: str | None = None):
        self._port = port
        self._pid_file = Path(pid_file) if pid_file else Path.home() / ".nanobot" / "gateway_shadow.pid"
        self._activated = False
        self._gateway_proc: asyncio.subprocess.Process | None = None
        self._server: asyncio.Server | None = None

    async def start(self):
        self._server = await asyncio.start_server(self._handle_client, host="127.0.0.1", port=self._port)
        addr = self._server.sockets[0].getsockname()
        logger.info(f"Shadow gateway listening on {addr}")
        self._pid_file.write_text(str(os.getpid()))
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await asyncio.wait_for(reader.readline(), timeout=5)
            command = data.decode().strip()
            
            if command == "ACTIVATE":
                await self._handle_activate(writer)
            elif command == "DEACTIVATE":
                await self._handle_deactivate(writer)
            elif command.startswith("STATE"):
                writer.write(b"STATE_OK\n")
            else:
                writer.write(b"UNKNOWN\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_activate(self, writer: asyncio.StreamWriter):
        if self._activated:
            writer.write(b"ALREADY_ACTIVATED\n")
            return

        logger.info("ACTIVATE received, spawning real gateway...")
        # BUG-03 FIX: Cleanup old child if any
        if self._gateway_proc:
            try: self._gateway_proc.terminate()
            except: pass

        # Spawn gateway
        self._gateway_proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "nanobot", "gateway",
            cwd="/root/nanobot",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._activated = True
        logger.info(f"Real gateway spawned (pid={self._gateway_proc.pid})")
        writer.write(b"ACTIVATED\n")

    async def _handle_deactivate(self, writer: asyncio.StreamWriter):
        """BUG-01 FIX: Reset state for next failover."""
        logger.info("DEACTIVATE received, stopping real gateway...")
        if self._gateway_proc:
            try:
                self._gateway_proc.terminate()
                await asyncio.wait_for(self._gateway_proc.wait(), timeout=5)
            except:
                if self._gateway_proc: self._gateway_proc.kill()
            self._gateway_proc = None
        
        self._activated = False
        logger.info("Shadow gateway reset to STANDBY mode.")
        writer.write(b"DEACTIVATED\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--pid-file", type=str, default=None)
    args = parser.parse_args()

    gw = ShadowGateway(port=args.port, pid_file=args.pid_file)
    try:
        asyncio.run(gw.start())
    except KeyboardInterrupt:
        pass
