"""
L1: ShadowEngine — 影子引擎

职责:
1. 启动/管理 main + shadow 两个 gateway 子进程
2. 健康检查 (进程存活 + session 新鲜度)
3. 故障时激活 shadow gateway
4. 重建 main gateway 并在健康后切回

设计原则（对标 Android A/B）:
- Main = Slot A（当前运行的版本）
- Shadow = Slot B（上一个确认安全的版本，inactive slot）
- ShadowEngine 始终监控 Main，Main 健康时才有效
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

NANOBOT_ROOT = Path.home() / ".nanobot"
PENDING_SWITCH_FILE = NANOBOT_ROOT / "pending_switch"



logger = logging.getLogger(__name__)

# 路径常量
NANOBOT_ROOT = Path.home() / ".nanobot"
SLOT_A = NANOBOT_ROOT / "slot_a"
SLOT_B = NANOBOT_ROOT / "slot_b"
ACTIVE_SLOT_FILE = NANOBOT_ROOT / "active_slot"

# 自检参数
CHECK_INTERVAL = 5.0  # 健康检查间隔（秒）
SESSION_MAX_AGE = 120  # session 文件最大年龄（秒）
REBUILD_DELAY = 10  # 重建 main 前等待（秒）


@dataclass
class GatewayProcess:
    """单个 gateway 进程"""
    name: str
    port: int
    process: Optional[asyncio.subprocess.Process] = None
    is_shadow: bool = False


class ShadowEngine:
    """
    L1: 影子引擎

    管理两个 gateway 子进程:
    - Main Gateway (Slot A): 正常运行时，接收所有改动
    - Shadow Gateway (Slot B): 平时待机，收到 ACTIVATE 后接管
    """

    def __init__(
        self,
        main_port: int = 8080,
        shadow_port: int = 8081,
        check_interval: float = CHECK_INTERVAL
    ):
        self._main_port = main_port
        self._shadow_port = shadow_port
        self._check_interval = check_interval

        self._main_gateway: Optional[GatewayProcess] = None
        self._shadow_gateway: Optional[GatewayProcess] = None

        # False = shadow 在服务，True = main 在服务
        self._main_is_active = True
        self._shadow_activated = False

        self._health_check_task: Optional[asyncio.Task] = None
        self._shutdown = False

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def start(self):
        """启动双引擎"""
        logger.info(f"ShadowEngine starting (main={self._main_port}, shadow={self._shadow_port})")

        # 启动 main gateway
        main_proc = await self._spawn_gateway(self._main_port, is_shadow=False)
        self._main_gateway = GatewayProcess(
            name="main",
            port=self._main_port,
            process=main_proc,
            is_shadow=False
        )

        # 启动 shadow gateway (待机)
        shadow_proc = await self._spawn_gateway(self._shadow_port, is_shadow=True)
        self._shadow_gateway = GatewayProcess(
            name="shadow",
            port=self._shadow_port,
            process=shadow_proc,
            is_shadow=True
        )

        self._main_is_active = True

        # 启动健康检查循环
        self._health_check_task = asyncio.create_task(self._health_check_loop())

        logger.info("ShadowEngine started")

    async def stop(self):
        """停止所有 gateway 子进程"""
        self._shutdown = True

        if self._health_check_task:
            self._health_check_task.cancel()

        for gw in (self._main_gateway, self._shadow_gateway):
            if gw and gw.process:
                try:
                    gw.process.terminate()
                    await asyncio.wait_for(gw.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    gw.process.kill()
                    await gw.process.wait()
                except Exception as e:
                    logger.warning(f"Error stopping {gw.name}: {e}")

        logger.info("ShadowEngine stopped")

    # ── 子进程管理 ──────────────────────────────────────────────────────

    async def _spawn_gateway(
        self,
        port: int,
        is_shadow: bool
    ) -> asyncio.subprocess.Process:
        """启动 gateway 子进程，等待端口就绪"""
        args = [sys.executable, "-m", "nanobot", "gateway"]

        if is_shadow:
            args.extend(["--shadow-mode", "--shadow-port", str(port)])
        else:
            args.extend(["--port", str(port)])

        env = {**__import__("os").environ}

        proc = await asyncio.create_subprocess_exec(
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        logger.info(
            f"Spawned {'shadow' if is_shadow else 'main'} gateway "
            f"(pid={proc.pid}, port={port})"
        )

        # 等待端口就绪，最多 30 秒，超时则杀死进程并抛异常
        try:
            for attempt in range(30):
                await asyncio.sleep(1)
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection("localhost", port),
                        timeout=2,
                    )
                    writer.close()
                    await writer.wait_closed()
                    logger.info(f"Gateway on port {port} is ready")
                    return proc
                except (ConnectionRefusedError, OSError):
                    pass  # 还没起来，继续等

            # 30 秒还没就绪，进程肯定有问题
            proc.kill()
            raise RuntimeError(
                f"Gateway on port {port} failed to start within 30s"
            )
        except asyncio.CancelledError:
            proc.kill()
            raise

    # ── 健康检查 ────────────────────────────────────────────────────────

    async def _health_check_loop(self):
        """健康检查循环"""
        while not self._shutdown:
            try:
                if self._main_is_active:
                    await self._check_main_health()
            except Exception as e:
                logger.error(f"Health check error: {e}")

            await asyncio.sleep(self._check_interval)

    async def _check_main_health(self):
        """检查 main gateway 健康状态（进程 + TCP 连接）"""
        if not self._main_gateway or not self._main_gateway.process:
            return

        proc = self._main_gateway.process

        # 检查进程是否存活
        try:
            proc.wait(timeout=0.01)  # 非阻塞
            logger.warning(f"Main gateway exited with code {proc.returncode}")
            await self._failover_to_shadow()
            return
        except asyncio.TimeoutError:
            pass  # 进程还在运行

        # 检查 main gateway 是否真正响应（TCP 连接检查）
        # 进程可能卡死或 segfault 但进程 PID 还在
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("localhost", self._main_port),
                timeout=3,
            )
            writer.close()
            await writer.wait_closed()
        except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
            logger.warning(f"Main gateway not reachable on port {self._main_port}")
            await self._failover_to_shadow()
            return

        # 检查 session 文件新鲜度
        if not _is_session_fresh(SESSION_MAX_AGE):
            logger.warning("Main gateway session not fresh")
            await self._failover_to_shadow()

    # ── 故障切换 ────────────────────────────────────────────────────────

    async def _failover_to_shadow(self):
        """
        故障切换到 shadow gateway
        1. 写 pending_switch 文件，通知 watchdog 进入 ARK 模式（不重启 gateway）
        2. 发送 ACTIVATE 到 shadow
        3. shadow 读取 session 接管
        4. 后台重建 main
        """
        if self._shadow_activated:
            logger.debug("Shadow already activated, skipping failover")
            return

        logger.warning("Failing over to shadow gateway")

        # 通知 watchdog 进入 ARK 模式，不要重启 gateway
        PENDING_SWITCH_FILE.write_text(json.dumps({
            "event": "failover",
            "from": "main",
            "to": "shadow",
            "at": datetime.now().isoformat(),
        }))
        logger.info("Wrote pending_switch (ARK mode)")

        success = await self._send_activate()
        if success:
            self._main_is_active = False
            self._shadow_activated = True
            logger.info("Shadow gateway activated")

            # 后台重建 main gateway
            asyncio.create_task(self._rebuild_main())
        else:
            logger.error("Failed to activate shadow, shadow gateway may be down")

    async def _send_activate(self) -> bool:
        """发送 ACTIVATE 信号到 shadow gateway"""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("localhost", self._shadow_port),
                timeout=5
            )

            writer.write(b"ACTIVATE\n")
            await writer.drain()

            resp = await asyncio.wait_for(reader.readline(), timeout=5)
            logger.info(f"Shadow responded: {resp.decode().strip()}")

            writer.close()
            await writer.wait_closed()

            return True
        except asyncio.TimeoutError:
            logger.error("Shadow gateway activation timed out")
            return False
        except Exception as e:
            logger.error(f"Failed to activate shadow: {e}")
            return False

    async def _send_state(self, state: dict) -> bool:
        """发送状态更新到 shadow gateway"""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("localhost", self._shadow_port),
                timeout=5
            )

            import json
            writer.write(f"STATE\n{json.dumps(state)}\n".encode())
            await writer.drain()

            resp = await asyncio.wait_for(reader.readline(), timeout=5)
            ok = resp.decode().strip() == "STATE_OK"

            writer.close()
            await writer.wait_closed()

            return ok
        except Exception as e:
            logger.warning(f"State sync to shadow failed: {e}")
            return False

    # ── 重建 Main ───────────────────────────────────────────────────────

    async def _rebuild_main(self):
        """重建 main gateway"""
        await asyncio.sleep(REBUILD_DELAY)

        # 终止旧 main
        if self._main_gateway and self._main_gateway.process:
            try:
                self._main_gateway.process.terminate()
                await asyncio.wait_for(
                    self._main_gateway.process.wait(),
                    timeout=5
                )
            except asyncio.TimeoutError:
                self._main_gateway.process.kill()
                await self._main_gateway.process.wait()
            except Exception:
                pass

        # 启动新 main
        main_proc = await self._spawn_gateway(self._main_port, is_shadow=False)
        self._main_gateway = GatewayProcess(
            name="main",
            port=self._main_port,
            process=main_proc,
            is_shadow=False
        )

        # 等待新 main 就绪
        await asyncio.sleep(5)

        # 检查新 main 健康状态
        if await self._check_main_health_poll():
            await self._switch_back_to_main()

    async def _check_main_health_poll(self) -> bool:
        """轮询检查 main 健康（用于重建后验证）"""
        for _ in range(3):
            await asyncio.sleep(2)
            if not self._main_gateway or not self._main_gateway.process:
                continue
            try:
                self._main_gateway.process.wait(timeout=0.01)
            except asyncio.TimeoutError:
                if _is_session_fresh(SESSION_MAX_AGE):
                    return True
        return False

    async def _switch_back_to_main(self):
        """切回 main gateway"""
        if not self._shadow_activated:
            return

        logger.info("Main gateway healthy, switching back")

        # 通知 shadow 停机（简单做法：杀 shadow 进程）
        if self._shadow_gateway and self._shadow_gateway.process:
            try:
                self._shadow_gateway.process.terminate()
                await asyncio.wait_for(
                    self._shadow_gateway.process.wait(),
                    timeout=5
                )
            except asyncio.TimeoutError:
                self._shadow_gateway.process.kill()
            except Exception:
                pass

        self._shadow_activated = False
        self._main_is_active = True

        # 重新启动 shadow 作为备用
        shadow_proc = await self._spawn_gateway(self._shadow_port, is_shadow=True)
        self._shadow_gateway = GatewayProcess(
            name="shadow",
            port=self._shadow_port,
            process=shadow_proc,
            is_shadow=True
        )

        logger.info("Switched back to main gateway, shadow restarted as standby")


# ── 工具函数 ──────────────────────────────────────────────────────────

def _is_session_fresh(max_age: int = SESSION_MAX_AGE) -> bool:
    """检查 session 文件新鲜度（跨进程，不需要 async）"""
    try:
        if not ACTIVE_SLOT_FILE.exists():
            return False

        slot_name = ACTIVE_SLOT_FILE.read_text().strip().lower()
        slot_path = SLOT_A if slot_name == "a" else SLOT_B

        latest = slot_path / "sessions" / "latest.json"
        if not latest.exists():
            return False

        age = time.time() - latest.stat().st_mtime
        return age < max_age
    except Exception:
        return False
