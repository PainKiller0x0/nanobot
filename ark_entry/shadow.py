"""
Standalone shadow gateway entry point.
Does NOT import the nanobot package — stays lightweight in standby mode.

Usage:
    python ark_entry/shadow.py --port 8081
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

# ── Minimal logging setup (no nanobot imports) ─────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("shadow")


# ── Shadow Gateway (standalone, no nanobot deps) ──────────────────────────────

class ShadowGateway:
    """
    Lightweight shadow gateway: only listens on a socket in standby.
    Activation triggers a subprocess that runs the real gateway.
    """

    def __init__(
        self,
        port: int = 8081,
        pid_file: str | None = None,
    ):
        self._port = port
        self._pid_file = Path(pid_file) if pid_file else Path.home() / ".nanobot" / "gateway_shadow.pid"
        self._activated = False
        self._server: asyncio.Server | None = None

    async def start(self):
        """Start the shadow socket listener."""
        self._server = await asyncio.start_server(
            self._handle_client,
            host="localhost",
            port=self._port,
        )
        addr = self._server.sockets[0].getsockname()
        logger.info(f"Shadow gateway listening on {addr}")

        # Write PID
        self._pid_file.parent.mkdir(parents=True, exist_ok=True)
        self._pid_file.write_text(str(os.getpid()))

        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        addr = writer.get_extra_info("peername")
        try:
            data = await asyncio.wait_for(reader.readline(), timeout=5)
            command = data.decode().strip()

            if command == "ACTIVATE":
                await self._handle_activate(writer)
            elif command.startswith("STATE"):
                await self._handle_state(reader, writer)
            else:
                writer.write(b"UNKNOWN\n")
                await writer.drain()
        except Exception as e:
            logger.error(f"Error handling {addr}: {e}")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_activate(self, writer: asyncio.StreamWriter):
        """Spawn the real gateway as a subprocess."""
        if self._activated:
            writer.write(b"ALREADY_ACTIVATED\n")
            await writer.drain()
            return

        logger.info("ACTIVATE received, checking stable version")
        self._activated = True

        # Fallback: checkout stable ref BEFORE spawning gateway
        stable_ref_path = Path.home() / ".nanobot/ark/stable_ref"
        fallback_marker = Path.home() / ".nanobot/ark/fallback_marker"
        if stable_ref_path.exists():
            ref = stable_ref_path.read_text().strip()
            logger.info(f"Fallback to stable ref: {ref[:8]}")
            # Checkout stable version in nanobot source tree
            checkout_proc = await asyncio.create_subprocess_exec(
                "git", "checkout", ref,
                cwd="/root/nanobot",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await checkout_proc.communicate()
            if checkout_proc.returncode == 0:
                logger.info(f"Checked out stable ref: {ref[:8]}")
                # Write marker so gateway can notify user
                fallback_marker.write_text(ref)
                logger.info(f"Fallback marker written: {fallback_marker}")
            else:
                logger.warning(f"git checkout failed (running current code)")
                fallback_marker.unlink(missing_ok=True)

        # Spawn nanobot gateway as a child process
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "nanobot", "gateway",
            cwd="/root/nanobot",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info(f"Real gateway spawned (pid={proc.pid})")

        writer.write(b"ACTIVATED\n")
        await writer.drain()

    async def _handle_state(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Receive state sync (no-op in lightweight mode)."""
        try:
            json_line = await asyncio.wait_for(reader.readline(), timeout=5)
            logger.debug(f"STATE received: {json_line[:50]}")
            writer.write(b"STATE_OK\n")
            await writer.drain()
        except Exception:
            writer.write(b"STATE_ERROR\n")
            await writer.drain()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(prog="shadow-gateway")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--pid-file", default=None)
    args = parser.parse_args()

    try:
        asyncio.run(ShadowGateway(port=args.port, pid_file=args.pid_file).start())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
