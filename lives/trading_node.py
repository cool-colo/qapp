"""Manage startup and shutdown of the live Nautilus node."""
from __future__ import annotations

import asyncio

from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode


class LiveTradingNode(TradingNode):
    """Cancel startup waits before stopping, and bound startup as a whole.

    Nautilus can keep its startup coroutine waiting for connections after the
    engines have stopped. Keeping that coroutine in a separate task lets SIGINT
    stop it without interrupting the asynchronous disconnect/cleanup work.
    """

    def __init__(
        self,
        config: TradingNodeConfig,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        super().__init__(config=config, loop=loop)
        self._runtime_task: asyncio.Task | None = None
        self._shutdown_task: asyncio.Task | None = None
        self._startup_timeout = (
            config.timeout_connection + config.timeout_reconciliation + config.timeout_portfolio
        )
        self._disconnection_timeout = config.timeout_disconnection
        self._shutdown_timeout = config.timeout_shutdown

    async def run_async(self) -> None:
        self._runtime_task = self.kernel.loop.create_task(super().run_async())
        startup_task = self.kernel.loop.create_task(self._await_startup())
        try:
            done, _ = await asyncio.wait(
                (self._runtime_task, startup_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if startup_task in done:
                await startup_task
            await self._runtime_task
        finally:
            startup_task.cancel()
            await asyncio.gather(startup_task, return_exceptions=True)
            await self.stop_async()

    async def _await_startup(self) -> None:
        deadline = self.kernel.loop.time() + self._startup_timeout
        while not self.trader.is_running:
            if self.kernel.loop.time() >= deadline:
                raise RuntimeError(
                    f"Live node startup failed after {self._startup_timeout}s: "
                    "connections, execution reconciliation or portfolio initialization "
                    "did not complete; shutting down",
                )
            await asyncio.sleep(0.05)

    async def stop_async(self) -> None:
        # A signal and run_async's finally block can both request shutdown. They
        # must await the same task, so dispose() never races with disconnection.
        if self._shutdown_task is None:
            self._shutdown_task = self.kernel.loop.create_task(self._stop_runtime())
        await asyncio.shield(self._shutdown_task)

    async def _stop_runtime(self) -> None:
        if self._runtime_task is not None and not self.trader.is_running:
            self._runtime_task.cancel()
            await asyncio.gather(self._runtime_task, return_exceptions=True)
        if self.is_running():
            existing_tasks = asyncio.all_tasks(self.kernel.loop)
            await super().stop_async()
            # Failed connections are already marked disconnected. Nautilus can
            # therefore finish stop_async before the newly scheduled client
            # disconnect coroutines finish closing their transports.
            cleanup_tasks = asyncio.all_tasks(self.kernel.loop) - existing_tasks
            if cleanup_tasks:
                done, pending = await asyncio.wait(
                    cleanup_tasks,
                    timeout=self._disconnection_timeout,
                )
                for task in done:
                    if not task.cancelled() and task.exception() is not None:
                        self.kernel.logger.exception("Client shutdown failed", task.exception())
                if pending:
                    self.kernel.logger.warning("Client shutdown timed out; cancelling pending tasks")
                    for task in pending:
                        task.cancel()
                    await asyncio.wait(pending, timeout=self._shutdown_timeout)
        # Once trading has started, keep the engine queues alive until Nautilus
        # has stopped the trader and allowed its order cancellations to finish.
        if self._runtime_task is not None:
            self._runtime_task.cancel()
            await asyncio.gather(self._runtime_task, return_exceptions=True)
