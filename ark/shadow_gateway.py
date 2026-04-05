"""
L1: ShadowGateway — 影子网关

nanobot gateway --shadow-mode 的实现

工作流程:
1. 监听 localhost:shadow_port
2. 接收命令:
   - ACTIVATE\n: 激活，接管服务
   - STATE\n{json}\n: 更新内存状态
3. 平时只维护 socket 连接，不跑 agent loop
4. 激活后: 读取 session → 启动 agent loop → 处理请求
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ShadowGateway:
    """
    Shadow Gateway - 待机模式
    不跑 agent loop，只监听 socket 命令
    激活后才启动完整 gateway
    """

    def __init__(self, port: int = 8081):
        self._port = port
        self._activated = False
        self._state: dict = {
            "session_key": None,
            "memory": {},
            "context": []
        }
        self._server: Optional[asyncio.Server] = None
        self._agent_task: Optional[asyncio.Task] = None

    async def start(self):
        """启动 shadow gateway（待机模式）"""
        self._server = await asyncio.start_server(
            self._handle_client,
            host="localhost",
            port=self._port
        )

        addr = self._server.sockets[0].getsockname()
        logger.info(f"Shadow gateway listening on {addr}")

        # 保持运行
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter
    ):
        """处理 ShadowEngine 发来的命令"""
        addr = writer.get_extra_info("peername")
        logger.debug(f"Shadow gateway connection from {addr}")

        try:
            data = await reader.readline()
            command = data.decode().strip()

            if command == "ACTIVATE":
                await self._handle_activate(writer)
            elif command.startswith("STATE"):
                # STATE\n{json}\n
                json_data = await reader.readline()
                await self._handle_state(json_data.decode(), writer)
            else:
                writer.write(b"UNKNOWN\n")
                await writer.drain()

        except Exception as e:
            logger.error(f"Error handling client {addr}: {e}")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_activate(self, writer: asyncio.StreamWriter):
        """处理激活命令"""
        if self._activated:
            writer.write(b"ALREADY_ACTIVE\n")
            await writer.drain()
            return

        logger.info("Shadow gateway activating...")

        # 启动 agent loop（在后台任务中）
        self._agent_task = asyncio.create_task(self._start_agent_loop())
        self._activated = True

        writer.write(b"ACTIVATED\n")
        await writer.drain()

    async def _handle_state(self, json_data: str, writer: asyncio.StreamWriter):
        """处理状态更新"""
        try:
            state = json.loads(json_data)
            self._state.update(state)
            writer.write(b"STATE_OK\n")
        except json.JSONDecodeError:
            writer.write(b"STATE_ERROR\n")

        await writer.drain()

    async def _start_agent_loop(self):
        """
        启动完整 agent loop
        激活后调用，模拟正常运行 gateway
        """
        logger.info("Shadow gateway: starting agent loop...")

        try:
            await self._run_gateway()

        except Exception as e:
            logger.error(f"Shadow gateway agent loop failed: {e}")
            import traceback
            traceback.print_exc()

    async def _run_gateway(self):
        """
        影子网关激活后运行的完整 gateway
        等价于 gateway() 命令去掉 Typer 包装
        """
        import asyncio as _asyncio
        from pathlib import Path as _Path

        from nanobot import __logo__, __version__
        from rich.console import Console

        from .gateway_shared import build_gateway, _load_runtime_config

        console = Console()
        loop = _asyncio.get_running_loop()
        shutdown_event = _asyncio.Event()

        def _sigterm_handler():
            console.print("[yellow]Shadow gateway: SIGTERM received[/yellow]")
            _asyncio.create_task(_gs())

        try:
            loop.add_signal_handler(signal.SIGTERM, _sigterm_handler)
        except NotImplementedError:
            pass  # Windows

        components = None  # Will be set if build_gateway succeeds
        try:
            # 加载配置
            config = _load_runtime_config(None, None)
            console.print(
                f"[dim]{__logo__}[/dim] "
                f"[green]Shadow gateway activated[/green] "
                f"v{__version__}"
            )

            # 构建所有组件（共用 gateway_shared）
            components = await build_gateway(
                config=config,
                console=console,
                shutdown_event=shutdown_event,
            )

            # 写 PID 文件（激活时写，ShadowEngine 可以检查）
            pid_file = _Path.home() / ".nanobot" / "gateway.pid"
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(os.getpid()))

            await components["cron"].start()
            await components["heartbeat"].start()

            await _asyncio.gather(
                components["agent"].run(),
                components["channels"].start_all(),
            )

        except KeyboardInterrupt:
            console.print("\nShadow gateway interrupted...")
            if components is not None:
                await components["graceful_shutdown"]()
        except Exception:
            import traceback
            console.print("\n[red]Shadow gateway crashed[/red]")
            console.print(traceback.format_exc())
            if components is not None:
                await components["graceful_shutdown"]()

        await shutdown_event.wait()


# ── CLI 入口 ──────────────────────────────────────────────────────────

def main():
    """shadow gateway 入口: nanobot gateway --shadow-mode"""
    import typer

    app = typer.Typer()

    @app.command()
    def serve(
        shadow_port: int = typer.Option(8081, "--shadow-port", "-p", help="Shadow gateway port"),
        verbose: bool = typer.Option(False, "--verbose", "-v", help="Show debug logs"),
    ):
        if verbose:
            import logging
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s | %(name)s | %(levelname)s | %(message)s"
            )
        else:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s | %(levelname)s | %(message)s"
            )

        logger.info(f"Starting shadow gateway on port {shadow_port}")
        asyncio.run(ShadowGateway(port=shadow_port).start())

    app()


if __name__ == "__main__":
    main()
