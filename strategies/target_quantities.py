from __future__ import annotations

import asyncio
import inspect
import random
import threading
import time
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.data import OrderBookDepth10
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.trading.strategy import Strategy

from strategies.pricing import OpenOffsetBuyPriceStrategy
from strategies.pricing import OpenOffsetSellPriceStrategy
from strategies.pricing import PriceContext
from strategies.order_splitter import NotionalOrderSplitter


async def _await_awaitable(awaitable: Any) -> Any:
    return await awaitable


@dataclass(frozen=True)
class TargetQuantityTargetEvent:
    target_id: str
    target_date: date
    execute_date: date
    instrument_id: str
    target_qty: Decimal
    current_qty: Decimal | None
    delta_qty: Decimal | None
    reason: str
    extra: dict[str, Any]


@dataclass(frozen=True)
class TargetQuantityOrderEvent:
    order_id: str
    trading_date: date
    instrument_id: str
    side: str
    quantity: int
    target_qty: Decimal
    status: str
    reason: str | None
    extra: dict[str, Any]


@dataclass(frozen=True)
class IntentContext:
    instrument_id: str
    target_qty: Decimal
    current_qty: Decimal
    delta_qty: Decimal
    price_context: PriceContext | None
    pricer_class: str | None
    details: dict[str, Any] = field(default_factory=dict)

    def record(self, **details: Any) -> None:
        for key, value in details.items():
            if value is not None:
                self.details[key] = value

    def snapshot(self) -> dict[str, Any]:
        result = {
            "instrument_id": self.instrument_id,
            "target_qty": self.target_qty,
            "current_qty": self.current_qty,
            "delta_qty": self.delta_qty,
            "pricer_class": self.pricer_class,
            "price_context": self._price_context_snapshot(),
        }
        result.update(self.details)
        return result

    def log_text(self) -> str:
        return self._format_value(self.snapshot())

    def _price_context_snapshot(self) -> dict[str, Any] | None:
        ctx = self.price_context
        if ctx is None:
            return None
        return {
            "instrument_id": str(ctx.instrument_id),
            "side": ctx.side.name,
            "open_price": ctx.open_price,
            "last_close": ctx.last_close,
            "tick": ctx.tick,
            "quantity": ctx.quantity,
            "cancel_count": ctx.cancel_count,
            "best_bid": ctx.best_bid,
            "best_ask": ctx.best_ask,
            "bids": ctx.bids,
            "asks": ctx.asks,
        }

    @classmethod
    def _format_value(cls, value: Any) -> str:
        if isinstance(value, dict):
            return "{" + ", ".join(
                f"{key}={cls._format_value(item)}"
                for key, item in sorted(value.items())
            ) + "}"
        if isinstance(value, list):
            return "[" + ", ".join(cls._format_value(item) for item in value) + "]"
        if isinstance(value, tuple):
            return "(" + ", ".join(cls._format_value(item) for item in value) + ")"
        return str(value)


@dataclass(frozen=True)
class TargetOrderIntent:
    instrument_id: InstrumentId
    instrument: Any
    side: OrderSide
    quantity: Decimal
    price: Any
    context: IntentContext


@dataclass(frozen=True)
class TodayFillSnapshot:
    buy_qty: Decimal
    sell_qty: Decimal
    fill_count: int
    latest_ts_event: int



class TargetQuantityStrategyConfig(StrategyConfig, kw_only=True, frozen=True):
    instrument_ids: list[InstrumentId]
    bar_types: dict[str, BarType]
    initial_cash: Decimal = Decimal("1000000")
    timezone_name: str = "Asia/Shanghai"
    trading_windows: str = "09:29-11:30,13:00-14:55"
    exchange_trading_windows: str = "09:30-11:30,13:00-14:55"
    stop_time: str | None = "14:55"
    initial_last_closes: dict[str, float] | None = None
    unfilled_timeout_secs: float = 60.0
    resubmit_check_interval_secs: float = 10.0
    cash_buffer_percent: float = 0.01
    target_cash_buffer_percent: float = 0.05
    buy_offset_bps: float = 5.0
    sell_offset_bps: float = 5.0
    buy_max_price_bps: float = 10.0
    buy_cancel_threshold: int = 2
    sell_cancel_threshold: int = 1
    exit_non_targets: bool = True
    limit_stop_mode: str = "freeze_symbol"
    order_slice_notional: Decimal = Decimal("300000")
    require_account_cash: bool = True
    trade_tick_log_sample_rate: float = 0.0
    order_book_depth_log_sample_rate: float = 0.0
    subscribe_bars: bool = True
    subscribe_quote_ticks: bool = True
    subscribe_trade_ticks: bool = True
    quote_tick_window_probe_instrument_ids: tuple[InstrumentId, ...] = ()
    subscribe_order_book_depth: bool = False
    full_tick_refresh_secs: float = 1.0
    full_tick_prefetch_time: str | None = "09:27"


class TargetQuantityStrategy(Strategy):
    """
    Nautilus-first target-quantity executor.

    Subclasses or external controllers provide per-instrument target share counts
    (固定目标股数). This class owns the account-aware convergence loop: exits,
    cash-gated buys, stale-order cancel/resubmit, symbol freezes, and achievement
    checks. Sizing is a direct share-count delta (target_qty - current_qty); no
    weights are consulted.
    """

    _UP_LIMIT_KEYS = ("UpStopPrice", "up_stop_price", "high_limit", "up_limit", "limit_up", "\u6da8\u505c\u4ef7")
    _DOWN_LIMIT_KEYS = (
        "DownStopPrice",
        "down_stop_price",
        "low_limit",
        "down_limit",
        "limit_down",
        "\u8dcc\u505c\u4ef7",
    )
    _PRE_OPEN_RECONCILE_ALERT = "TARGET-WEIGHT-PRE-OPEN-RECONCILE"
    _FULL_TICK_REFRESH_TIMER = "TARGET-WEIGHT-FULL-TICK-REFRESH"
    _FULL_TICK_PREFETCH_ALERT = "TARGET-WEIGHT-FULL-TICK-PREFETCH"
    _TERMINAL_ORDER_STATUSES = {
        OrderStatus.DENIED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.FILLED,
    }

    def __init__(self, config: TargetQuantityStrategyConfig) -> None:
        super().__init__(config)
        self._instrument_ids = list(config.instrument_ids)
        self._bar_types = {str(key): value for key, value in config.bar_types.items()}
        self._last_close = normalize_initial_last_closes(config.initial_last_closes)
        # Per-instrument target share count (固定目标股数). ``0`` is a valid target
        # (liquidate / hold-none) and is retained. This is the sole sizing input:
        # each cycle converges current_qty -> target_qty. No weights are consulted.
        self._target_quantities: dict[str, Decimal] = {}
        self._target_date: date | None = None
        self._target_reason = "target_quantity"
        self._target_version = ""
        self._achieved_versions: set[str] = set()
        self._frozen_instruments: dict[str, str] = {}
        self._deferred_buys: dict[str, Decimal] = {}
        self._converge_lock = threading.Lock()
        self._rejected_order_ids: set[str] = set()
        self._insufficient_funds: set[str] = set()
        self._order_target_versions: dict[str, str] = {}
        self._order_intent_contexts: dict[str, IntentContext] = {}
        self._order_splitter = NotionalOrderSplitter(config.order_slice_notional)
        self._buy_pricer = OpenOffsetBuyPriceStrategy(
            offset_bps=float(config.buy_offset_bps),
            max_price_bps=float(config.buy_max_price_bps),
            cancel_threshold=int(config.buy_cancel_threshold),
        )
        self._sell_pricer = OpenOffsetSellPriceStrategy(
            offset_bps=float(config.sell_offset_bps),
            cancel_threshold=int(config.sell_cancel_threshold),
        )
        # Today's open price per instrument (first bar of the trading date wins).
        self._today_open: dict[str, float] = {}
        # Instruments whose _today_open came from an authoritative source (real
        # bar open or QMT full-tick).
        self._authoritative_open: set[str] = set()
        # True once the first depth-driven convergence of the current trading day
        # has fired (reset per day in _roll_trading_day). Lets the earliest
        # order-book depth callback submit orders as early as possible.
        self._depth_converged_today = False
        self._depth_books: dict[str, tuple[list[tuple[float, float]], list[tuple[float, float]]]] = {}
        self._subscribed_order_book_depth_instruments: set[str] = set()
        self._sleep = time.sleep
        # Side-specific cancel counts per instrument; reset daily and on fill.
        self._cancel_count_buy: dict[str, int] = {}
        self._cancel_count_sell: dict[str, int] = {}
        self._trading_day: date | None = None
        # Latest quote tick event timestamp. The order gate requires both the
        # local clock and the exchange event timestamp to be inside their
        # configured windows, preventing stale/pre-open quotes from unlocking
        # trading purely because local time has advanced.
        self._last_quote_tick_ts_event: int = 0
        self._quote_tick_window_probe_ids = {
            str(instrument_id)
            for instrument_id in config.quote_tick_window_probe_instrument_ids
        }
        self._subscribed_quote_tick_probe_instruments: set[str] = set()
        self._convergence_suspended = False
        self._pre_open_reconcile = None
        self._pre_open_reconcile_time: tuple[int, int] | None = None
        self._pre_open_reconcile_timeout_secs = 30.0
        self._pre_open_reconcile_task: asyncio.Future[Any] | ConcurrentFuture[Any] | None = None
        # Node event loop for marshalling async callbacks. LiveClock time-alert callbacks
        # (pre-open reconcile, full-tick fetch) fire on the Rust timer thread, NOT the
        # asyncio loop thread, so async work must be scheduled onto this loop via
        # run_coroutine_threadsafe. None in backtest (callbacks never wired).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._full_tick_source: Any | None = None
        self._full_tick_prefetch_time: tuple[int, int] | None = self._parse_hh_mm(
            config.full_tick_prefetch_time,
        )
        self._full_tick_task: asyncio.Future[Any] | ConcurrentFuture[Any] | None = None
        self._sellable_exhausted: dict[str, date] = {}
        # Broker-reported sellable quantity per instrument (QMT `can_use_volume`), refreshed
        # from execution mass-status reports. Empty in backtest (no venue reports) -> the
        # fill-based estimate is used. When populated it is authoritative over the estimate.
        self._venue_sellable: dict[str, Decimal] = {}
        self._venue_sellable_ts: int = 0
        self._last_account_sizing_snapshot: str | None = None
        self.target_events: list[TargetQuantityTargetEvent] = []
        self.order_events: list[TargetQuantityOrderEvent] = []

    def configure_pre_open_reconciliation(
        self,
        reconcile: Any,
        reconcile_time: str,
        timeout_secs: float,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._pre_open_reconcile = reconcile
        parsed_time = self._parse_hh_mm(reconcile_time)
        if parsed_time is None:
            raise ValueError("pre-open reconcile time is required")
        self._pre_open_reconcile_time = parsed_time
        self._pre_open_reconcile_timeout_secs = float(timeout_secs)
        if loop is not None:
            self._loop = loop

    def configure_full_tick_source(self, fetch_full_tick: Any | None) -> None:
        """
        Inject an async callback returning today's authoritative full-tick snapshot
        as ``{instrument_id_text: {"open": ..., ...}}``. Called at start, at the
        configured pre-open prefetch time, and every ``full_tick_refresh_secs``
        during the trading window. Nautilus has no full-tick data type, so this
        callback reaches the QMT proxy directly (infrastructure plumbing). Only the
        ``open`` field is consumed today; the snapshot carries the full tick so more
        fields (last price, bid/ask, ...) can be used later without rewiring.
        """
        self._full_tick_source = fetch_full_tick

    def on_start(self) -> None:
        # Capture the node event loop while running on the loop thread. Used to
        # marshal async callbacks (reconcile, full-tick) that later fire on the
        # LiveClock timer thread. An explicit loop from configure_* wins if set.
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None
        self.log.info(
            f"target-weight executor start: instruments={len(self._instrument_ids)} "
            f"bar_types={len(self._bar_types)} target_version={self._target_version or '<none>'}",
            color=LogColor.BLUE,
        )
        self._schedule_pre_open_reconcile()
        self._subscribe_execution_mass_status()
        if not self._bar_types:
            self.log.warning("target-weight executor has no bar_types configured")
        for bar_type in self._bar_types.values():
            self._subscribe_market_data(bar_type)
        self._subscribe_quote_tick_window_probes()
        interval_secs = float(self.config.resubmit_check_interval_secs)
        if float(self.config.unfilled_timeout_secs) > 0 and interval_secs > 0:
            self.clock.set_timer(
                name="TARGET-WEIGHT-CONVERGE",
                interval=timedelta(seconds=interval_secs),
                callback=self._on_converge_timer,
                fire_immediately=False,
            )
        self._start_full_tick_refresh()

    def _start_full_tick_refresh(self) -> None:

        refresh_secs = float(self.config.full_tick_refresh_secs)
        self.clock.set_timer(
            name=self._FULL_TICK_REFRESH_TIMER,
            interval=timedelta(seconds=refresh_secs),
            callback=self._on_full_tick_refresh_timer,
            fire_immediately=False,
        )
        self._schedule_full_tick_prefetch()
        # Prime the snapshot immediately on start (e.g. mid-session restart) so the
        # first convergence prices against real opens.
        self._run_full_tick_fetch(trigger="start")

    def _schedule_full_tick_prefetch(self) -> None:
        alert_time = self._next_daily_time(self._full_tick_prefetch_time)
        self.clock.set_time_alert(
            name=self._FULL_TICK_PREFETCH_ALERT,
            alert_time=alert_time,
            callback=self._on_full_tick_prefetch_timer,
            override=True,
        )
        self.log.info(
            f"Next full-tick prefetch scheduled for {alert_time.isoformat()} "
            f"({self.config.timezone_name})",
            color=LogColor.BLUE,
        )

    def _on_full_tick_prefetch_timer(self, _event: Any) -> None:
        if self._full_tick_prefetch_time is not None:
            self._schedule_full_tick_prefetch()
        self._run_full_tick_fetch(trigger="prefetch")

    def _on_full_tick_refresh_timer(self, _event: Any) -> None:
        if not self._within_trading_window():
            return
        self._run_full_tick_fetch(trigger="refresh")

    def _run_full_tick_fetch(self, trigger: str) -> None:

        if self._full_tick_task is not None and not self._full_tick_task.done():
            return
        try:
            result = self._full_tick_source()
        except Exception as exc:
            self.log.warning(f"full-tick fetch failed to start ({trigger}): {exc}")
            return
        if not inspect.isawaitable(result):
            self._apply_full_tick(result, trigger)
            return
        task = self._schedule_on_loop(result)
        if task is None:
            return
        self._full_tick_task = task
        task.add_done_callback(lambda t: self._on_full_tick_fetch_done(t, trigger))

    def _on_full_tick_fetch_done(self, task: asyncio.Future[Any], trigger: str) -> None:
        self._full_tick_task = None
        try:
            result = task.result()
        except Exception as exc:
            self.log.warning(f"full-tick fetch failed ({trigger}): {exc}")
            return
        self._apply_full_tick(result, trigger)

    def _apply_full_tick(self, snapshot: Any, trigger: str) -> None:
        """
        Apply an authoritative full-tick snapshot keyed by instrument id. Each
        value is a per-instrument field mapping (e.g. ``{"open": ..., "last_price":
        ...}``). Today only the ``open`` field is consumed (to anchor pricing); the
        snapshot shape is deliberately open so future fields can be used without a
        signature change.
        """
        if not isinstance(snapshot, dict) or not snapshot:
            self.log.warning(f"full-tick snapshot is invalid! ({trigger})")
            return
        trading_date = self._clock_date()
        updated = 0
        for instrument_id, fields in snapshot.items():
            open_price = self._full_tick_open(fields)
            if open_price is None:
                self.log.warning(f"full-tick snapshot contains invalid open price for {instrument_id} ({trigger})")
                continue
            if self._set_authoritative_open(str(instrument_id), trading_date, open_price):
                updated += 1
        if updated:
            self.log.info(
                f"applied authoritative opens from full-tick ({trigger}): "
                f"date={trading_date} count={updated}",
                color=LogColor.BLUE,
            )

    @staticmethod
    def _full_tick_open(fields: Any) -> float | None:
        """Extract a positive open price from a full-tick value (mapping or float)."""
        raw = fields.get("open") if isinstance(fields, dict) else fields
        try:
            price = float(raw)
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    def _set_authoritative_open(
        self,
        instrument_id: str,
        trading_date: date,
        open_price: float,
    ) -> bool:
        """Set today's open from an authoritative source."""
        if open_price <= 0:
            return False
        self._roll_trading_day(trading_date)
        previous = self._today_open.get(instrument_id)
        self._today_open[instrument_id] = float(open_price)
        self._authoritative_open.add(instrument_id)
        return previous != float(open_price)

    @staticmethod
    def _parse_hh_mm(value: str | None) -> tuple[int, int] | None:
        if not value or not str(value).strip():
            return None
        hh, mm = str(value).strip().split(":")
        return int(hh), int(mm)

    def _next_daily_time(self, hh_mm: tuple[int, int] | None) -> pd.Timestamp:
        if hh_mm is None:
            raise RuntimeError("daily time is not configured")
        tz = self.config.timezone_name
        now = pd.Timestamp(self.clock.utc_now()).tz_convert(tz)
        hh, mm = hh_mm
        target = now.normalize() + pd.Timedelta(hours=hh, minutes=mm)
        if target <= now:
            target = target + pd.Timedelta(days=1)
        return target

    def _schedule_pre_open_reconcile(self) -> None:
        alert_time = self._next_daily_time(self._pre_open_reconcile_time)
        self.clock.set_time_alert(
            name=self._PRE_OPEN_RECONCILE_ALERT,
            alert_time=alert_time,
            callback=self._on_pre_open_reconcile_timer,
            override=True,
        )
        self.log.info(
            f"Next pre-open execution-state reconciliation scheduled for {alert_time.isoformat()} "
            f"({self.config.timezone_name})",
            color=LogColor.BLUE,
        )

    def _on_pre_open_reconcile_timer(self, _event: Any) -> None:
        self._schedule_pre_open_reconcile()
        self._run_pre_open_reconcile()

    def _schedule_on_loop(
        self,
        awaitable: Any,
    ) -> asyncio.Future[Any] | ConcurrentFuture[Any] | None:
        """
        Schedule an awaitable onto the node's event loop and return a future.

        LiveClock time-alert callbacks fire on the Rust timer thread, so there is no
        running loop here and the awaitable's I/O (aiohttp) is bound to the node loop
        on another thread. Marshal onto that loop with run_coroutine_threadsafe; the
        returned concurrent.futures.Future exposes done()/result()/add_done_callback()
        just like an asyncio.Future.

        When no node loop is available (backtest/tests, or a plain-async callback whose
        I/O is not bound to another loop) the awaitable is run synchronously to
        completion and an already-resolved future is returned, so the caller's
        add_done_callback still fires (immediately) and result-logging is preserved.
        Returns None only if the awaitable could not be run at all.
        """
        loop = self._loop
        if loop is not None and loop.is_running():
            return asyncio.run_coroutine_threadsafe(_await_awaitable(awaitable), loop)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            return asyncio.ensure_future(awaitable, loop=running)
        resolved: ConcurrentFuture[Any] = ConcurrentFuture()
        try:
            resolved.set_result(asyncio.run(_await_awaitable(awaitable)))
        except Exception as exc:  # noqa: BLE001 - surfaced via the future
            resolved.set_exception(exc)
        return resolved

    def _run_pre_open_reconcile(self) -> None:
        if self._pre_open_reconcile_task is not None and not self._pre_open_reconcile_task.done():
            self.log.warning("Previous pre-open execution-state reconciliation is still running; skipping")
            return
        try:
            result = self._pre_open_reconcile(timeout_secs=self._pre_open_reconcile_timeout_secs)
        except Exception as exc:
            self.log.warning(f"Pre-open execution-state reconciliation failed to start: {exc}")
            return
        if inspect.isawaitable(result):
            task = self._schedule_on_loop(result)
            if task is None:
                return
            self._pre_open_reconcile_task = task
            task.add_done_callback(self._on_pre_open_reconcile_done)
            self.log.info("Started pre-open execution-state reconciliation", color=LogColor.BLUE)
            return
        self._log_pre_open_reconcile_result(bool(result))

    def request_execution_reconcile(self) -> None:
        """
        Trigger an execution-state reconcile using the configured pre-open callback,
        if available. Used to refresh the broker sellable map (``_venue_sellable``)
        outside the scheduled pre-open window (e.g. on the periodic refresh timer).
        Live target-model nodes configure this callback during startup.
        """
        self._run_pre_open_reconcile()

    def _on_pre_open_reconcile_done(self, task: asyncio.Future[Any]) -> None:
        self._pre_open_reconcile_task = None
        try:
            result = task.result()
        except Exception as exc:
            self.log.warning(f"Pre-open execution-state reconciliation failed: {exc}")
            return
        self._log_pre_open_reconcile_result(bool(result))

    def _log_pre_open_reconcile_result(self, succeeded: bool) -> None:
        if succeeded:
            self.log.info("Pre-open execution-state reconciliation succeeded", color=LogColor.GREEN)
        else:
            self.log.warning("Pre-open execution-state reconciliation did not complete successfully")

    def refresh_target_instruments(
        self,
        instrument_ids: list[InstrumentId],
        bar_types: dict[str, BarType],
        last_closes: dict[str, float] | None = None,
        subscribe_new_bars: bool = True,
        unsubscribe_removed_bars: bool = False,
    ) -> None:
        existing_bar_type_keys = set(self._bar_types)
        refreshed_bar_types = {str(key): value for key, value in bar_types.items()}
        if unsubscribe_removed_bars:
            removable = existing_bar_type_keys.difference(refreshed_bar_types).difference(self._target_quantities)
            for key in sorted(removable):
                if self._current_quantity(InstrumentId.from_str(key)) > 0:
                    continue
                self._unsubscribe_market_data(self._bar_types[key])
                self._bar_types.pop(key, None)
        self._bar_types.update(refreshed_bar_types)
        known_ids = {str(instrument_id): instrument_id for instrument_id in self._instrument_ids}
        for instrument_id in instrument_ids:
            known_ids[str(instrument_id)] = instrument_id
        self._instrument_ids = list(known_ids.values())
        self._last_close.update(normalize_initial_last_closes(last_closes))
        if subscribe_new_bars:
            for key, bar_type in self._bar_types.items():
                if key in existing_bar_type_keys:
                    continue
                try:
                    self.request_instrument(bar_type.instrument_id)
                except Exception as exc:
                    self.log.warning(f"Instrument request failed for {bar_type.instrument_id}: {exc}")
                self._subscribe_market_data(bar_type)

    def _subscribe_market_data(self, bar_type: BarType) -> None:
        if bool(self.config.subscribe_bars):
            self.subscribe_bars(bar_type)
        if bool(self.config.subscribe_quote_ticks):
            self.subscribe_quote_ticks(bar_type.instrument_id)
        if bool(self.config.subscribe_trade_ticks):
            self.subscribe_trade_ticks(bar_type.instrument_id)

    def _subscribe_quote_tick_window_probes(self) -> None:
        if bool(self.config.subscribe_quote_ticks):
            return
        for instrument_id_text in sorted(self._quote_tick_window_probe_ids):
            if instrument_id_text in self._subscribed_quote_tick_probe_instruments:
                continue
            instrument_id = InstrumentId.from_str(instrument_id_text)
            try:
                self.subscribe_quote_ticks(instrument_id)
            except Exception as exc:
                self.log.warning(f"Quote-tick window probe subscribe failed for {instrument_id}: {exc}")
                continue
            self._subscribed_quote_tick_probe_instruments.add(instrument_id_text)
        if self._subscribed_quote_tick_probe_instruments:
            self.log.info(
                "Subscribed quote-tick window probes: "
                f"count={len(self._subscribed_quote_tick_probe_instruments)} "
                f"instruments={sorted(self._subscribed_quote_tick_probe_instruments)}",
                color=LogColor.BLUE,
            )

    def _unsubscribe_market_data(self, bar_type: BarType) -> None:
        if bool(self.config.subscribe_bars):
            try:
                self.unsubscribe_bars(bar_type)
            except Exception as exc:
                self.log.warning(f"Bar unsubscribe failed for {bar_type}: {exc}")

    def update_target_quantities(
        self,
        quantities: dict[str | InstrumentId, int | Decimal],
        target_date: date,
        reason: str,
        version: str | None = None,
    ) -> None:
        """
        Accept the day's per-instrument target share counts (固定目标股数).

        Quantity is the sole sizing input: each convergence cycle drives current_qty
        toward target_qty per instrument. ``0`` is a valid target (liquidate / hold
        none) and is retained. Called from the bar/timer path and from the snapshot
        recorder (once per day, after the target is generated or loaded from MySQL on
        restart).
        """
        normalized: dict[str, Decimal] = {}
        for instrument_id, quantity in quantities.items():
            instrument_id_text = str(instrument_id)
            try:
                value = Decimal(str(quantity))
            except Exception:
                continue
            if value < 0:
                continue
            normalized[instrument_id_text] = value
        version_value = version or target_version(target_date, normalized, reason)
        if (
            version_value == self._target_version
            and normalized == self._target_quantities
        ):
            self.log.info(
                f"target quantities unchanged for version={version_value} date={target_date} "
                f"count={len(normalized)} reason={reason}",
                color=LogColor.BLUE,
            )
            return
        self._refresh_order_book_depth_subscriptions(normalized)
        self._target_quantities = dict(sorted(normalized.items()))
        self._target_date = target_date
        self._target_reason = reason
        self._target_version = version_value
        self._frozen_instruments = {}
        self._deferred_buys = {}
        self._insufficient_funds = set()
        self._achieved_versions.discard(version_value)
        detail = self._target_quantities_log_detail(self._target_quantities)
        self.log.info(
            f"accepted target quantities version={version_value} date={target_date} "
            f"count={len(self._target_quantities)} reason={reason} detail={detail}",
            color=LogColor.BLUE,
        )
        if self._convergence_suspended:
            return
        self._converge_to_target(current_date=target_date, trigger="target_update")

    def _refresh_order_book_depth_subscriptions(self, quantities: dict[str, Decimal]) -> None:
        subscribed_ids = set(self._subscribed_order_book_depth_instruments)
        desired_ids = set(quantities).union(self._held_instrument_ids())
        removable_ids = subscribed_ids.difference(desired_ids)
        addable_ids = desired_ids.difference(subscribed_ids)
        removed_ids: set[str] = set()
        for instrument_id_text in sorted(removable_ids):
            try:
                self.unsubscribe_order_book_depth(InstrumentId.from_str(instrument_id_text))
            except Exception as exc:
                self.log.warning(
                    f"Order-book depth unsubscribe failed for {instrument_id_text}: {exc}",
                )
                continue
            subscribed_ids.discard(instrument_id_text)
            removed_ids.add(instrument_id_text)
        added_ids: set[str] = set()
        for instrument_id_text in sorted(addable_ids):
            instrument_id = InstrumentId.from_str(instrument_id_text)
            try:
                self.subscribe_order_book_depth(
                    instrument_id,
                    book_type=BookType.L2_MBP,
                    depth=10,
                )
            except Exception as exc:
                self.log.warning(
                    f"Order-book depth subscribe failed for {instrument_id_text}: {exc}",
                )
                continue
            subscribed_ids.add(instrument_id_text)
            added_ids.add(instrument_id_text)
        self._subscribed_order_book_depth_instruments = subscribed_ids
        if added_ids:
            self.log.info(
                "Updated order-book depth subscriptions: "
                f"subscribed={sorted(added_ids)} unsubscribed={sorted(removed_ids)} "
                f"active_count={len(subscribed_ids)}",
                color=LogColor.BLUE,
            )
            self._sleep(10.0)

    @staticmethod
    def _target_quantities_log_detail(quantities: dict[str, Decimal]) -> str:
        if not quantities:
            return "[]"
        return "[" + ", ".join(
            f"{instrument_id}={int(quantity)}"
            for instrument_id, quantity in sorted(quantities.items())
        ) + "]"

    def on_bar(self, bar: Bar) -> None:
        instrument_id = str(bar.bar_type.instrument_id)
        trading_date = bar_date(bar, self.config.timezone_name)
        self._update_price_state(
            instrument_id=instrument_id,
            trading_date=trading_date,
            last_price=float(bar.close),
        )
        # The first real bar of the day carries the true daily open; later intraday
        # bars must not clobber it. Full-tick fetches (below) remain authoritative
        # and may still refine it.
        self._roll_trading_day(trading_date)
        if instrument_id not in self._authoritative_open:
            self._set_authoritative_open(instrument_id, trading_date, float(bar.open))
        within_window = self._within_trading_window()
        self._convergence_suspended = not within_window
        self.on_target_bar(bar)
        self._convergence_suspended = False
        self._converge_to_target(current_date=trading_date, trigger="bar")

    def on_target_bar(self, bar: Bar) -> None:
        """Hook for subclasses to update targets before convergence."""

    def _roll_trading_day(self, trading_date: date) -> None:
        """Roll intraday state to ``trading_date``.

        Idempotent within a day: when the trading date is unchanged this is a
        no-op. On a day boundary it resets all per-day intraday state (today's
        opens, depth books, the first-depth convergence flag, cancel counts) and
        drops stale quote timestamps so they cannot satisfy the exchange window
        gate.
        """
        if trading_date == self._trading_day:
            return
        self._trading_day = trading_date
        self._today_open = {}
        self._authoritative_open = set()
        self._depth_books = {}
        self._depth_converged_today = False
        self._cancel_count_buy = {}
        self._cancel_count_sell = {}
        # New trading day: stale quote timestamps must not satisfy the exchange
        # window gate.
        if self._event_trading_date(self._last_quote_tick_ts_event) != trading_date:
            self._last_quote_tick_ts_event = 0

    def _update_price_state(
        self,
        instrument_id: str,
        trading_date: date,
        last_price: float | None = None,
    ) -> None:
        self._roll_trading_day(trading_date)
        if last_price is not None and last_price > 0:
            self._last_close[instrument_id] = float(last_price)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        self._note_quote_tick(tick)
        if not self._within_pre_open_quote_log_window():
            return
        self.log.info(
            "Quote tick order-book sample (forced pre-open diagnostics), "
            f"instrument_id={tick.instrument_id}, "
            f"bid_price={tick.bid_price}, bid_size={tick.bid_size}, "
            f"ask_price={tick.ask_price}, ask_size={tick.ask_size}, "
            f"ts_event={tick.ts_event}, ts_init={tick.ts_init}",
            color=LogColor.CYAN,
        )

    def on_trade_tick(self, tick: TradeTick) -> None:
        if not self._should_log_sample(self.config.trade_tick_log_sample_rate):
            return
        self.log.info(
            "Trade tick sample, "
            f"instrument_id={tick.instrument_id}, "
            f"price={tick.price}, size={tick.size}, "
            f"aggressor_side={tick.aggressor_side}, trade_id={tick.trade_id}, "
            f"ts_event={tick.ts_event}, ts_init={tick.ts_init}",
            color=LogColor.CYAN,
        )

    def on_order_book_depth(self, depth: OrderBookDepth10) -> None:
        bids = self._ladder_levels(depth.bids)
        asks = self._ladder_levels(depth.asks)
        trading_date = self._event_trading_date(int(getattr(depth, "ts_event", 0) or 0)) or self._clock_date()
        # The depth book is captured for aggressive walk-book pricing only. Its
        # mid/best price is NOT a dependable reference for today's open or last
        # price (pre-open auction levels are provisional, snapshots are sparse), so
        # it must never feed _today_open / _last_close. Today's open comes from
        # real bars or the QMT full-tick snapshot.
        self._roll_trading_day(trading_date)
        self._depth_books[str(depth.instrument_id)] = (bids, asks)
        # On the first depth callback of each trading day, converge immediately so
        # orders are submitted as early as possible (priced against real depth)
        # rather than waiting for the next bar/timer. Gated on the trading window.
        if not self._depth_converged_today and self._within_trading_window(
            int(getattr(depth, "ts_event", 0) or 0),
        ):
            self._depth_converged_today = True
            self._converge_to_target(current_date=trading_date, trigger="order_book_depth")
        if not self._should_log_sample(self.config.order_book_depth_log_sample_rate):
            return
        self.log.info(
            "Order-book depth sample, "
            f"instrument_id={depth.instrument_id}, "
            f"bids={self._format_depth_side(depth.bids)}, "
            f"asks={self._format_depth_side(depth.asks)}, "
            f"ts_event={depth.ts_event}, ts_init={depth.ts_init}",
            color=LogColor.CYAN,
        )

    def _order_book_depth_logging_enabled(self) -> bool:
        return float(self.config.order_book_depth_log_sample_rate) > 0.0

    @staticmethod
    def _should_log_sample(sample_rate: float) -> bool:
        rate = max(0.0, min(1.0, float(sample_rate)))
        if rate <= 0.0:
            return False
        return rate >= 1.0 or random.random() < rate

    @staticmethod
    def _format_depth_side(orders: list[Any]) -> str:
        levels = []
        for order in orders[:10]:
            try:
                price = float(order.price)
                size = float(order.size)
            except Exception:
                continue
            if price <= 0 or size <= 0:
                continue
            levels.append(f"{order.price}@{order.size}")
        return "[" + ", ".join(levels) + "]"

    def on_order_filled(self, event: Any) -> None:
        client_order_id = str(event.client_order_id)
        instrument_id_text = str(getattr(event, "instrument_id", ""))
        self._forget_submitted_order(client_order_id)
        self._deferred_buys.pop(instrument_id_text, None)
        self._insufficient_funds.discard(instrument_id_text)
        # Reset the filled side's cancel count so the next convergence cycle for
        # this instrument starts pricing at the base offset again.
        if instrument_id_text:
            order_side = getattr(event, "order_side", None)
            if order_side == OrderSide.BUY:
                self._cancel_count_buy.pop(instrument_id_text, None)
            elif order_side == OrderSide.SELL:
                self._cancel_count_sell.pop(instrument_id_text, None)

    def on_order_canceled(self, event: Any) -> None:
        client_order_id = str(event.client_order_id)
        self._forget_submitted_order(client_order_id)
        self._converge_to_target(current_date=self._clock_date(), trigger="cancel")

    def on_order_denied(self, event: Any) -> None:
        self._handle_order_not_accepted(event)

    def on_order_rejected(self, event: Any) -> None:
        self._handle_order_not_accepted(event)

    def _handle_order_not_accepted(self, event: Any) -> None:
        client_order_id = str(event.client_order_id)
        instrument_id_text = str(getattr(event, "instrument_id", ""))
        reason = str(getattr(event, "reason", "") or "")
        self._forget_submitted_order(client_order_id)
        self._rejected_order_ids.add(client_order_id)
        if _is_insufficient_funds(reason):
            order = self.cache.order(event.client_order_id)
            order_qty = self._decimal_quantity(order.quantity)
            if instrument_id_text:
                self._insufficient_funds.add(instrument_id_text)
                if order_qty > 0:
                    self._deferred_buys[instrument_id_text] = order_qty
            self.log.warning(
                f"Order {client_order_id} {instrument_id_text} rejected for insufficient funds; "
                f"will retry after cash changes. reason={reason}",
            )
            return
        if _is_sellable_position_denial(reason):
            if instrument_id_text:
                self._sellable_exhausted[instrument_id_text] = self._clock_date()
            self.log.warning(
                f"Order {client_order_id} {instrument_id_text} rejected for sellable-position exhaustion; "
                f"will not retry this instrument today. reason={reason}",
            )
            return
        if instrument_id_text:
            self._deferred_buys.pop(instrument_id_text, None)

    def _on_converge_timer(self, _event: Any) -> None:
        try:
            self._converge_to_target(current_date=self._clock_date(), trigger="timer")
        except Exception as exc:
            self.log.warning(f"target convergence failed: {exc}")

    def _converge_to_target(self, current_date: date, trigger: str) -> None:
        if not self._within_trading_window():
            return
        acquired = self._converge_lock.acquire(blocking=False)
        if not acquired:
            return
        try:
            self._converge_to_target_locked(current_date=current_date, trigger=trigger)
        finally:
            self._converge_lock.release()

    def _converge_to_target_locked(self, current_date: date, trigger: str) -> None:
        if not self._target_version:
            self.log.warning("target convergence skipped: no target version set")
            return
        if self._target_version in self._achieved_versions:
            self.log.info(
                f"target convergence skipped: already achieved version={self._target_version}",
                color=LogColor.GREEN,
            )
            return
        self._sellable_exhausted = {
            instrument_id: exhausted_date
            for instrument_id, exhausted_date in self._sellable_exhausted.items()
            if exhausted_date == current_date
        }
        if self._stop_time_reached():
            self.log.info(
                f"target convergence stopped by stop_time={self.config.stop_time} "
                f"version={self._target_version}",
                color=LogColor.BLUE,
            )
            return
        self._log_account_sizing_snapshot(trigger)
        self._reconcile_unfilled_orders(current_date)
        open_order_instruments = self._open_order_instruments()
        desired = self._desired_quantities()
        sell_intents: dict[str, TargetOrderIntent] = {}
        buy_intents: dict[str, TargetOrderIntent] = {}
        # Per-instrument classification tally for the convergence summary log below.
        # Every desired instrument lands in exactly one action/skip bucket so we can
        # explain each cycle. Missing mandatory order inputs are logged as errors by
        # _target_order_intent and counted as intent_error here.
        skip_open_order: list[str] = []
        skip_frozen: list[str] = []
        skip_sellable_exhausted: list[str] = []
        skip_on_target: list[str] = []
        skip_intent_error: list[str] = []
        for instrument_id_text, target_qty in desired.items():
            if instrument_id_text in open_order_instruments:
                skip_open_order.append(instrument_id_text)
                continue
            if instrument_id_text in self._frozen_instruments:
                skip_frozen.append(instrument_id_text)
                continue
            intent = self._target_order_intent(instrument_id_text, target_qty)
            if intent is None:
                current_qty = self._current_quantity(InstrumentId.from_str(instrument_id_text))
                if current_qty == Decimal(str(target_qty)):
                    skip_on_target.append(instrument_id_text)
                else:
                    skip_intent_error.append(instrument_id_text)
                continue
            intent.context.record(
                convergence_trigger=trigger,
                target_version=self._target_version,
                trading_date=current_date,
            )
            limit_reason = self._price_limit_reason(instrument_id_text, intent.side)
            if limit_reason is not None and str(self.config.limit_stop_mode) == "freeze_symbol":
                intent.context.record(limit_reason=limit_reason, action="freeze_symbol")
                self._frozen_instruments[instrument_id_text] = limit_reason
                self._deferred_buys.pop(instrument_id_text, None)
                self._insufficient_funds.discard(instrument_id_text)
                skip_frozen.append(instrument_id_text)
                self.log.warning(
                    f"Freezing {instrument_id_text} for target version={self._target_version}: "
                    f"{limit_reason} intent_context={intent.context.log_text()}",
                )
                continue
            if intent.side == OrderSide.SELL:
                if self._sellable_exhausted.get(instrument_id_text) == current_date:
                    intent.context.record(action="skip", skip_reason="sellable_exhausted")
                    skip_sellable_exhausted.append(instrument_id_text)
                    continue
                intent.context.record(action="sell_target")
                sell_intents[instrument_id_text] = intent
            elif intent.side == OrderSide.BUY:
                intent.context.record(action="buy_target")
                buy_intents[instrument_id_text] = intent
            else:
                intent.context.record(action="skip", skip_reason="unsupported_side")
                skip_intent_error.append(instrument_id_text)

        self._log_convergence_summary(
            trigger=trigger,
            desired_count=len(desired),
            sell_intents=sell_intents,
            buy_intents=buy_intents,
            skip_open_order=skip_open_order,
            skip_frozen=skip_frozen,
            skip_sellable_exhausted=skip_sellable_exhausted,
            skip_on_target=skip_on_target,
            skip_intent_error=skip_intent_error,
        )

        for instrument_id_text, intent in sorted(sell_intents.items()):
            self._record_target(
                current_date,
                instrument_id_text,
                intent.context.target_qty,
                self._target_reason,
                intent.context,
            )
            self._submit_target_quantity(
                current_date,
                intent,
                self._target_reason,
            )
        if buy_intents:
            self._submit_buys_within_cash(current_date, buy_intents, self._target_reason)

        if self._target_achieved():
            self._achieved_versions.add(self._target_version)
            self.log.info(
                f"target achieved version={self._target_version} trigger={trigger} "
                f"count={len(self._target_quantities)}",
                color=LogColor.GREEN,
            )

    def _desired_quantities(self) -> dict[str, Decimal]:
        desired = dict(self._target_quantities)
        if not bool(self.config.exit_non_targets):
            return desired
        for instrument_id in self._held_instrument_ids():
            desired.setdefault(instrument_id, Decimal("0"))
        return desired

    def _held_instrument_ids(self) -> set[str]:
        result: set[str] = set()
        try:
            open_positions = self.cache.positions_open()
        except Exception:
            open_positions = []
        for position in open_positions:
            try:
                if not position.is_long:
                    continue
                instrument_id = position.instrument_id
            except Exception:
                continue
            if self._current_quantity(instrument_id) > 0:
                result.add(str(instrument_id))
        for instrument_id in self._instrument_ids:
            if self._current_quantity(instrument_id) > 0:
                result.add(str(instrument_id))
        return result

    def _log_convergence_summary(
        self,
        *,
        trigger: str,
        desired_count: int,
        sell_intents: dict[str, TargetOrderIntent],
        buy_intents: dict[str, TargetOrderIntent],
        skip_open_order: list[str],
        skip_frozen: list[str],
        skip_sellable_exhausted: list[str],
        skip_on_target: list[str],
        skip_intent_error: list[str],
    ) -> None:
        estimated_buy_cost = Decimal("0")
        unknown_buy_cost = 0
        for instrument_id_text, intent in buy_intents.items():
            cost = self._estimated_buy_cost(instrument_id_text, intent.context.target_qty)
            if cost is None:
                unknown_buy_cost += 1
                continue
            estimated_buy_cost += cost

        free_cash = self._free_cash()
        reserved_buy_cash = self._open_buy_order_notional()
        available_buy_cash: Decimal | None
        cash_gap: Decimal | None
        if free_cash is None:
            available_buy_cash = None
            cash_gap = None
        else:
            buffer_pct = max(0.0, float(self.config.cash_buffer_percent))
            available_buy_cash = free_cash * Decimal(str(1.0 - min(buffer_pct, 1.0)))
            if reserved_buy_cash > 0:
                available_buy_cash = max(Decimal("0"), available_buy_cash - reserved_buy_cash)
            cash_gap = max(Decimal("0"), estimated_buy_cost - available_buy_cash)

        self.log.info(
            "Target convergence summary "
            f"trigger={trigger} version={self._target_version} desired={desired_count} "
            f"sell_targets={len(sell_intents)} buy_targets={len(buy_intents)} "
            f"open_order={len(skip_open_order)} frozen={len(skip_frozen)} "
            f"sellable_exhausted={len(skip_sellable_exhausted)} "
            f"intent_error={len(skip_intent_error)} "
            f"on_target={len(skip_on_target)} "
            f"deferred_buys={len(self._deferred_buys)} "
            f"insufficient_funds={len(self._insufficient_funds)} "
            f"estimated_buy_cost={estimated_buy_cost} unknown_buy_cost={unknown_buy_cost} "
            f"free_cash={free_cash} reserved_buy_cash={reserved_buy_cash} "
            f"available_buy_cash={available_buy_cash} cash_gap={cash_gap} "
            f"intent_error_instruments={self._instrument_list_sample(skip_intent_error)} "
            f"open_order_instruments={self._instrument_list_sample(skip_open_order)} "
            f"frozen_instruments={self._instrument_list_sample(skip_frozen)} "
            f"sellable_exhausted_instruments={self._instrument_list_sample(skip_sellable_exhausted)}",
            color=LogColor.BLUE,
        )

    @staticmethod
    def _instrument_list_sample(instruments: list[str], limit: int = 8) -> str:
        if not instruments:
            return "[]"
        ordered = sorted(instruments)
        shown = ", ".join(ordered[:limit])
        remaining = len(ordered) - limit
        if remaining > 0:
            shown = f"{shown}, ...(+{remaining})"
        return f"[{shown}]"

    def _price_limit_reason(self, instrument_id_text: str, side: OrderSide) -> str | None:
        price = self._last_close.get(instrument_id_text)
        if price is None or price <= 0:
            return None
        up_limit, down_limit = self._price_limits(instrument_id_text)
        if side == OrderSide.BUY and up_limit is not None and price >= up_limit:
            return "up_limit"
        if side == OrderSide.SELL and down_limit is not None and price <= down_limit:
            return "down_limit"
        return None

    def _target_achieved(self) -> bool:
        if self._open_order_instruments():
            return False
        if self._deferred_buys or self._insufficient_funds:
            return False
        desired = self._desired_quantities()
        for instrument_id_text, target_qty in desired.items():
            if instrument_id_text in self._frozen_instruments:
                continue
            current_qty = self._current_quantity(InstrumentId.from_str(instrument_id_text))
            if current_qty != Decimal(str(target_qty)):
                return False
        return True

    def _reconcile_unfilled_orders(self, trading_date: date) -> None:
        timeout_secs = float(self.config.unfilled_timeout_secs)
        if timeout_secs <= 0:
            return
        now = self.clock.timestamp_ns()
        timeout_ns = int(timeout_secs * 1_000_000_000)
        try:
            open_orders = self.cache.orders_open(strategy_id=self.id)
        except Exception:
            open_orders = []
        for order in open_orders:
            client_order_id = str(order.client_order_id)
            instrument_id_text = str(order.instrument_id)
            if client_order_id in self._rejected_order_ids:
                continue
            try:
                if order.is_pending_cancel:
                    continue
            except Exception:
                pass
            submit_ts = int(order.ts_last)
            if now - submit_ts < timeout_ns:
                continue
            if self._at_price_limit(order):
                continue
            # A buy already resting at its max-buy-price cap is left to fill if the
            # market comes back down; do not cancel it (so its cancel count never
            # grows and the price never escalates past the cap).
            if self._at_buy_price_cap(order):
                continue
            try:
                self.cancel_order(order)
            except Exception as exc:
                self.log.warning(f"cancel_order failed for {client_order_id}: {exc}")
                continue
            intent_context = self._order_intent_contexts.get(client_order_id)
            if intent_context is None:
                intent_context = self._intent_context_from_order(
                    order=order,
                    target_qty=self._target_quantities.get(instrument_id_text, Decimal("0")),
                )
            intent_context.record(
                cancel_reason="unfilled_timeout",
                canceled_order_id=client_order_id,
            )
            self._forget_submitted_order(client_order_id)
            if order.side == OrderSide.BUY:
                self._cancel_count_buy[instrument_id_text] = (
                    self._cancel_count_buy.get(instrument_id_text, 0) + 1
                )
            else:
                self._cancel_count_sell[instrument_id_text] = (
                    self._cancel_count_sell.get(instrument_id_text, 0) + 1
                )
            self._record_order(
                trading_date=trading_date,
                instrument_id=instrument_id_text,
                side="buy" if order.side == OrderSide.BUY else "sell",
                quantity=0,
                target_qty=self._target_quantities.get(instrument_id_text, Decimal("0")),
                status="canceled",
                reason="unfilled_timeout",
                order_id=client_order_id,
                intent_context=intent_context,
            )

    def _submit_buys_within_cash(
        self,
        trading_date: date,
        buy_intents: dict[str, TargetOrderIntent],
        reason: str,
    ) -> None:
        if not buy_intents:
            return
        free_cash = self._free_cash()
        if free_cash is None:
            for instrument_id, intent in buy_intents.items():
                target_qty = intent.context.target_qty
                intent.context.record(
                    cash_gate="missing_free_cash",
                    trading_date=trading_date,
                )
                self._deferred_buys[instrument_id] = target_qty
                self._record_order(
                    trading_date,
                    instrument_id,
                    "buy",
                    0,
                    target_qty,
                    "deferred",
                    "missing_free_cash",
                    intent_context=intent.context,
                )
            return
        original_free_cash = free_cash
        buffer_pct = max(0.0, float(self.config.cash_buffer_percent))
        if buffer_pct > 0:
            free_cash = free_cash * Decimal(str(1.0 - min(buffer_pct, 1.0)))
        reserved_buy_cash = self._open_buy_order_notional()
        if reserved_buy_cash > 0:
            free_cash = max(Decimal("0"), free_cash - reserved_buy_cash)
        ordered_candidates = sorted(
            buy_intents,
            key=lambda i: buy_intents[i].context.target_qty,
            reverse=True,
        )
        for candidate_index, instrument_id in enumerate(ordered_candidates):
            intent = buy_intents[instrument_id]
            target_qty = intent.context.target_qty
            intent.context.record(
                cash_gate="evaluating",
                cash_gate_index=candidate_index,
                free_cash_initial=original_free_cash,
                cash_buffer_percent=buffer_pct,
                reserved_buy_cash=reserved_buy_cash,
                available_cash_before_candidate=free_cash,
                trading_date=trading_date,
            )
            if instrument_id in self._insufficient_funds:
                intent.context.record(
                    cash_gate="deferred",
                    defer_reason="insufficient_funds_backoff",
                )
                self._deferred_buys[instrument_id] = target_qty
                continue
            if intent.side != OrderSide.BUY:
                continue
            slices = self._order_slices(intent)
            intent.context.record(slice_count=len(slices))
            if not slices:
                intent.context.record(cash_gate="skipped", skip_reason="no_order_slices")
                continue
            submitted_any = False
            for slice_index, quantity in enumerate(slices):
                est_cost = self._estimated_order_cost(quantity, intent.price)
                intent.context.record(
                    slice_index=slice_index,
                    slice_quantity=quantity,
                    slice_estimated_cost=est_cost,
                    available_cash_before_slice=free_cash,
                )
                if est_cost > free_cash:
                    self._deferred_buys[instrument_id] = target_qty
                    intent.context.record(
                        cash_gate="deferred",
                        defer_reason="insufficient_cash",
                        available_cash_at_defer=free_cash,
                        deferred_slice_index=slice_index,
                    )
                    self._record_order(
                        trading_date,
                        instrument_id,
                        "buy",
                        0,
                        target_qty,
                        "deferred",
                        "insufficient_cash",
                        intent_context=intent.context,
                    )
                    break
                if not submitted_any:
                    self._record_target(trading_date, instrument_id, target_qty, reason, intent.context)
                self._submit_order_quantity(
                    trading_date=trading_date,
                    intent=intent,
                    quantity=quantity,
                    reason=reason,
                    slice_index=slice_index,
                    slice_count=len(slices),
                )
                submitted_any = True
                free_cash -= est_cost
                intent.context.record(
                    cash_gate="submitted",
                    available_cash_after_slice=free_cash,
                )
            if not submitted_any and instrument_id not in self._deferred_buys:
                self._deferred_buys[instrument_id] = target_qty
                intent.context.record(cash_gate="deferred", defer_reason="no_slice_submitted")

    def _target_order_intent(self, instrument_id_text: str, target_qty: Decimal) -> TargetOrderIntent | None:
        instrument_id = InstrumentId.from_str(instrument_id_text)
        current_qty = self._current_quantity(instrument_id)
        target_qty = Decimal(str(target_qty))
        delta_qty = target_qty - current_qty
        if target_qty <= 0:
            if current_qty <= 0:
                return None
            quantity = abs(current_qty)
            side = OrderSide.SELL
        else:
            if delta_qty == 0:
                return None
            side = OrderSide.BUY if delta_qty > 0 else OrderSide.SELL
            quantity = abs(delta_qty)

        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self.log.error(
                f"Cannot create target order intent for {instrument_id_text}: missing instrument",
            )
            return None

        price_context = self._build_price_context(instrument, instrument_id, side, quantity)
        pricer = self._pricer_for_side(side)
        raw_price = pricer.compute(price_context)
        price = None if raw_price is None or raw_price <= 0 else instrument.make_price(raw_price)
        context = IntentContext(
            instrument_id=instrument_id_text,
            target_qty=target_qty,
            current_qty=current_qty,
            delta_qty=delta_qty,
            price_context=price_context,
            pricer_class=pricer.__class__.__name__,
        )
        context.record(
            side=side.name,
            intent_quantity=quantity,
            limit_price=price,
            target_version=self._target_version,
        )
        if price is None:
            self.log.error(
                f"Cannot create target order intent for {instrument_id_text}: missing limit price "
                f"side={side.name} quantity={quantity} target_qty={target_qty} current_qty={current_qty} "
                f"intent_context={context.log_text()}",
            )
            return None
        return TargetOrderIntent(instrument_id, instrument, side, quantity, price, context)

    def _order_slices(self, intent: TargetOrderIntent) -> list[Decimal]:
        return self._order_splitter.split(
            instrument=intent.instrument,
            quantity=Decimal(str(intent.quantity)),
            price=Decimal(str(intent.price)),
        )

    def _intent_context_from_order(self, order: Any, target_qty: Decimal) -> IntentContext:
        instrument_id = getattr(order, "instrument_id")
        instrument_id_text = str(instrument_id)
        current_qty = self._current_quantity(instrument_id)
        target_qty = Decimal(str(target_qty))
        side = getattr(order, "side", OrderSide.NO_ORDER_SIDE)
        raw_quantity = getattr(order, "quantity", Decimal("0"))
        quantity = self._decimal_quantity(raw_quantity)
        instrument = self.cache.instrument(instrument_id)
        price_context = None
        pricer_class = None
        if instrument is not None and side in {OrderSide.BUY, OrderSide.SELL}:
            price_context = self._build_price_context(instrument, instrument_id, side, quantity)
            pricer_class = self._pricer_for_side(side).__class__.__name__
        context = IntentContext(
            instrument_id=instrument_id_text,
            target_qty=target_qty,
            current_qty=current_qty,
            delta_qty=target_qty - current_qty,
            price_context=price_context,
            pricer_class=pricer_class,
        )
        context.record(
            side=getattr(side, "name", str(side)),
            intent_quantity=quantity,
            limit_price=getattr(order, "price", None),
            target_version=self._target_version,
            context_source="order_record",
            order_id=str(getattr(order, "client_order_id", "")),
        )
        return context

    @staticmethod
    def _estimated_order_cost(quantity: Decimal, price: Any) -> Decimal:
        return Decimal(str(quantity)) * Decimal(str(price))

    def _estimated_buy_cost(self, instrument_id_text: str, target_qty: Decimal) -> Decimal | None:
        instrument_id = InstrumentId.from_str(instrument_id_text)
        instrument = self.cache.instrument(instrument_id)
        open_price = self._today_open.get(instrument_id_text)
        if instrument is None or open_price is None or open_price <= 0:
            return None
        current_qty = self._current_quantity(instrument_id)
        delta_qty = Decimal(str(target_qty)) - current_qty
        if delta_qty <= 0:
            return Decimal("0")
        return Decimal(str(delta_qty)) * Decimal(str(open_price))

    def _submit_target_quantity(
        self,
        trading_date: date,
        intent: TargetOrderIntent,
        reason: str,
    ) -> bool:
        instrument_id_text = str(intent.instrument_id)
        target_qty = intent.context.target_qty
        if self.cache.instrument(intent.instrument_id) is None:
            self.log.error(f"Cannot submit target quantity for {instrument_id_text}: missing instrument")
            self._record_order(
                trading_date,
                instrument_id_text,
                "buy",
                0,
                target_qty,
                "rejected",
                "missing_instrument",
                intent_context=intent.context,
            )
            return False
        slices = self._order_slices(intent)
        intent.context.record(
            submit_path="target_quantity",
            trading_date=trading_date,
            slice_count=len(slices),
        )
        for slice_index, quantity in enumerate(slices):
            self._submit_order_quantity(
                trading_date=trading_date,
                intent=intent,
                quantity=quantity,
                reason=reason,
                slice_index=slice_index,
                slice_count=len(slices),
            )
        return True

    def _submit_order_quantity(
        self,
        trading_date: date,
        intent: TargetOrderIntent,
        quantity: Decimal,
        reason: str,
        slice_index: int | None = None,
        slice_count: int | None = None,
    ) -> bool:
        target_qty = intent.context.target_qty
        intent.context.record(
            submit_path="order_quantity",
            trading_date=trading_date,
            requested_quantity=quantity,
            slice_index=slice_index,
            slice_count=slice_count,
        )
        if intent.side == OrderSide.SELL:
            clamped_quantity = self._clamp_sell_quantity(
                trading_date=trading_date,
                instrument_id=intent.instrument_id,
                requested_qty=quantity,
                reason=reason,
                intent_context=intent.context,
            )
            intent.context.record(clamped_quantity=clamped_quantity)
            if clamped_quantity is None or clamped_quantity <= 0:
                return False
            quantity = clamped_quantity
        else:
            intent.context.record(clamped_quantity=quantity)
        if intent.price is None:
            self.log.error(
                f"Cannot submit order for {intent.instrument_id}: missing limit price "
                f"side={intent.side.name} quantity={quantity} intent_context={intent.context.log_text()}",
            )
            return False
        intent.context.record(submitted_quantity=quantity, limit_price=intent.price)
        order = self.order_factory.limit(
            instrument_id=intent.instrument_id,
            order_side=intent.side,
            quantity=intent.instrument.make_qty(quantity),
            price=intent.price,
        )
        intent.context.record(order_id=str(order.client_order_id))
        self._track_submitted_order(order, intent.context)
        try:
            self.submit_order(order)
        except Exception:
            self._forget_submitted_order(str(order.client_order_id))
            raise
        side_text = "buy" if intent.side == OrderSide.BUY else "sell"
        self._record_order(
            trading_date,
            str(intent.instrument_id),
            side_text,
            int(quantity),
            target_qty,
            "submitted",
            reason,
            intent.context,
            str(order.client_order_id),
        )
        return True

    def _clamp_sell_quantity(
        self,
        trading_date: date,
        instrument_id: InstrumentId,
        requested_qty: Decimal,
        reason: str,
        intent_context: IntentContext,
    ) -> Decimal | None:
        instrument_id_text = str(instrument_id)
        target_qty = intent_context.target_qty
        if requested_qty <= 0:
            return Decimal("0")

        fills_before = self._today_fill_snapshot(instrument_id, trading_date)
        current_qty = self._current_quantity(instrument_id)
        open_sell_qty = self._open_sell_quantity(instrument_id)
        fills_after = self._today_fill_snapshot(instrument_id, trading_date)
        if fills_before != fills_after:
            intent_context.record(
                sell_clamp_status="deferred",
                sell_clamp_reason="sellable_snapshot_unstable",
                fills_before=fills_before,
                fills_after=fills_after,
            )
            self._record_order(
                trading_date,
                instrument_id_text,
                "sell",
                0,
                target_qty,
                "deferred",
                "sellable_snapshot_unstable",
                intent_context=intent_context,
            )
            self.log.warning(
                f"Deferring SELL for {instrument_id_text}: Nautilus fill snapshot changed while "
                f"calculating sellable quantity; before={fills_before}, after={fills_after} "
                f"intent_context={intent_context.log_text()}",
            )
            return None

        # Prefer the broker-reported sellable quantity (QMT `can_use_volume`) when available:
        # it already excludes today's buys / frozen / in-transit shares and is immune to the
        # reconciliation quirk where restart-rebuilt holdings get stamped as "today's buys"
        # (which would zero out the fill-based estimate). Fall back to the fill-based estimate
        # in backtest or when no venue report has arrived yet. In both cases still subtract this
        # strategy's own open sells so we do not double-count in-flight sell orders.
        venue_can_use = self._venue_sellable.get(instrument_id_text)
        if venue_can_use is not None:
            sellable_base = venue_can_use
            sellable_source = "broker_can_use_volume"
        else:
            sellable_base = current_qty - fills_after.buy_qty
            sellable_source = "fill_estimate"
        # Only latch `_sellable_exhausted` (which blocks retries for the rest of the day) when
        # the broker figure is authoritative. When we fell back to the fill estimate because no
        # venue report has arrived yet, do NOT latch: the estimate is unreliable across restarts
        # (reconstructed holdings look like today's buys), and the broker map is populated
        # asynchronously by the on-start / refresh reconcile. Latching here would permanently
        # skip a genuinely-sellable position for the day if the first attempt races the reconcile.
        broker_authoritative = venue_can_use is not None
        sellable_qty = max(Decimal("0"), sellable_base - open_sell_qty)
        intent_context.record(
            sellable_source=sellable_source,
            broker_authoritative=broker_authoritative,
            sellable_base=sellable_base,
            sellable_qty=sellable_qty,
            venue_can_use=venue_can_use,
            open_sell_qty=open_sell_qty,
            today_buy_qty=fills_after.buy_qty,
        )
        if sellable_qty <= 0:
            if broker_authoritative:
                self._sellable_exhausted[instrument_id_text] = trading_date
            intent_context.record(
                sell_clamp_status="deferred",
                sell_clamp_reason=(
                    "sellable_exhausted"
                    if broker_authoritative
                    else "sellable_pending_broker_data"
                ),
                clamped_quantity=Decimal("0"),
            )
            self._record_order(
                trading_date,
                instrument_id_text,
                "sell",
                0,
                target_qty,
                "deferred",
                "sellable_exhausted" if broker_authoritative else "sellable_pending_broker_data",
                intent_context=intent_context,
            )
            self.log.warning(
                f"Skipping SELL for {instrument_id_text}: sellable exhausted "
                f"(source={sellable_source}, latched={broker_authoritative}, net_qty={current_qty}, "
                f"today_buy_qty={fills_after.buy_qty}, venue_can_use={venue_can_use}, "
                f"open_sell_qty={open_sell_qty}, requested_qty={requested_qty}) "
                f"intent_context={intent_context.log_text()}",
            )
            return Decimal("0")

        clamped_qty = min(Decimal(str(requested_qty)), sellable_qty)
        intent_context.record(
            sell_clamp_status="clamped" if clamped_qty < requested_qty else "unchanged",
            clamped_quantity=clamped_qty,
        )
        if clamped_qty < requested_qty:
            if broker_authoritative:
                self._sellable_exhausted[instrument_id_text] = trading_date
            self.log.warning(
                f"Clamping SELL for {instrument_id_text}: requested_qty={requested_qty}, "
                f"sellable_qty={sellable_qty}, source={sellable_source}, "
                f"today_buy_qty={fills_after.buy_qty}, venue_can_use={venue_can_use}, "
                f"open_sell_qty={open_sell_qty} "
                f"intent_context={intent_context.log_text()}",
            )
        return clamped_qty

    def _subscribe_execution_mass_status(self) -> None:
        """
        Subscribe to execution mass-status reports so the broker-reported sellable
        quantity (``can_use_volume``) can be cached per instrument. Inert in backtest,
        where there is no execution client publishing these reports.
        """
        venue = self._instrument_ids[0].venue if self._instrument_ids else None
        if venue is None:
            return
        msgbus = getattr(self, "msgbus", None)
        if msgbus is None:
            return
        try:
            msgbus.subscribe(
                topic=f"reports.execution.{venue}",
                handler=self._on_execution_mass_status,
            )
        except Exception as exc:  # pragma: no cover - defensive; backtest has no msgbus reports
            self.log.warning(f"Could not subscribe to execution mass status: {exc}")

    def _on_execution_mass_status(self, mass_status: Any) -> None:
        """
        Rebuild the per-instrument broker sellable map from a mass-status report.

        Replaces the map wholesale each cycle so instruments no longer held drop out.
        Only entries where the venue actually reported ``can_use_volume`` are kept.
        """
        position_reports = getattr(mass_status, "position_reports", None)
        if not position_reports:
            return
        sellable: dict[str, Decimal] = {}
        for reports in position_reports.values():
            report_list = reports if isinstance(reports, (list, tuple)) else [reports]
            for report in report_list:
                can_use = getattr(report, "can_use_volume", None)
                if can_use is None:
                    continue
                instrument_id = getattr(report, "instrument_id", None)
                if instrument_id is None:
                    continue
                try:
                    sellable[str(instrument_id)] = Decimal(str(can_use))
                except Exception:
                    continue
        self._venue_sellable = sellable
        self._venue_sellable_ts = self.clock.timestamp_ns()
        self.log.info(
            f"Updated broker sellable map from mass status: instruments={len(sellable)}",
            color=LogColor.BLUE,
        )

    def _today_fill_snapshot(self, instrument_id: InstrumentId, trading_date: date) -> TodayFillSnapshot:
        buy_qty = Decimal("0")
        sell_qty = Decimal("0")
        fill_count = 0
        latest_ts_event = 0
        try:
            orders = self.cache.orders(instrument_id=instrument_id)
        except Exception:
            orders = []
        for order in orders:
            if self._is_reconciliation_order(order):
                continue
            for event in getattr(order, "events", []) or []:
                if not isinstance(event, OrderFilled) and not (
                    hasattr(event, "order_side") and hasattr(event, "last_qty")
                ):
                    continue
                if getattr(event, "instrument_id", instrument_id) != instrument_id:
                    continue
                ts_event = int(getattr(event, "ts_event", 0) or 0)
                if self._event_trading_date(ts_event) != trading_date:
                    continue
                order_side = getattr(event, "order_side", None)
                last_qty = self._decimal_quantity(getattr(event, "last_qty", Decimal("0")))
                if order_side == OrderSide.BUY:
                    buy_qty += last_qty
                elif order_side == OrderSide.SELL:
                    sell_qty += last_qty
                else:
                    continue
                fill_count += 1
                latest_ts_event = max(latest_ts_event, ts_event)
        return TodayFillSnapshot(
            buy_qty=buy_qty,
            sell_qty=sell_qty,
            fill_count=fill_count,
            latest_ts_event=latest_ts_event,
        )

    def _event_trading_date(self, ts_event: int) -> date | None:
        if ts_event <= 0:
            return None
        try:
            return pd.Timestamp(ts_event, unit="ns", tz="UTC").tz_convert(self.config.timezone_name).date()
        except Exception:
            return None

    @staticmethod
    def _is_reconciliation_order(order: Any) -> bool:
        tags = getattr(order, "tags", []) or []
        return "RECONCILIATION" in {str(tag) for tag in tags}

    def _open_sell_quantity(self, instrument_id: InstrumentId) -> Decimal:
        total = Decimal("0")
        for order in self._active_orders(instrument_id=instrument_id, side=OrderSide.SELL):
            try:
                if order.is_pending_cancel:
                    continue
            except Exception:
                pass
            quantity = getattr(order, "leaves_qty", getattr(order, "quantity", Decimal("0")))
            total += self._decimal_quantity(quantity)
        return total

    def _open_buy_order_notional(self) -> Decimal:
        total = Decimal("0")
        for order in self._active_orders(side=OrderSide.BUY):
            try:
                if order.is_pending_cancel:
                    continue
            except Exception:
                pass
            price = getattr(order, "price", None)
            if price is None:
                continue
            quantity = getattr(order, "leaves_qty", getattr(order, "quantity", Decimal("0")))
            total += self._estimated_order_cost(self._decimal_quantity(quantity), price)
        return total

    @staticmethod
    def _decimal_quantity(quantity: Any) -> Decimal:
        if quantity is None:
            return Decimal("0")
        try:
            return Decimal(str(quantity.as_decimal()))
        except Exception:
            return Decimal(str(quantity))

    def _track_submitted_order(self, order: Any, intent_context: IntentContext) -> None:
        client_order_id = str(order.client_order_id)
        self._order_target_versions[client_order_id] = self._target_version
        self._order_intent_contexts[client_order_id] = intent_context

    def _forget_submitted_order(self, client_order_id: str) -> None:
        self._order_target_versions.pop(client_order_id, None)
        self._order_intent_contexts.pop(client_order_id, None)

    def _active_orders(
        self,
        instrument_id: InstrumentId | None = None,
        side: OrderSide = OrderSide.NO_ORDER_SIDE,
    ) -> list[Any]:
        orders_by_id: dict[str, Any] = {}
        query_kwargs = {"strategy_id": self.id, "side": side}
        if instrument_id is not None:
            query_kwargs["instrument_id"] = instrument_id
        for method_name in ("orders", "orders_open", "orders_inflight"):
            method = getattr(self.cache, method_name, None)
            if method is None:
                continue
            try:
                orders = method(**query_kwargs)
            except Exception:
                continue
            for order in orders:
                if not self._is_active_order(order):
                    continue
                orders_by_id[str(order.client_order_id)] = order
        return list(orders_by_id.values())

    def _is_active_order(self, order: Any) -> bool:
        status = getattr(order, "status", None)
        if status is not None:
            return status not in self._TERMINAL_ORDER_STATUSES
        try:
            if order.is_closed:
                return False
        except Exception:
            pass
        return True

    def _pricer_for_side(self, side: OrderSide) -> Any:
        return self._buy_pricer if side == OrderSide.BUY else self._sell_pricer

    def _limit_price(
        self,
        instrument: Any,
        instrument_id: InstrumentId,
        side: OrderSide,
        quantity: Decimal | None = None,
    ) -> Any | None:
        ctx = self._build_price_context(instrument, instrument_id, side, quantity)
        pricer = self._pricer_for_side(side)
        raw = pricer.compute(ctx)
        if raw is None or raw <= 0:
            return None
        return instrument.make_price(raw)

    def _build_price_context(
        self,
        instrument: Any,
        instrument_id: InstrumentId,
        side: OrderSide,
        quantity: Decimal | None,
    ) -> PriceContext:
        instrument_id_text = str(instrument_id)
        tick = float(instrument.price_increment)
        best_bid, best_ask, bids, asks = self._book_snapshot(instrument_id)
        if side == OrderSide.BUY:
            cancel_count = self._cancel_count_buy.get(instrument_id_text, 0)
        else:
            cancel_count = self._cancel_count_sell.get(instrument_id_text, 0)
        return PriceContext(
            instrument_id=instrument_id,
            side=side,
            open_price=self._today_open.get(instrument_id_text),
            last_close=self._last_close.get(instrument_id_text),
            tick=tick,
            quantity=Decimal(str(quantity)) if quantity is not None else Decimal("0"),
            cancel_count=cancel_count,
            best_bid=best_bid,
            best_ask=best_ask,
            bids=bids,
            asks=asks,
        )

    def _quote_snapshot(
        self,
        instrument_id: InstrumentId,
    ) -> tuple[float | None, float | None]:
        best_bid: float | None = None
        best_ask: float | None = None
        try:
            quote = self.cache.quote_tick(instrument_id)
        except Exception:
            quote = None
        if quote is None:
            return best_bid, best_ask
        try:
            bid_price = float(quote.bid_price)
            if bid_price > 0:
                best_bid = bid_price
        except Exception:
            pass
        try:
            ask_price = float(quote.ask_price)
            if ask_price > 0:
                best_ask = ask_price
        except Exception:
            pass
        return best_bid, best_ask

    def _book_snapshot(
        self,
        instrument_id: InstrumentId,
    ) -> tuple[float | None, float | None, list[tuple[float, float]], list[tuple[float, float]]]:
        """
        Read the latest depth event captured by on_order_book_depth. This avoids
        reaching through adapter internals while still letting the price strategy
        use live depth when the strategy subscribed to it.
        """
        bids, asks = self._depth_books.get(str(instrument_id), ([], []))
        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        if best_bid is None and best_ask is None:
            best_bid, best_ask = self._quote_snapshot(instrument_id)
            bids = [(best_bid, 0.0)] if best_bid is not None else []
            asks = [(best_ask, 0.0)] if best_ask is not None else []
        return best_bid, best_ask, bids, asks

    @staticmethod
    def _ladder_levels(levels: Any) -> list[tuple[float, float]]:
        result: list[tuple[float, float]] = []
        for level in levels or []:
            try:
                price = float(level.price)
                size_attr = getattr(level, "size")
                size = float(size_attr() if callable(size_attr) else size_attr)
            except Exception:
                continue
            if price > 0 and size > 0:
                result.append((price, size))
        return result

    def _at_buy_price_cap(self, order: Any) -> bool:
        """
        True when a BUY order is resting at (or above) the buy strategy's max-buy
        price cap. Such orders are not cancelled by the reconciler.
        """
        if order.side != OrderSide.BUY:
            return False
        order_price = getattr(order, "price", None)
        if order_price is None:
            return False
        instrument_id = getattr(order, "instrument_id", None)
        if instrument_id is None:
            return False
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            return False
        ctx = self._build_price_context(instrument, instrument_id, OrderSide.BUY, None)
        cap = self._buy_pricer.max_buy_price(ctx)
        if cap is None:
            return False
        try:
            return float(order_price) >= cap - float(instrument.price_increment) / 2.0
        except Exception:
            return False

    def _target_quantity(self, instrument_id_text: str) -> Decimal:
        """The committed target share count for an instrument (0 when none set)."""
        return self._target_quantities.get(instrument_id_text, Decimal("0"))

    def investable_total_asset(self) -> Decimal:
        """
        Pre-market investable total: current total asset net of the trading cash
        buffer. This is the sizing basis handed to the target planner (so the
        server-side share counts leave room for the buffer) and the value persisted as
        ``investable_asset`` alongside the raw ``total_asset``.
        """
        total = self._portfolio_value()
        buffer_pct = Decimal(str(self.config.target_cash_buffer_percent))
        investable = total * (Decimal("1") - buffer_pct)
        return investable if investable > 0 else Decimal("0")

    def _portfolio_value(self) -> Decimal:
        broker_total_asset = self._broker_account_decimal("total_asset")
        if broker_total_asset is not None and broker_total_asset > 0:
            return broker_total_asset
        nautilus_equity = self._nautilus_portfolio_equity()
        if nautilus_equity is not None:
            return nautilus_equity
        return Decimal(str(self.config.initial_cash))

    def _nautilus_portfolio_equity(self) -> Decimal | None:
        venue = self._instrument_ids[0].venue if self._instrument_ids else None
        try:
            equity = self.portfolio.equity(venue=Venue(str(venue))) if venue is not None else self.portfolio.equity()
        except Exception:
            equity = {}
        if not equity:
            return None
        first = next(iter(equity.values()))
        try:
            return Decimal(str(first.as_decimal()))
        except Exception:
            return Decimal(str(float(first)))

    def _free_cash(self) -> Decimal | None:
        broker_free_cash = self._broker_account_decimal("available_cash", "cash")
        if broker_free_cash is not None:
            return broker_free_cash
        return self._nautilus_free_cash()

    def _nautilus_free_cash(self) -> Decimal | None:
        venue = self._instrument_ids[0].venue if self._instrument_ids else None
        try:
            account = (
                self.portfolio.account(venue=Venue(str(venue)))
                if venue is not None
                else self.portfolio.account()
            )
        except Exception:
            account = None
        if account is not None:
            try:
                free = account.balance_free()
            except Exception:
                free = None
            if free is not None:
                try:
                    return Decimal(str(free.as_decimal()))
                except Exception:
                    return Decimal(str(float(free)))
        if bool(self.config.require_account_cash):
            return None
        return self._portfolio_value()

    def _log_account_sizing_snapshot(self, trigger: str) -> None:
        broker_total_asset = self._broker_account_decimal("total_asset")
        broker_cash = self._broker_account_decimal("available_cash", "cash")
        broker_market_value = self._broker_account_decimal("market_value")
        broker_fetch_balance = self._broker_account_decimal("fetch_balance")
        nautilus_equity = self._nautilus_portfolio_equity()
        nautilus_free_cash = self._nautilus_free_cash()
        selected_portfolio_value = self._portfolio_value()
        selected_free_cash = self._free_cash()
        if broker_total_asset is not None and broker_total_asset > 0:
            value_source = "account_state_info.total_asset"
        elif nautilus_equity is not None:
            value_source = "portfolio.equity"
        else:
            value_source = "config.initial_cash"
        cash_source = (
            "account_state_info.cash"
            if broker_cash is not None
            else "portfolio.account.balance_free"
        )
        snapshot = (
            f"value_source={value_source} selected_portfolio_value={selected_portfolio_value} "
            f"nautilus_portfolio_equity={nautilus_equity} "
            f"broker_total_asset={broker_total_asset} broker_market_value={broker_market_value} "
            f"cash_source={cash_source} selected_free_cash={selected_free_cash} "
            f"nautilus_free_cash={nautilus_free_cash} broker_cash={broker_cash} "
            f"broker_fetch_balance={broker_fetch_balance}"
        )
        if snapshot == getattr(self, "_last_account_sizing_snapshot", None):
            return
        self._last_account_sizing_snapshot = snapshot
        self.log.info(
            f"Account sizing snapshot trigger={trigger}: {snapshot}",
            color=LogColor.BLUE,
        )

    def _broker_account_decimal(self, *keys: str) -> Decimal | None:
        for account in self._broker_accounts():
            info = self._account_info(account)
            for key in keys:
                value = info.get(key)
                if value is None:
                    continue
                try:
                    result = Decimal(str(value))
                except Exception:
                    continue
                if result >= 0:
                    return result
        return None

    def _broker_accounts(self) -> list[Any]:
        accounts: list[Any] = []
        cache_accounts = getattr(getattr(self, "cache", None), "accounts", None)
        if cache_accounts is not None:
            try:
                accounts.extend(cache_accounts() or [])
            except Exception:
                pass
        venue = self._instrument_ids[0].venue if self._instrument_ids else None
        try:
            account = (
                self.portfolio.account(venue=Venue(str(venue)))
                if venue is not None
                else self.portfolio.account()
            )
        except Exception:
            account = None
        if account is not None:
            accounts.append(account)

        unique: list[Any] = []
        seen: set[int] = set()
        for account in accounts:
            marker = id(account)
            if marker in seen:
                continue
            seen.add(marker)
            unique.append(account)
        return unique

    @staticmethod
    def _account_info(account: Any) -> dict[str, Any]:
        try:
            event = account.last_event
        except Exception:
            return {}
        info = getattr(event, "info", None) if event is not None else None
        return info if isinstance(info, dict) else {}

    def _current_quantity(self, instrument_id: InstrumentId) -> Decimal:
        try:
            qty = self.portfolio.net_position(instrument_id)
        except Exception:
            return Decimal("0")
        if qty is None:
            return Decimal("0")
        return Decimal(str(qty))

    def _current_weight(self, instrument_id_text: str) -> float | None:
        open_price = self._today_open.get(instrument_id_text)
        if open_price is None or open_price <= 0:
            return None
        portfolio_value = self._portfolio_value()
        if portfolio_value <= 0:
            return None
        qty = self._current_quantity(InstrumentId.from_str(instrument_id_text))
        return float(qty * Decimal(str(open_price)) / portfolio_value)

    def _open_order_instruments(self) -> set[str]:
        return {str(order.instrument_id) for order in self._active_orders()}

    def _price_limits(self, instrument_id_text: str) -> tuple[float | None, float | None]:
        try:
            instrument = self.cache.instrument(InstrumentId.from_str(instrument_id_text))
        except Exception:
            return (None, None)
        info = getattr(instrument, "info", None)
        if not isinstance(info, dict):
            return (None, None)
        fields = info.get("fields")
        if not isinstance(fields, dict):
            fields = info

        def _read(keys: tuple[str, ...]) -> float | None:
            for key in keys:
                if key not in fields:
                    continue
                try:
                    value = float(fields[key])
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    return value
            return None

        return (_read(self._UP_LIMIT_KEYS), _read(self._DOWN_LIMIT_KEYS))

    def _at_price_limit(self, order: Any) -> bool:
        return self._price_limit_reason(str(order.instrument_id), order.side) is not None

    def _note_quote_tick(self, tick: QuoteTick) -> None:
        """Record the latest live quote exchange timestamp for the trading gate."""
        ts_event = int(tick.ts_event)
        if ts_event <= 0:
            raise ValueError(f"Quote tick ts_event must be positive, got {ts_event}")
        self._last_quote_tick_ts_event = ts_event

    def _within_pre_open_quote_log_window(self) -> bool:
        try:
            now = pd.Timestamp(self.clock.utc_now()).tz_convert(self.config.timezone_name).time()
            start = pd.Timestamp("09:20").time()
            end = pd.Timestamp("09:30").time()
        except Exception:
            return False
        return start <= now < end

    @staticmethod
    def _time_window_index(current: Any, windows: str) -> int | None:
        for index, session in enumerate(str(windows).split(",")):
            session = session.strip()
            if not session or "-" not in session:
                continue
            open_str, close_str = session.split("-", 1)
            try:
                open_t = pd.Timestamp(open_str.strip()).time()
                close_t = pd.Timestamp(close_str.strip()).time()
            except Exception:
                continue
            if open_t <= current <= close_t:
                return index
        return None

    @staticmethod
    def _time_in_windows(current: Any, windows: str) -> bool:
        return TargetQuantityStrategy._time_window_index(current, windows) is not None

    def _local_trading_window_index(self) -> int | None:
        try:
            now = pd.Timestamp(self.clock.utc_now()).tz_convert(self.config.timezone_name).time()
        except Exception:
            return 0
        return self._time_window_index(now, self.config.trading_windows)

    def _within_trading_time(self) -> bool:
        """True when the clock is inside a scheduled trading session (time only).

        This is the necessary-but-not-sufficient half of ``_within_trading_window``:
        it ignores exchange event time.
        """
        return self._local_trading_window_index() is not None

    def _within_exchange_trading_time(
        self,
        ts_event: int | None = None,
        session_index: int | None = None,
    ) -> bool:
        event_ts = int(ts_event or 0) or self._last_quote_tick_ts_event
        if event_ts <= 0:
            return False
        try:
            event_time = pd.Timestamp(event_ts, unit="ns", tz="UTC").tz_convert(
                self.config.timezone_name,
            )
        except Exception:
            return False
        if event_time.date() != self._clock_date():
            return False
        event_window_index = self._time_window_index(
            event_time.time(),
            self.config.exchange_trading_windows,
        )
        if event_window_index is None:
            return False
        return session_index is None or event_window_index == session_index

    def _within_trading_window(self, ts_event: int | None = None) -> bool:
        # Both local clock time and exchange event time must be inside their
        # configured windows. Live timer/cancel paths use the latest quote tick
        # ts_event; data-driven paths can pass the current event timestamp.
        local_window_index = self._local_trading_window_index()
        if local_window_index is None:
            return False
        return self._within_exchange_trading_time(ts_event, session_index=local_window_index)

    def _stop_time_reached(self) -> bool:
        stop_time = self.config.stop_time
        if not stop_time:
            return False
        try:
            now = pd.Timestamp(self.clock.utc_now()).tz_convert(self.config.timezone_name).time()
            stop = pd.Timestamp(str(stop_time).strip()).time()
        except Exception:
            return False
        return now >= stop

    def _clock_date(self) -> date:
        try:
            return pd.Timestamp(self.clock.utc_now()).tz_convert(self.config.timezone_name).date()
        except Exception:
            return date.today()

    def _record_target(
        self,
        trading_date: date,
        instrument_id: str,
        target_qty: Decimal,
        reason: str,
        intent_context: IntentContext,
    ) -> None:
        target_qty = Decimal(str(target_qty))
        current_qty = self._current_quantity(InstrumentId.from_str(instrument_id))
        extra: dict[str, Any] = {"target_version": self._target_version}
        intent_context.record(
            record_type="target",
            record_reason=reason,
            record_trading_date=trading_date,
        )
        extra["intent_context"] = intent_context.snapshot()
        self.target_events.append(
            TargetQuantityTargetEvent(
                target_id=f"{self._target_version}-{instrument_id}-{len(self.target_events)}",
                target_date=self._target_date or trading_date,
                execute_date=trading_date,
                instrument_id=instrument_id,
                target_qty=target_qty,
                current_qty=current_qty,
                delta_qty=target_qty - current_qty,
                reason=reason,
                extra=extra,
            ),
        )
        self.log.info(
            f"Recorded target instrument_id={instrument_id} target_qty={target_qty} "
            f"current_qty={current_qty} reason={reason} "
            f"intent_context={intent_context.log_text()}",
            color=LogColor.BLUE,
        )

    def _record_order(
        self,
        trading_date: date,
        instrument_id: str,
        side: str,
        quantity: int,
        target_qty: Decimal,
        status: str,
        reason: str | None,
        intent_context: IntentContext,
        order_id: str | None = None,
    ) -> None:
        extra: dict[str, Any] = {"target_version": self._target_version}
        intent_context.record(
            record_type="order",
            record_status=status,
            record_reason=reason,
            record_order_id=order_id,
            record_trading_date=trading_date,
            record_quantity=quantity,
        )
        extra["intent_context"] = intent_context.snapshot()
        self.order_events.append(
            TargetQuantityOrderEvent(
                order_id=order_id or f"internal-{trading_date.isoformat()}-{instrument_id}-{len(self.order_events)}",
                trading_date=trading_date,
                instrument_id=instrument_id,
                side=side,
                quantity=int(quantity),
                target_qty=Decimal(str(target_qty)),
                status=status,
                reason=reason,
                extra=extra,
            ),
        )
        self.log.info(
            f"Recorded order instrument_id={instrument_id} side={side} quantity={quantity} "
            f"target_qty={target_qty} status={status} reason={reason} order_id={order_id} "
            f"intent_context={intent_context.log_text()}",
            color=LogColor.BLUE,
        )


_INSUFFICIENT_FUNDS_MARKERS = (
    "260200",
    "\u53ef\u7528\u8d44\u91d1\u4e0d\u8db3",
    "\u8d44\u91d1\u4e0d\u8db3",
    "insufficient",
    "free_balance",
    "free balance",
    "cum_notional_exceeds_free_balance",
)
_SELLABLE_POSITION_MARKERS = (
    "251005",
    "sellable volume",
    "sellable position",
    "can_use_volume",
    "\u8bc1\u5238\u53ef\u7528\u6570\u91cf\u4e0d\u8db3",
    "\u53ef\u7528\u6301\u4ed3\u4e0d\u8db3",
    "\u53ef\u5356\u6570\u91cf\u4e0d\u8db3",
)


def _is_insufficient_funds(reason: str) -> bool:
    text = (reason or "").lower()
    return any(marker.lower() in text for marker in _INSUFFICIENT_FUNDS_MARKERS)


def _is_sellable_position_denial(reason: str) -> bool:
    text = (reason or "").lower()
    return any(marker.lower() in text for marker in _SELLABLE_POSITION_MARKERS)


def normalize_initial_last_closes(raw: dict[str, float] | None) -> dict[str, float]:
    result: dict[str, float] = {}
    for instrument_id, close in (raw or {}).items():
        try:
            close_value = float(close)
        except (TypeError, ValueError):
            continue
        if close_value > 0:
            result[str(instrument_id)] = close_value
    return result


def bar_date(bar: Bar, timezone_name: str) -> date:
    return pd.Timestamp(bar.ts_event, unit="ns", tz="UTC").tz_convert(timezone_name).date()


def target_version(target_date: date, quantities: dict[str, Decimal], reason: str) -> str:
    total = sum((Decimal(str(qty)) for qty in quantities.values()), Decimal("0"))
    return f"{target_date.isoformat()}-{reason}-{len(quantities)}-{int(total)}"
