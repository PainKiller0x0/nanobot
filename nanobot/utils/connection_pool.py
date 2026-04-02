"""Connection pool for reusing connections and reducing overhead."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Callable, TypeVar

T = TypeVar("T")


class ConnectionPool:
    """Connection pool - reuses connections to reduce overhead.

    Usage:
        pool = ConnectionPool(max_size=10, factory=my_connection_factory)
        conn = await pool.acquire()
        try:
            await conn.do_something()
        finally:
            await pool.release(conn)
    """

    def __init__(
        self,
        max_size: int = 10,
        factory: Callable[[], T] | Callable[[], asyncio.coroutine[T]] | None = None,
    ):
        self.max_size = max_size
        self._factory = factory
        self._pool: deque[T] = deque()
        self._in_use: set[int] = set()
        self._lock = asyncio.Lock()  # per-instance lock

    async def acquire(self, timeout: float = 10.0) -> T:
        """Acquire a connection from the pool.

        Reuses an idle connection if available, creates a new one
        if under max_size, otherwise waits for one to become available.
        Raises TimeoutError if no connection available within timeout.
        """
        import time
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            async with self._lock:
                if self._pool:
                    conn = self._pool.popleft()
                    self._in_use.add(id(conn))
                    return conn
                if len(self._in_use) < self.max_size and self._factory:
                    factory_result = self._factory()
                    is_coro = asyncio.iscoroutine(factory_result)
                    if is_coro:
                        conn = await factory_result
                    else:
                        conn = factory_result
                    self._in_use.add(id(conn))
                    return conn

            await asyncio.sleep(0.05)

        raise TimeoutError(f"ConnectionPool.acquire timed out after {timeout}s")

    async def release(self, conn: T) -> None:
        """Release a connection back to the pool."""
        key = id(conn)
        async with self._lock:
            self._in_use.discard(key)
            self._pool.append(conn)

    async def close(self) -> None:
        """Close all pooled connections."""
        async with self._lock:
            self._pool.clear()
            self._in_use.clear()

    @property
    def idle_count(self) -> int:
        """Number of idle connections in the pool."""
        return len(self._pool)

    @property
    def in_use_count(self) -> int:
        """Number of connections currently in use."""
        return len(self._in_use)
