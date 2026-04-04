"""Watchdog CLI entry point — run as: python -m watchdog"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from watchdog.config.settings import load_config
from watchdog.daemon import WatchdogDaemon


def _setup_logging(level: str = "INFO"):
    """Configure loguru-style logging to stderr."""
    import loguru

    loguru.logger.remove()
    loguru.logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level=level,
    )


def _write_pid(pid_file: Path):
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))


def main():
    import argparse

    parser = argparse.ArgumentParser(prog="watchdog", description="NanoBot Watchdog — unified health monitor")
    parser.add_argument("--config", "-c", type=str, help="Path to watchdog.yaml")
    parser.add_argument("--log-level", "-l", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--pid-file", "-p", type=str, default="~/.nanobot/watchdog.pid")
    parser.add_argument("--foreground", "-f", action="store_true", help="Run in foreground (don't daemonize)")
    args = parser.parse_args()

    # Setup logging first
    _setup_logging(args.log_level)
    logger = logging.getLogger("watchdog")

    # Load config
    config = load_config(Path(args.config) if args.config else None)
    logger.info(f"Watchdog config loaded — workspace: {config.get('workspace')}")

    # PID file
    pid_file = Path(os.path.expanduser(args.pid_file))
    _write_pid(pid_file)
    logger.info(f"PID file: {pid_file}")

    # Setup signal handlers
    daemon = WatchdogDaemon(config)

    def _sigterm_handler(signum, frame):
        logger.info(f"Received SIGTERM")
        daemon.stop()

    def _sigint_handler(signum, frame):
        logger.info(f"Received SIGINT")
        daemon.stop()

    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigint_handler)

    # Run
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
    finally:
        pid_file.unlink(missing_ok=True)
        logger.info("Watchdog exited")


if __name__ == "__main__":
    main()
