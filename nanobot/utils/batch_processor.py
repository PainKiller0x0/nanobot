"""Batch processor for reducing IO overhead by grouping operations."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class _BatchItem:
    item: Any
    future: asyncio.Future
    processor: Callable[[Any], Awaitable[Any]]


class BatchProcessor:
    """Batch processor - groups items and processes them together to reduce IO overhead.

    Usage:
        processor = BatchProcessor(batch_size=10, timeout=1.0)

        async def process(item):
            return item * 2

        # Add items
        result = await processor.add(my_item, process)
        # Or with context manager
        async with processor.context() as batch:
            batch.add(item, process)

        await processor.close()
    """

    def __init__(self, batch_size: int = 10, timeout: float = 1.0):
        self.batch_size = batch_size
        self.timeout = timeout
        self._queue: deque[_BatchItem] = deque()
        self._timer: asyncio.Task | None = None
        self._closed = False

    async def add(
        self,
        item: Any,
        processor: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """Add an item to the batch and await its result."""
        if self._closed:
            raise RuntimeError("BatchProcessor is closed")

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._queue.append(_BatchItem(item, future, processor))

        # Trigger immediate batch if size reached
        if len(self._queue) >= self.batch_size:
            await self._process_batch()
        elif self._timer is None:
            self._timer = asyncio.create_task(self._timeout_handler())

        return await future

    async def _timeout_handler(self) -> None:
        """Fire after timeout if queue has items."""
        await asyncio.sleep(self.timeout)
        if self._queue and not self._closed:
            await self._process_batch()
        self._timer = None

    async def _process_batch(self) -> None:
        """Process up to batch_size items from the queue."""
        if not self._queue:
            return

        batch: list[_BatchItem] = []
        for _ in range(min(self.batch_size, len(self._queue))):
            batch.append(self._queue.popleft())

        if not batch:
            return

        # Gather all results
        tasks = [item.processor(item.item) for item in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for item, result in zip(batch, results):
            if isinstance(result, BaseException):
                item.future.set_exception(result)
            else:
                item.future.set_result(result)

    async def close(self) -> None:
        """Close the processor, processing remaining items."""
        self._closed = True
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self._queue:
            await self._process_batch()
        self._queue.clear()

    @property
    def pending(self) -> int:
        """Number of items waiting in the queue."""
        return len(self._queue)

    class _Context:
        """Context manager for batch operations."""

        def __init__(self, processor: BatchProcessor):
            self._processor = processor
            self._items: list[tuple[Any, Callable]] = []

        def add(
            self,
            item: Any,
            processor: Callable[[Any], Awaitable[Any]],
        ) -> asyncio.Future:
            """Add an item to the batch."""
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._processor._queue.append(_BatchItem(item, future, processor))
            return future

        async def __aenter__(self) -> "_Context":
            return self

        async def __aexit__(self, *_: Any) -> None:
            if len(self._processor._queue) >= self._processor.batch_size:
                await self._processor._process_batch()
            elif self._processor._timer is None:
                self._processor._timer = asyncio.create_task(
                    self._processor._timeout_handler()
                )

    def context(self) -> _Context:
        """Return a context manager for batch operations."""
        return self._Context(self)
