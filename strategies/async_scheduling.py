from __future__ import annotations

import asyncio
from concurrent.futures import Future as ConcurrentFuture
from typing import Any
from typing import Awaitable


async def _await(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


class AsyncAwaitableScheduler:
    """Schedules awaitables on the node event loop from any callback thread."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def capture_running_loop(self) -> None:
        if self._loop is not None:
            return
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def schedule(
        self,
        awaitable: Awaitable[Any],
    ) -> asyncio.Future[Any] | ConcurrentFuture[Any]:
        """
        Schedule an awaitable and return a future with a uniform callback API.

        LiveClock callbacks can run on the Rust timer thread, while their I/O is
        bound to the node loop. Backtests and synchronous tests have no node loop,
        so they resolve the awaitable immediately into a concurrent future.
        """
        loop = self._loop
        if loop is not None and loop.is_running():
            return asyncio.run_coroutine_threadsafe(_await(awaitable), loop)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            return asyncio.ensure_future(awaitable, loop=running)
        resolved: ConcurrentFuture[Any] = ConcurrentFuture()
        try:
            resolved.set_result(asyncio.run(_await(awaitable)))
        except Exception as exc:  # noqa: BLE001 - surfaced via the future
            resolved.set_exception(exc)
        return resolved
