from __future__ import annotations

import asyncio
import unittest

from strategies.async_scheduling import AsyncAwaitableScheduler


class AsyncAwaitableSchedulerTest(unittest.TestCase):
    def test_schedule_resolves_without_running_loop(self) -> None:
        async def value() -> int:
            return 7

        future = AsyncAwaitableScheduler().schedule(value())

        self.assertTrue(future.done())
        self.assertEqual(future.result(), 7)

    def test_schedule_uses_captured_running_loop(self) -> None:
        async def scenario() -> int:
            scheduler = AsyncAwaitableScheduler()
            scheduler.capture_running_loop()

            async def value() -> int:
                await asyncio.sleep(0)
                return 11

            future = scheduler.schedule(value())
            return await asyncio.wrap_future(future)

        self.assertEqual(asyncio.run(scenario()), 11)
