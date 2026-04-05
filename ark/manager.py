#!/usr/bin/env python3
"""
ARK Manager — nanobot-independent coordinator.

nanobot-independent entry point for `nanobot ark start`.
Does NOT import nanobot package — only stdlib + subprocess.

Responsibilities:
- Read slot config from JSON files
- Spawn main + shadow gateway processes
- Health check loop (process alive + PID file + session mtime)
- Failover to shadow on main crash
- Rebuild main when it recovers
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

NANOBOT_ROOT = Path.home() / ".nanobot"
ARK_DIR = NANOBOT_ROOT / "ark"
SLOT_A_DIR = NANOBOT_ROOT / "slot_a"
SLOT_B_DIR = NANOBOT_ROOT / "slot_b"
ACTIVE_SLOT_FILE = NANOBOT_ROOT / "active_slot"
MAIN_PID_FILE = NANOBOT_ROOT / "gateway_main.pid"
SHADOW_PID_FILE = NANOBOT_ROOT / "gateway_shadow.pid"
SESSION_DIR = Path.home() / ".nanobot" / "workspace" / "sessions"
SESSION_LATEST = SESSION_DIR / "latest.json"
GATEWAY_LOG = Path.home() / ".nanobot" / "workspace" / "lof_monitor" / "nanobot_gateway.log"

logger = logging.getLogger("ark.manager")

HEALTH_CHECK_INTERVAL = 5.0
SESSION_MAX_AGE = 120
HEALTH_CHECK_INTERVAL_FILE = Path.home() / ".nanobot" / "ark" / "health_check_interval.txt"
LOG_DIR = Path.home() / ".nanobot" / "logs"


def _get_check_interval(default: float = HEALTH_CHECK_INTERVAL) -> float:
    try:
        if HEALTH_CHECK_INTERVAL_FILE.exists():
            val = float(HEALTH_CHECK_INTERVAL_FILE.read_text().strip())
            if 1.0 <= val <= 300.0:
                return val
    except Exception:
        pass
    return default


REBUILD_DELAY = 5.0
STARTUP_BUFFER = 60.0


@dataclass
class GatewayProc:
    name: str
    port: int
    process: Optional[subprocess.Popen] = None
    pid: Optional[int] = None
    is_shadow: bool = False

    @property
    def alive(self) -> bool:
        if self.process is None:
            return False
        return self.process.poll() is None


class ArkManager:

    def __init__(self, main_port: int = 8080, shadow_port: int = 8081):
        self.main_port = main_port
        self.shadow_port = shadow_port

        self._main: GatewayProc = GatewayProc(name="main", port=main_port, is_shadow=False)
        self._shadow: GatewayProc = GatewayProc(name="shadow", port=shadow_port, is_shadow=True)
        self._main_start_time: float = 0.0
        self._shadow_activated = False
        self._shutdown = False

        self._active_slot = self._detect_active_slot()
        self._standby_slot = "B" if self._active_slot == "A" else "A"
        logger.info(f"Active slot={self._active_slot}, standby={self._standby_slot}")

    def _detect_active_slot(self) -> str:
        if ACTIVE_SLOT_FILE.exists():
            return ACTIVE_SLOT_FILE.read_text().strip().upper()
        return "A"

    def _gateway_workspace(self, slot: str) -> Optional[Path]:
        slot_dir = SLOT_A_DIR if slot == "A" else SLOT_B_DIR
        ws = slot_dir / "workspace"
        return ws if ws.exists() else None

    def _gateway_config(self, slot: str) -> Optional[Path]:
        slot_dir = SLOT_A_DIR if slot == "A" else SLOT_B_DIR
        cfg = slot_dir / "config.json"
        return cfg if cfg.exists() else None

    def _port_available(self, port: int) -> bool:
        try:
            with socket.create_server(("localhost", port)):
                return True
        except OSError:
            return False

    def _session_fresh(self, max_age: int = SESSION_MAX_AGE) -> bool:
        if not SESSION_LATEST.exists():
            logger.debug("No session file found, skipping session freshness check")
            return True
        age = time.time() - SESSION_LATEST.stat().st_mtime
        if age >= max_age:
            logger.debug(f"Session file too old: {age:.0f}s >= {max_age}s")
        return age < max_age

    def _open_log(self, name: str):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"{name}.log"
        return open(log_path, "a")

    def start(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._run())

    async def _run(self):
        self._spawn_main()
        self._spawn_shadow()
        self._main_start_time = time.monotonic()

        HEALTH_CHECK_INTERVAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not HEALTH_CHECK_INTERVAL_FILE.exists():
            HEALTH_CHECK_INTERVAL_FILE.write_text(str(HEALTH_CHECK_INTERVAL))
            logger.info(f"Created {HEALTH_CHECK_INTERVAL_FILE} with default {HEALTH_CHECK_INTERVAL}s")

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._handle_shutdown)
            except NotImplementedError:
                pass

        while not self._shutdown:
            try:
                await self._health_check()
            except Exception as e:
                logger.error(f"Health check error: {e}")
            await asyncio.sleep(_get_check_interval())

        await self._cleanup()

    def _handle_shutdown(self):
        logger.info("ARK Manager shutting down...")
        self._shutdown = True

    def _spawn_main(self):
        logger.info(f"Spawning main gateway on port {self.main_port}")
        logger.info(f"sys.executable = {sys.executable}")
        ws = self._gateway_workspace(self._active_slot)
        cfg = self._gateway_config(self._active_slot)
        env = os.environ.copy()
        env["ARK_SLOT_WORKSPACE"] = str(ws) if ws else ""

        args = [sys.executable, "-m", "nanobot", "gateway", "--port", str(self.main_port)]

        log_file = self._open_log("gateway_main")
        proc = subprocess.Popen(
            args,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._main.process = proc
        self._main.pid = proc.pid
        self._main._log_file = log_file
        MAIN_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        MAIN_PID_FILE.write_text(str(proc.pid))
        logger.info(f"Main gateway spawned (pid={proc.pid})")

    def _spawn_shadow(self):
        logger.info(f"Spawning shadow gateway on port {self.shadow_port}")

        shadow_script = Path(__file__).parent / "shadow.py"
        args = [sys.executable, str(shadow_script), "--port", str(self.shadow_port)]

        log_file = self._open_log("gateway_shadow")
        proc = subprocess.Popen(
            args,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._shadow.process = proc
        self._shadow.pid = proc.pid
        self._shadow._log_file = log_file
        SHADOW_PID_FILE.write_text(str(proc.pid))
        logger.info(f"Shadow gateway spawned (pid={proc.pid})")

    async def _health_check(self):
        logger.debug(f"Health check running, uptime={time.monotonic() - self._main_start_time:.1f}s")
        if time.monotonic() - self._main_start_time < STARTUP_BUFFER:
            return

        main_ok = self._check_main_alive()
        session_ok = self._session_fresh()

        if main_ok and session_ok:
            return

        if not main_ok:
            logger.warning("Main gateway process not alive")
        else:
            logger.warning("Main gateway session not fresh")

        if self._shadow_activated:
            return

        await self._failover()

    def _check_main_alive(self) -> bool:
        if self._main.process is None:
            return False
        return self._main.process.poll() is None

    async def _failover(self):
        if not self._shadow.alive:
            logger.error("Shadow gateway is dead, cannot failover")
            return

        logger.warning("Failing over to shadow gateway")

        try:
            reader, writer = await asyncio.open_connection("localhost", self.shadow_port)
            writer.write(b"ACTIVATE\n")
            await writer.drain()
            resp = await asyncio.wait_for(reader.readline(), timeout=5)
            writer.close()
            await writer.wait_closed()
            logger.info(f"Shadow responded: {resp.decode().strip()}")
        except Exception as e:
            logger.error(f"Failed to activate shadow: {e}")
            return

        self._shadow_activated = True

        if self._main.alive:
            self._main.process.terminate()
            try:
                self._main.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._main.process.kill()
                self._main.process.wait()

        asyncio.create_task(self._rebuild_main())

    async def _rebuild_main(self):
        await asyncio.sleep(REBUILD_DELAY)
        logger.info("Rebuilding main gateway...")

        if self._main.alive:
            self._main.process.terminate()
            try:
                self._main.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._main.process.kill()

        if hasattr(self._main, '_log_file'):
            try:
                self._main._log_file.close()
            except Exception:
                pass

        self._spawn_main()
        self._main_start_time = time.monotonic()

        for _ in range(12):
            await asyncio.sleep(5)
            if self._check_main_alive() and self._session_fresh():
                logger.info("Main gateway healthy, switchback complete")
                self._shadow_activated = False
                self._main_start_time = time.monotonic()

                try:
                    reader, writer = await asyncio.open_connection("localhost", self.shadow_port)
                    writer.write(b"DEACTIVATE\n")
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

                if self._shadow.alive:
                    self._shadow.process.terminate()
                    try:
                        self._shadow.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._shadow.process.kill()

                if hasattr(self._shadow, '_log_file'):
                    try:
                        self._shadow._log_file.close()
                    except Exception:
                        pass

                self._spawn_shadow()
                return

        logger.warning("Main gateway still unhealthy after 60s, keeping shadow active")

    async def _cleanup(self):
        for proc_obj in (self._main, self._shadow):
            if proc_obj.process and proc_obj.process.poll() is None:
                proc_obj.process.terminate()
                try:
                    proc_obj.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc_obj.process.kill()
            if hasattr(proc_obj, '_log_file'):
                try:
                    proc_obj._log_file.close()
                except Exception:
                    pass

        for pf in (MAIN_PID_FILE, SHADOW_PID_FILE):
            if pf.exists():
                pf.unlink()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="ARK Manager")
    parser.add_argument("--main-port", type=int, default=8080)
    parser.add_argument("--shadow-port", type=int, default=8081)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    if not SLOT_A_DIR.exists() and not SLOT_B_DIR.exists():
        print("Error: No slots found. Run 'nanobot ark init' first.", file=sys.stderr)
        sys.exit(1)

    try:
        with socket.create_server(("localhost", args.main_port)):
            pass
    except OSError:
        print(f"Error: Port {args.main_port} already in use.", file=sys.stderr)
        sys.exit(1)

    try:
        with socket.create_server(("localhost", args.shadow_port)):
            pass
    except OSError:
        print(f"Error: Port {args.shadow_port} already in use.", file=sys.stderr)
        sys.exit(1)

    print("ARK pre-flight checks passed, starting...")
    ArkManager(main_port=args.main_port, shadow_port=args.shadow_port).start()


if __name__ == "__main__":
    main()
