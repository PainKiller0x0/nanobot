"""
ShadowEngine - Dual Engine Manager (REPAIRED & OPTIMIZED).
- L1 Defense: Maintains a lightweight shadow listener.
- Failover: Automatically activates shadow on main process death.
"""

from __future__ import annotations
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
NANOBOT_ROOT = Path.home() / '.nanobot'
PENDING_SWITCH_FILE = NANOBOT_ROOT / 'pending_switch'

@dataclass
class GatewayProcess:
    name: str
    port: int
    process: Optional[asyncio.subprocess.Process] = None

class ShadowEngine:
    def __init__(self, main_port: int = 8080, shadow_port: int = 8081):
        self._main_port = main_port
        self._shadow_port = shadow_port
        self._main_gateway: Optional[GatewayProcess] = None
        self._shadow_gateway: Optional[GatewayProcess] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._is_active = False

    async def start(self):
        """Start both the main gateway and the shadow listener."""
        self._is_active = True
        logger.info(f'Starting ShadowEngine (Main:{self._main_port}, Shadow:{self._shadow_port})')

        # 1. Start Main Gateway
        main_proc = await self._spawn_gateway(self._main_port, is_shadow=False)
        self._main_gateway = GatewayProcess('main', self._main_port, main_proc)
        logger.info(f'Main gateway started (pid={main_proc.pid})')

        # 2. Start Shadow Listener (Lightweight)
        shadow_proc = await self._spawn_gateway(self._shadow_port, is_shadow=True)
        self._shadow_gateway = GatewayProcess('shadow', self._shadow_port, shadow_proc)
        logger.info(f'Shadow listener started (pid={shadow_proc.pid})')

        # 3. Start Health Monitor
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        """Stop all processes and monitoring."""
        self._is_active = False
        if self._monitor_task:
            self._monitor_task.cancel()
        
        for gw in [self._main_gateway, self._shadow_gateway]:
            if gw and gw.process:
                try:
                    gw.process.terminate()
                    await asyncio.wait_for(gw.process.wait(), timeout=5)
                except:
                    if gw.process: gw.process.kill()
        
        logger.info('ShadowEngine stopped.')

    async def _spawn_gateway(self, port: int, is_shadow: bool) -> asyncio.subprocess.Process:
        """Spawn a process based on role."""
        cwd = '/root/nanobot'
        if is_shadow:
            # Runs the lightweight activator
            args = [sys.executable, 'ark/shadow.py', '--port', str(port)]
        else:
            # Runs the full nanobot gateway
            args = [sys.executable, '-m', 'nanobot', 'gateway', '--port', str(port)]
        
        return await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=None,
            stderr=None
        )

    async def _monitor_loop(self):
        """Watch the main process and trigger failover if it dies."""
        while self._is_active:
            await asyncio.sleep(5)
            
            if self._main_gateway and self._main_gateway.process:
                # Check if process is still running
                if self._main_gateway.process.returncode is not None:
                    exit_code = self._main_gateway.process.returncode
                    logger.error(f'Main gateway DIED with exit code {exit_code}! Starting failover...')
                    
                    success = await self._failover_to_shadow()
                    if success:
                        logger.info('ARK Failover: Successfully activated shadow gateway.')
                        # We can stop monitoring or try to restart main in background
                        break 
                    else:
                        logger.error('ARK Failover: Failed to activate shadow gateway.')

    async def _failover_to_shadow(self) -> bool:
        """Send activation command to the shadow listener."""
        PENDING_SWITCH_FILE.write_text(json.dumps({
            'event': 'failover',
            'at': datetime.now().isoformat(),
            'reason': 'main_process_crash'
        }))
        
        # Try to connect to shadow listener and send ACTIVATE
        for attempt in range(3):
            try:
                reader, writer = await asyncio.open_connection('127.0.0.1', self._shadow_port)
                writer.write(b'ACTIVATE\n')
                await writer.drain()
                
                resp = await asyncio.wait_for(reader.readline(), timeout=5)
                writer.close()
                await writer.wait_closed()
                
                if b'ACTIVATED' in resp or b'ALREADY' in resp:
                    return True
            except Exception as e:
                logger.warning(f'Failover attempt {attempt+1} failed: {e}')
                await asyncio.sleep(1)
        
        return False
