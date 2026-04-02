"""Hot reload configuration without restart."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable, Any


class HotReloadConfig:
    """Hot reload configuration - watch for file changes and reload without restart.

    Usage:
        async def on_reload(new_config):
            print("Config reloaded!")

        watcher = HotReloadConfig(Path("config.yaml"), on_reload)
        await watcher.watch()  # Blocks, watches forever
        # Or: asyncio.create_task(watcher.watch()) for background
    """

    def __init__(
        self,
        config_path: Path | str,
        loader: Callable[[], dict[str, Any]],
        on_reload: Callable[[dict[str, Any]], None] | None = None,
        poll_interval: float = 1.0,
    ):
        self.config_path = Path(config_path)
        self._loader = loader
        self._on_reload = on_reload
        self.poll_interval = poll_interval
        self._last_mtime: float = 0
        self._running = False
        self._stop_event = asyncio.Event()
        self._timer: asyncio.Task | None = None

        if self.config_path.exists():
            self._last_mtime = self.config_path.stat().st_mtime

    async def watch(self, *, stop_event: asyncio.Event | None = None) -> None:
        """Watch for file changes. Blocks until stop_event is set."""
        self._running = True
        stop = stop_event or self._stop_event

        while self._running and not stop.is_set():
            await asyncio.sleep(self.poll_interval)
            if not self._running or stop.is_set():
                break
            await self._check_and_reload()

    async def _check_and_reload(self) -> None:
        """Check if file changed, reload if so."""
        if not self.config_path.exists():
            return

        try:
            current_mtime = self.config_path.stat().st_mtime
        except OSError:
            return

        if current_mtime > self._last_mtime:
            self._last_mtime = current_mtime
            try:
                new_config = self._loader()
                if self._on_reload:
                    self._on_reload(new_config)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(
                    "Hot reload failed: {} — keeping previous config".format(e)
                )

    def stop(self) -> None:
        """Stop watching."""
        self._running = False
        self._stop_event.set()
        if self._timer and not self._timer.done():
            self._timer.cancel()

    async def reload_now(self) -> dict[str, Any] | None:
        """Force an immediate reload."""
        if not self.config_path.exists():
            return None
        try:
            self._last_mtime = self.config_path.stat().st_mtime
            new_config = self._loader()
            if self._on_reload:
                self._on_reload(new_config)
            return new_config
        except Exception:
            return None

    @property
    def is_running(self) -> bool:
        return self._running
