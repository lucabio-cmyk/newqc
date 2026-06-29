"""Small async queue wrapper decoupling ingest from evaluation/upload.

HL7 ingestion, local evaluation and cloud upload run at different rates. A
bounded :class:`asyncio.Queue` between them provides backpressure (so a burst of
analyzer messages cannot exhaust memory) and clean shutdown semantics.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from loguru import logger


class QueueManager:
    """Bounded async work queue with a simple consumer loop."""

    def __init__(self, maxsize: int = 1000) -> None:
        """Create the queue.

        Args:
            maxsize: maximum number of buffered items before producers block.
        """
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._stop = asyncio.Event()

    async def put(self, item: Any) -> None:
        """Enqueue an item (awaits if the queue is full - backpressure)."""
        await self._queue.put(item)

    def put_nowait(self, item: Any) -> bool:
        """Enqueue without blocking; returns False if the queue is full."""
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            logger.warning("QueueManager: queue full, dropping item")
            return False

    def qsize(self) -> int:
        """Current number of buffered items."""
        return self._queue.qsize()

    async def consume(self, handler: Callable[[Any], Awaitable[None]]) -> None:
        """Consume items until :meth:`stop` is called, dispatching to ``handler``.

        Handler exceptions are logged and swallowed so one bad item does not kill
        the consumer.
        """
        logger.info("QueueManager consumer started")
        while not self._stop.is_set():
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await handler(item)
            except Exception as exc:  # noqa: BLE001 - isolate handler failures
                logger.exception("QueueManager handler error: {}", exc)
            finally:
                self._queue.task_done()
        logger.info("QueueManager consumer stopped")

    def stop(self) -> None:
        """Signal the consumer loop to exit after the current item."""
        self._stop.set()
