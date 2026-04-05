"""
Entry point for running nanobot ark as a module.

Usage:
    python -m ark                          # ark CLI
    python -m ark --shadow-mode --port 8081  # shadow gateway (bypasses nanobot gateway import chain)
"""
import argparse
import asyncio
import sys


def _parse_args():
    parser = argparse.ArgumentParser(prog="nanobot ark")
    parser.add_argument("--shadow-mode", action="store_true", help="Run shadow gateway in standby mode")
    parser.add_argument("--port", type=int, default=8081, help="Shadow gateway port")
    parser.add_argument("--pid-file", default=None, help="Write PID to this file")
    parser.add_argument("--standby-config", default=None, help="Path to standby config.json")
    parser.add_argument("--standby-workspace", default=None, help="Path to standby workspace")
    return parser.parse_known_args()


def main():
    args, _ = _parse_args()

    if args.shadow_mode:
        # Minimal entry: only import what shadow gateway needs
        import logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")

        from ark.shadow_gateway import ShadowGateway

        asyncio.run(
            ShadowGateway(
                port=args.port,
                pid_file=args.pid_file,
                standby_config=args.standby_config,
                standby_workspace=args.standby_workspace,
            ).start()
        )
    else:
        # Default: run ark CLI
        from ark.cli import app
        app()


if __name__ == "__main__":
    main()
