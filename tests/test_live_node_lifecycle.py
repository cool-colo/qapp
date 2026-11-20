from __future__ import annotations

import asyncio
import subprocess
import sys
import unittest
from pathlib import Path

from pipe_runtime import configure_runtime_paths

PROJECT_ROOT = Path(__file__).resolve().parents[1]
configure_runtime_paths(PROJECT_ROOT)

from lives.trading_node import LiveTradingNode  # noqa: E402
from nautilus_trader.config import LiveDataClientConfig  # noqa: E402
from nautilus_trader.config import LiveExecEngineConfig  # noqa: E402
from nautilus_trader.config import LoggingConfig  # noqa: E402
from nautilus_trader.config import TradingNodeConfig  # noqa: E402
from nautilus_trader.live.data_client import LiveDataClient  # noqa: E402
from nautilus_trader.live.factories import LiveDataClientFactory  # noqa: E402
from nautilus_trader.model.identifiers import ClientId  # noqa: E402
from nautilus_trader.trading.strategy import Strategy  # noqa: E402


class LifecycleDataClientConfig(LiveDataClientConfig, frozen=True):
    fail_connect: bool = False
    connect_successfully: bool = False


class LifecycleDataClient(LiveDataClient):
    def __init__(self, *, config, **kwargs):
        super().__init__(config=config, **kwargs)
        self.fail_connect = config.fail_connect
        self.connect_successfully = config.connect_successfully

    async def _connect(self) -> None:
        if self.fail_connect:
            print("CONNECT_FAILED", flush=True)
            raise RuntimeError("QMT-side pipe is unavailable")
        if self.connect_successfully:
            print("CONNECTED", flush=True)
            return
        print("CONNECT_WAITING", flush=True)
        await asyncio.sleep(3600)

    async def _disconnect(self) -> None:
        await asyncio.sleep(0.05)
        print("DISCONNECTED", flush=True)


class LifecycleDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(loop, name, config, msgbus, cache, clock):
        return LifecycleDataClient(
            loop=loop,
            client_id=ClientId(name),
            venue=None,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )


class LifecycleStrategy(Strategy):
    def __init__(self, queue_running):
        super().__init__()
        self.queue_running = queue_running

    def on_start(self) -> None:
        print("STRATEGY_STARTED", flush=True)

    def on_stop(self) -> None:
        print(f"STOP_QUEUE_RUNNING={self.queue_running()}", flush=True)


def build_lifecycle_node(
    *, fail_connect: bool, timeout: float, connect_successfully: bool = False,
) -> LiveTradingNode:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    node = LiveTradingNode(
        config=TradingNodeConfig(
            logging=LoggingConfig(log_level="ERROR"),
            exec_engine=LiveExecEngineConfig(reconciliation=False),
            data_clients={"TEST": LifecycleDataClientConfig(
                fail_connect=fail_connect,
                connect_successfully=connect_successfully,
            )},
            timeout_connection=timeout,
            timeout_reconciliation=0.05,
            timeout_portfolio=0.05,
            timeout_disconnection=0.5,
            timeout_post_stop=0.05,
        ),
        loop=loop,
    )
    node.add_data_client_factory("TEST", LifecycleDataClientFactory)
    node.build()
    if connect_successfully:
        node.trader.add_strategy(LifecycleStrategy(
            lambda: not node.kernel.exec_engine.get_cmd_queue_task().done(),
        ))
    return node


_PROCESS_DRIVER = """
import os
import signal
import sys

from tests.test_live_node_lifecycle import build_lifecycle_node

scenario = sys.argv[1]
node = build_lifecycle_node(
    fail_connect=scenario.startswith("failed"),
    timeout=600.0 if scenario in ("waiting_sigint", "failed_sigint") else 0.1,
    connect_successfully=scenario == "running_sigint",
)
if scenario.endswith("sigint"):
    delay = 0.5 if scenario == "running_sigint" else 0.2
    node.get_event_loop().call_later(delay, os.kill, os.getpid(), signal.SIGINT)
try:
    node.run(raise_exception=True)
finally:
    assert not node.is_running(), "dispose must follow completed shutdown"
    node.dispose()
    print("DISPOSED", flush=True)
"""


class LiveNodeLifecycleTest(unittest.TestCase):
    def run_node_process(self, scenario: str) -> subprocess.CompletedProcess:
        # Real SIGINT handling is process-wide, so isolate it from pytest's loop
        # and verify that the Python process itself exits after cleanup.
        return subprocess.run(
            [sys.executable, "-c", _PROCESS_DRIVER, scenario],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_ctrl_c_exits_during_connection_wait(self) -> None:
        result = self.run_node_process("waiting_sigint")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CONNECT_WAITING", result.stdout)
        self.assertIn("DISCONNECTED\nDISPOSED", result.stdout)

    def test_ctrl_c_exits_after_connection_error(self) -> None:
        result = self.run_node_process("failed_sigint")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CONNECT_FAILED", result.stdout)
        self.assertIn("DISCONNECTED\nDISPOSED", result.stdout)

    def test_failed_startup_exits_with_error_and_cleans_up(self) -> None:
        result = self.run_node_process("failed_timeout")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Live node startup failed", result.stderr)
        self.assertIn("DISCONNECTED\nDISPOSED", result.stdout)

    def test_started_node_keeps_execution_queue_alive_until_strategy_stops(self) -> None:
        result = self.run_node_process("running_sigint")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("STRATEGY_STARTED", result.stdout)
        self.assertIn("STOP_QUEUE_RUNNING=True", result.stdout)
        self.assertIn("DISCONNECTED\nDISPOSED", result.stdout)
