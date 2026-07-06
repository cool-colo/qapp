from __future__ import annotations

import asyncio
import inspect
import random
from dataclasses import dataclass
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
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.trading.strategy import Strategy


@dataclass(frozen=True)
class TargetWeightPlan:
    target_date: date
    weights: dict[str, float]
    reason: str
    version: str


@dataclass(frozen=True)
class TargetWeightTargetEvent:
    target_id: str
    target_date: date
    execute_date: date
    instrument_id: str
    target_weight: float
    current_weight: float | None
    delta_weight: float | None
    reason: str
    extra: dict[str, Any]


@dataclass(frozen=True)
class TargetWeightOrderEvent:
    order_id: str
    trading_date: date
    instrument_id: str
    side: str
    quantity: int
    target_weight: float
    status: str
    reason: str | None
    extra: dict[str, Any]


@dataclass(frozen=True)
class TargetOrderIntent:
    instrument_id: InstrumentId
    instrument: Any
    side: OrderSide
    quantity: Decimal
    price: Any


@dataclass(frozen=True)
class TodayFillSnapshot:
    buy_qty: Decimal
    sell_qty: Decimal
    fill_count: int
    latest_ts_event: int


class NotionalOrderSplitter:
    """Splits a target delta into same-side orders capped by notional amount."""

    def __init__(self, max_order_notional: Decimal) -> None:
        self.max_order_notional = Decimal(str(max_order_notional))

    def split(self, instrument: Any, quantity: Decimal, price: Decimal) -> list[Decimal]:
        if quantity <= 0:
            return []
        if self.max_order_notional <= 0 or price <= 0:
            return [quantity]
        if quantity * price <= self.max_order_notional:
            return [quantity]

        lot_size = Decimal(str(getattr(instrument, "lot_size", Decimal("1"))))
        if lot_size <= 0:
            lot_size = Decimal("1")
        max_qty = (self.max_order_notional / price // lot_size) * lot_size
        if max_qty <= 0:
            return [quantity]

        slices: list[Decimal] = []
        remaining = quantity
        while remaining > max_qty:
            slices.append(max_qty)
            remaining -= max_qty
        if remaining > 0:
            slices.append(remaining)
        return slices


class TargetWeightStrategyConfig(StrategyConfig, kw_only=True, frozen=True):
    instrument_ids: list[InstrumentId]
    bar_types: dict[str, BarType]
    initial_cash: Decimal = Decimal("1000000")
    timezone_name: str = "Asia/Shanghai"
    trading_windows: str = "09:30-11:30,13:00-14:55"
    stop_time: str | None = "14:55"
    initial_last_closes: dict[str, float] | None = None
    star_min_buy_qty: int = 200
    unfilled_timeout_secs: float = 60.0
    resubmit_check_interval_secs: float = 10.0
    cash_buffer_percent: float = 0.01
    target_cash_buffer_percent: float = 0.05
    weight_tolerance_percent: float = 0.003
    cash_tolerance_percent: float = 0.01
    price_offset_ticks: int = 1
    exit_non_targets: bool = True
    limit_stop_mode: str = "freeze_symbol"
    order_slice_notional: Decimal = Decimal("300000")
    require_account_cash: bool = True
    quote_tick_log_sample_rate: float = 0.0
    trade_tick_log_sample_rate: float = 0.0
    order_book_depth_log_sample_rate: float = 0.0


class TargetWeightStrategy(Strategy):
    """
    Nautilus-first target-weight executor.

    Subclasses or external controllers provide target weights. This class owns
    the account-aware convergence loop: exits, cash-gated buys, stale-order
    cancel/resubmit, symbol freezes, and achievement checks.
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

    def __init__(self, config: TargetWeightStrategyConfig) -> None:
        super().__init__(config)
        self._instrument_ids = list(config.instrument_ids)
        self._bar_types = {str(key): value for key, value in config.bar_types.items()}
        self._last_close = normalize_initial_last_closes(config.initial_last_closes)
        self._target_weights: dict[str, float] = {}
        self._target_date: date | None = None
        self._target_reason = "target_weight"
        self._target_version = ""
        self._achieved_versions: set[str] = set()
        self._frozen_instruments: dict[str, str] = {}
        self._deferred_buys: dict[str, float] = {}
        self._rejected_order_ids: set[str] = set()
        self._insufficient_funds: set[str] = set()
        self._order_submit_ts: dict[str, int] = {}
        self._order_target_weights: dict[str, float] = {}
        self._order_target_versions: dict[str, str] = {}
        self._order_splitter = NotionalOrderSplitter(config.order_slice_notional)
        self._convergence_suspended = False
        self._pre_open_reconcile = None
        self._pre_open_reconcile_time: tuple[int, int] | None = None
        self._pre_open_reconcile_timeout_secs = 30.0
        self._pre_open_reconcile_task: asyncio.Future[Any] | None = None
        self._sellable_exhausted: dict[str, date] = {}
        self.target_events: list[TargetWeightTargetEvent] = []
        self.order_events: list[TargetWeightOrderEvent] = []

    def configure_pre_open_reconciliation(
        self,
        reconcile: Any | None,
        reconcile_time: str | None,
        timeout_secs: float,
    ) -> None:
        self._pre_open_reconcile = reconcile
        self._pre_open_reconcile_time = self._parse_hh_mm(reconcile_time)
        self._pre_open_reconcile_timeout_secs = float(timeout_secs)

    def on_start(self) -> None:
        self.log.info(
            f"target-weight executor start: instruments={len(self._instrument_ids)} "
            f"bar_types={len(self._bar_types)} target_version={self._target_version or '<none>'}",
            color=LogColor.BLUE,
        )
        if self._pre_open_reconcile_time is not None:
            self._schedule_pre_open_reconcile()
        if not self._bar_types:
            self.log.warning("target-weight executor has no bar_types configured")
        for bar_type in self._bar_types.values():
            self.subscribe_bars(bar_type)
            self.subscribe_quote_ticks(bar_type.instrument_id)
            # self.subscribe_trade_ticks(bar_type.instrument_id)
            if self._order_book_depth_logging_enabled():
                self.subscribe_order_book_depth(
                    bar_type.instrument_id,
                    book_type=BookType.L2_MBP,
                    depth=10,
                )
        interval_secs = float(self.config.resubmit_check_interval_secs)
        if float(self.config.unfilled_timeout_secs) > 0 and interval_secs > 0:
            self.clock.set_timer(
                name="TARGET-WEIGHT-CONVERGE",
                interval=timedelta(seconds=interval_secs),
                callback=self._on_converge_timer,
                fire_immediately=False,
            )

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
        if self._pre_open_reconcile_time is not None:
            self._schedule_pre_open_reconcile()
        if self._pre_open_reconcile is None:
            self.log.warning("Pre-open execution-state reconciliation is configured but no callback is available")
            return
        if self._pre_open_reconcile_task is not None and not self._pre_open_reconcile_task.done():
            self.log.warning("Previous pre-open execution-state reconciliation is still running; skipping")
            return
        try:
            result = self._pre_open_reconcile(timeout_secs=self._pre_open_reconcile_timeout_secs)
        except Exception as exc:
            self.log.warning(f"Pre-open execution-state reconciliation failed to start: {exc}")
            return
        if inspect.isawaitable(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError as exc:
                if inspect.iscoroutine(result):
                    result.close()
                self.log.warning(f"Pre-open execution-state reconciliation has no running event loop: {exc}")
                return
            task = asyncio.ensure_future(result, loop=loop)
            self._pre_open_reconcile_task = task
            task.add_done_callback(self._on_pre_open_reconcile_done)
            self.log.info("Started pre-open execution-state reconciliation", color=LogColor.BLUE)
            return
        self._log_pre_open_reconcile_result(bool(result))

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
            removable = existing_bar_type_keys.difference(refreshed_bar_types).difference(self._target_weights)
            for key in sorted(removable):
                if self._current_quantity(InstrumentId.from_str(key)) > 0:
                    continue
                try:
                    self.unsubscribe_bars(self._bar_types[key])
                except Exception as exc:
                    self.log.warning(f"Bar unsubscribe failed for {self._bar_types[key]}: {exc}")
                if self._order_book_depth_logging_enabled():
                    try:
                        self.unsubscribe_order_book_depth(self._bar_types[key].instrument_id)
                    except Exception as exc:
                        self.log.warning(
                            f"Order-book depth unsubscribe failed for {self._bar_types[key].instrument_id}: {exc}",
                        )
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
                self.subscribe_bars(bar_type)
                self.subscribe_quote_ticks(bar_type.instrument_id)
                self.subscribe_trade_ticks(bar_type.instrument_id)
                if self._order_book_depth_logging_enabled():
                    self.subscribe_order_book_depth(
                        bar_type.instrument_id,
                        book_type=BookType.L2_MBP,
                        depth=10,
                    )

    def update_target_weights(
        self,
        weights: dict[str | InstrumentId, float],
        target_date: date,
        reason: str,
        version: str | None = None,
    ) -> None:
        normalized: dict[str, float] = {}
        for instrument_id, weight in weights.items():
            instrument_id_text = str(instrument_id)
            weight_value = float(weight)
            if weight_value <= 0:
                continue
            normalized[instrument_id_text] = weight_value
        version_value = version or target_version(target_date, normalized, reason)
        if version_value == self._target_version and normalized == self._target_weights:
            return
        self._target_weights = dict(sorted(normalized.items()))
        self._target_date = target_date
        self._target_reason = reason
        self._target_version = version_value
        self._frozen_instruments = {}
        self._deferred_buys = {}
        self._insufficient_funds = set()
        self._achieved_versions.discard(version_value)
        total_weight = sum(self._target_weights.values())
        if total_weight > 1.0:
            self.log.warning(f"target weights sum to {total_weight:.6f}; buys may be cash-constrained")
        self.log.info(
            f"accepted target weights version={version_value} date={target_date} "
            f"count={len(self._target_weights)} total_weight={total_weight:.6f} reason={reason}",
            color=LogColor.BLUE,
        )
        if self._convergence_suspended or not self._within_trading_window():
            return
        self._converge_to_target(current_date=target_date, trigger="target_update")

    def on_bar(self, bar: Bar) -> None:
        instrument_id = str(bar.bar_type.instrument_id)
        self._last_close[instrument_id] = float(bar.close)
        trading_date = bar_date(bar, self.config.timezone_name)
        within_window = self._within_trading_window()
        self._convergence_suspended = not within_window
        self.on_target_bar(bar)
        self._convergence_suspended = False
        if not within_window:
            return
        self._converge_to_target(current_date=trading_date, trigger="bar")

    def on_target_bar(self, bar: Bar) -> None:
        """Hook for subclasses to update targets before convergence."""

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if not self._should_log_sample(self.config.quote_tick_log_sample_rate):
            return
        self.log.info(
            "Quote tick order-book sample, "
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
        self._order_submit_ts.pop(client_order_id, None)
        self._order_target_weights.pop(client_order_id, None)
        self._order_target_versions.pop(client_order_id, None)
        self._deferred_buys.pop(instrument_id_text, None)
        self._insufficient_funds.discard(instrument_id_text)

    def on_order_canceled(self, event: Any) -> None:
        client_order_id = str(event.client_order_id)
        self._order_submit_ts.pop(client_order_id, None)
        self._order_target_weights.pop(client_order_id, None)
        self._order_target_versions.pop(client_order_id, None)
        if self._within_trading_window():
            self._converge_to_target(current_date=self._clock_date(), trigger="cancel")

    def on_order_rejected(self, event: Any) -> None:
        client_order_id = str(event.client_order_id)
        instrument_id_text = str(getattr(event, "instrument_id", ""))
        reason = str(getattr(event, "reason", "") or "")
        target_weight = self._order_target_weights.pop(client_order_id, self._target_weights.get(instrument_id_text, 0.0))
        self._order_submit_ts.pop(client_order_id, None)
        self._order_target_versions.pop(client_order_id, None)
        self._rejected_order_ids.add(client_order_id)
        if _is_insufficient_funds(reason):
            if instrument_id_text:
                self._insufficient_funds.add(instrument_id_text)
                if target_weight > 0:
                    self._deferred_buys[instrument_id_text] = target_weight
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
        if not self._within_trading_window():
            return
        try:
            self._converge_to_target(current_date=self._clock_date(), trigger="timer")
        except Exception as exc:
            self.log.warning(f"target convergence failed: {exc}")

    def _converge_to_target(self, current_date: date, trigger: str) -> None:
        if not self._target_version:
            return
        if self._target_version in self._achieved_versions:
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
        self._reconcile_unfilled_orders(current_date)
        open_order_instruments = self._open_order_instruments()
        desired = self._desired_weights()
        self._refresh_symbol_freezes(desired)
        sell_targets: dict[str, float] = {}
        buy_targets: dict[str, float] = {}
        for instrument_id_text, target_weight in desired.items():
            if instrument_id_text in open_order_instruments:
                continue
            if instrument_id_text in self._frozen_instruments:
                continue
            side = self._target_side(instrument_id_text, target_weight)
            if side == "sell":
                if self._sellable_exhausted.get(instrument_id_text) == current_date:
                    continue
                sell_targets[instrument_id_text] = target_weight
            elif side == "buy":
                buy_targets[instrument_id_text] = target_weight

        for instrument_id_text, target_weight in sorted(sell_targets.items()):
            self._record_target(current_date, instrument_id_text, target_weight, self._target_reason)
            self._submit_target_weight(current_date, instrument_id_text, target_weight, self._target_reason)
        if buy_targets:
            self._submit_buys_within_cash(current_date, buy_targets, self._target_reason)

        if self._target_achieved():
            self._achieved_versions.add(self._target_version)
            self.log.info(
                f"target achieved version={self._target_version} trigger={trigger} "
                f"count={len(self._target_weights)}",
                color=LogColor.GREEN,
            )

    def _desired_weights(self) -> dict[str, float]:
        desired = dict(self._target_weights)
        if not bool(self.config.exit_non_targets):
            return desired
        for instrument_id in self._held_instrument_ids():
            desired.setdefault(instrument_id, 0.0)
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

    def _target_side(self, instrument_id_text: str, target_weight: float) -> str | None:
        instrument_id = InstrumentId.from_str(instrument_id_text)
        current_qty = self._current_quantity(instrument_id)
        if target_weight <= 0:
            return "sell" if current_qty > 0 else None
        instrument = self.cache.instrument(instrument_id)
        close_price = self._last_close.get(instrument_id_text)
        if instrument is None or close_price is None or close_price <= 0:
            return "buy" if current_qty <= 0 else None
        current_weight = self._current_weight(instrument_id_text)
        if current_weight is not None:
            tolerance = max(0.0, float(self.config.weight_tolerance_percent))
            if abs(current_weight - float(target_weight)) <= tolerance:
                return None
        target_qty = self._target_quantity(instrument, close_price, target_weight)
        delta_qty = target_qty - current_qty
        if delta_qty > 0:
            return "buy"
        if delta_qty < 0:
            return "sell"
        return None

    def _refresh_symbol_freezes(self, desired: dict[str, float]) -> None:
        if str(self.config.limit_stop_mode) != "freeze_symbol":
            return
        for instrument_id_text, target_weight in desired.items():
            if instrument_id_text in self._frozen_instruments:
                continue
            side = self._target_side(instrument_id_text, target_weight)
            if side is None:
                continue
            limit_reason = self._price_limit_reason(instrument_id_text, side)
            if limit_reason is None:
                continue
            self._frozen_instruments[instrument_id_text] = limit_reason
            self._deferred_buys.pop(instrument_id_text, None)
            self._insufficient_funds.discard(instrument_id_text)
            self.log.warning(
                f"Freezing {instrument_id_text} for target version={self._target_version}: {limit_reason}",
            )

    def _price_limit_reason(self, instrument_id_text: str, side: str) -> str | None:
        price = self._last_close.get(instrument_id_text)
        if price is None or price <= 0:
            return None
        up_limit, down_limit = self._price_limits(instrument_id_text)
        if side == "buy" and up_limit is not None and price >= up_limit:
            return "up_limit"
        if side == "sell" and down_limit is not None and price <= down_limit:
            return "down_limit"
        return None

    def _target_achieved(self) -> bool:
        if self._open_order_instruments():
            return False
        if self._deferred_buys or self._insufficient_funds:
            return False
        desired = self._desired_weights()
        tolerance = max(0.0, float(self.config.weight_tolerance_percent))
        for instrument_id_text, target_weight in desired.items():
            if instrument_id_text in self._frozen_instruments:
                continue
            current_weight = self._current_weight(instrument_id_text)
            if current_weight is None:
                if self._current_quantity(InstrumentId.from_str(instrument_id_text)) <= 0 and target_weight <= tolerance:
                    continue
                return False
            if abs(current_weight - target_weight) > tolerance:
                return False
        target_cash = max(float(self.config.target_cash_buffer_percent), 1.0 - sum(self._target_weights.values()))
        free_cash = self._free_cash()
        if free_cash is None:
            return False
        cash_weight = float(free_cash / max(self._portfolio_value(), Decimal("1")))
        return cash_weight <= target_cash + max(0.0, float(self.config.cash_tolerance_percent))

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
            submit_ts = self._order_submit_ts.get(client_order_id)
            if submit_ts is None:
                submit_ts = int(getattr(order, "ts_last", now) or now)
            if now - submit_ts < timeout_ns:
                continue
            if self._at_price_limit(order):
                continue
            try:
                self.cancel_order(order)
            except Exception as exc:
                self.log.warning(f"cancel_order failed for {client_order_id}: {exc}")
                continue
            self._order_submit_ts.pop(client_order_id, None)
            self._order_target_weights.pop(client_order_id, None)
            self._order_target_versions.pop(client_order_id, None)
            self._record_order(
                trading_date=trading_date,
                instrument_id=instrument_id_text,
                side="buy" if order.side == OrderSide.BUY else "sell",
                quantity=0,
                target_weight=self._target_weights.get(instrument_id_text, 0.0),
                status="canceled",
                reason="unfilled_timeout",
                order_id=client_order_id,
            )

    def _submit_buys_within_cash(
        self,
        trading_date: date,
        buy_candidates: dict[str, float],
        reason: str,
    ) -> None:
        if not buy_candidates:
            return
        free_cash = self._free_cash()
        if free_cash is None:
            for instrument_id, target_weight in buy_candidates.items():
                self._deferred_buys[instrument_id] = target_weight
                self._record_order(
                    trading_date,
                    instrument_id,
                    "buy",
                    0,
                    target_weight,
                    "deferred",
                    "missing_free_cash",
                )
            return
        buffer_pct = max(0.0, float(self.config.cash_buffer_percent))
        if buffer_pct > 0:
            free_cash = free_cash * Decimal(str(1.0 - min(buffer_pct, 1.0)))
        for instrument_id in sorted(buy_candidates, key=lambda i: buy_candidates[i], reverse=True):
            target_weight = buy_candidates[instrument_id]
            if instrument_id in self._insufficient_funds:
                self._deferred_buys[instrument_id] = target_weight
                continue
            intent = self._target_order_intent(instrument_id, target_weight)
            if intent is None:
                self._record_target(trading_date, instrument_id, target_weight, reason)
                self._submit_target_weight(trading_date, instrument_id, target_weight, reason)
                continue
            if intent.side != OrderSide.BUY:
                continue
            slices = self._order_slices(intent)
            if not slices:
                continue
            submitted_any = False
            for quantity in slices:
                est_cost = self._estimated_order_cost(quantity, intent.price)
                if est_cost > free_cash:
                    self._deferred_buys[instrument_id] = target_weight
                    self._record_order(
                        trading_date,
                        instrument_id,
                        "buy",
                        0,
                        target_weight,
                        "deferred",
                        "insufficient_cash",
                    )
                    break
                if not submitted_any:
                    self._record_target(trading_date, instrument_id, target_weight, reason)
                self._submit_order_quantity(
                    trading_date=trading_date,
                    intent=intent,
                    quantity=quantity,
                    target_weight=target_weight,
                    reason=reason,
                )
                submitted_any = True
                free_cash -= est_cost
            if not submitted_any and instrument_id not in self._deferred_buys:
                self._deferred_buys[instrument_id] = target_weight

    def _target_order_intent(self, instrument_id_text: str, target_weight: float) -> TargetOrderIntent | None:
        instrument_id = InstrumentId.from_str(instrument_id_text)
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            return None
        current_qty = self._current_quantity(instrument_id)
        if target_weight <= 0:
            if current_qty <= 0:
                return None
            price = self._limit_price(instrument, instrument_id, OrderSide.SELL)
            return TargetOrderIntent(instrument_id, instrument, OrderSide.SELL, abs(current_qty), price)
        close_price = self._last_close.get(instrument_id_text)
        if close_price is None or close_price <= 0:
            return None
        current_weight = self._current_weight(instrument_id_text)
        if current_weight is not None:
            tolerance = max(0.0, float(self.config.weight_tolerance_percent))
            if abs(current_weight - float(target_weight)) <= tolerance:
                return None
        target_qty = self._target_quantity(instrument, close_price, target_weight)
        delta_qty = target_qty - current_qty
        if delta_qty == 0:
            return None
        side = OrderSide.BUY if delta_qty > 0 else OrderSide.SELL
        price = self._limit_price(instrument, instrument_id, side)
        if price is None:
            return None
        return TargetOrderIntent(instrument_id, instrument, side, abs(delta_qty), price)

    def _order_slices(self, intent: TargetOrderIntent) -> list[Decimal]:
        if intent.price is None:
            return [intent.quantity]
        return self._order_splitter.split(
            instrument=intent.instrument,
            quantity=Decimal(str(intent.quantity)),
            price=Decimal(str(intent.price)),
        )

    @staticmethod
    def _estimated_order_cost(quantity: Decimal, price: Any) -> Decimal:
        return Decimal(str(quantity)) * Decimal(str(price))

    def _estimated_buy_cost(self, instrument_id_text: str, target_weight: float) -> Decimal | None:
        instrument_id = InstrumentId.from_str(instrument_id_text)
        instrument = self.cache.instrument(instrument_id)
        close_price = self._last_close.get(instrument_id_text)
        if instrument is None or close_price is None or close_price <= 0:
            return None
        current_qty = self._current_quantity(instrument_id)
        target_qty = self._target_quantity(instrument, close_price, target_weight)
        delta_qty = target_qty - current_qty
        if delta_qty <= 0:
            return Decimal("0")
        return Decimal(str(delta_qty)) * Decimal(str(close_price))

    def _submit_target_weight(
        self,
        trading_date: date,
        instrument_id_text: str,
        target_weight: float,
        reason: str,
    ) -> bool:
        instrument_id = InstrumentId.from_str(instrument_id_text)
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self._record_order(trading_date, instrument_id_text, "buy", 0, target_weight, "rejected", "missing_instrument")
            return False
        current_qty = self._current_quantity(instrument_id)
        if target_weight <= 0:
            if current_qty <= 0:
                return True
            return self._submit_full_exit(trading_date, instrument_id, instrument, current_qty, reason)
        intent = self._target_order_intent(instrument_id_text, target_weight)
        if intent is None:
            if self._last_close.get(instrument_id_text) is None:
                self._record_order(trading_date, instrument_id_text, "buy", 0, target_weight, "rejected", "missing_price")
                return False
            self._record_order(trading_date, instrument_id_text, "buy", 0, target_weight, "skipped", "already_target")
            return True
        for quantity in self._order_slices(intent):
            self._submit_order_quantity(
                trading_date=trading_date,
                intent=intent,
                quantity=quantity,
                target_weight=target_weight,
                reason=reason,
            )
        return True

    def _submit_full_exit(
        self,
        trading_date: date,
        instrument_id: InstrumentId,
        instrument: Any,
        current_qty: Decimal,
        reason: str,
    ) -> bool:
        qty_abs = self._clamp_sell_quantity(
            trading_date=trading_date,
            instrument_id=instrument_id,
            requested_qty=abs(current_qty),
            target_weight=0.0,
            reason=reason,
        )
        if qty_abs is None or qty_abs <= 0:
            return False
        price = self._limit_price(instrument, instrument_id, OrderSide.SELL)
        if price is None:
            order = self.order_factory.market(
                instrument_id=instrument_id,
                order_side=OrderSide.SELL,
                quantity=instrument.make_qty(qty_abs),
            )
            self.submit_order(order)
            self._track_submitted_order(order, 0.0)
            self._record_order(
                trading_date,
                str(instrument_id),
                "sell",
                int(qty_abs),
                0.0,
                "submitted",
                reason,
                str(order.client_order_id),
            )
            return True
        intent = TargetOrderIntent(instrument_id, instrument, OrderSide.SELL, qty_abs, price)
        for quantity in self._order_slices(intent):
            self._submit_order_quantity(
                trading_date=trading_date,
                intent=intent,
                quantity=quantity,
                target_weight=0.0,
                reason=reason,
            )
        return True

    def _submit_order_quantity(
        self,
        trading_date: date,
        intent: TargetOrderIntent,
        quantity: Decimal,
        target_weight: float,
        reason: str,
    ) -> bool:
        if intent.side == OrderSide.SELL:
            clamped_quantity = self._clamp_sell_quantity(
                trading_date=trading_date,
                instrument_id=intent.instrument_id,
                requested_qty=quantity,
                target_weight=target_weight,
                reason=reason,
            )
            if clamped_quantity is None or clamped_quantity <= 0:
                return False
            quantity = clamped_quantity
        if intent.price is not None:
            order = self.order_factory.limit(
                instrument_id=intent.instrument_id,
                order_side=intent.side,
                quantity=intent.instrument.make_qty(quantity),
                price=intent.price,
            )
        else:
            order = self.order_factory.market(
                instrument_id=intent.instrument_id,
                order_side=intent.side,
                quantity=intent.instrument.make_qty(quantity),
            )
        self.submit_order(order)
        self._track_submitted_order(order, target_weight)
        side_text = "buy" if intent.side == OrderSide.BUY else "sell"
        self._record_order(
            trading_date,
            str(intent.instrument_id),
            side_text,
            int(quantity),
            target_weight,
            "submitted",
            reason,
            str(order.client_order_id),
        )
        return True

    def _clamp_sell_quantity(
        self,
        trading_date: date,
        instrument_id: InstrumentId,
        requested_qty: Decimal,
        target_weight: float,
        reason: str,
    ) -> Decimal | None:
        instrument_id_text = str(instrument_id)
        if requested_qty <= 0:
            return Decimal("0")

        fills_before = self._today_fill_snapshot(instrument_id, trading_date)
        current_qty = self._current_quantity(instrument_id)
        open_sell_qty = self._open_sell_quantity(instrument_id)
        fills_after = self._today_fill_snapshot(instrument_id, trading_date)
        if fills_before != fills_after:
            self._record_order(
                trading_date,
                instrument_id_text,
                "sell",
                0,
                target_weight,
                "deferred",
                "sellable_snapshot_unstable",
            )
            self.log.warning(
                f"Deferring SELL for {instrument_id_text}: Nautilus fill snapshot changed while "
                f"calculating sellable quantity; before={fills_before}, after={fills_after}",
            )
            return None

        sellable_qty = max(Decimal("0"), current_qty - fills_after.buy_qty - open_sell_qty)
        if sellable_qty <= 0:
            self._sellable_exhausted[instrument_id_text] = trading_date
            self._record_order(
                trading_date,
                instrument_id_text,
                "sell",
                0,
                target_weight,
                "deferred",
                "sellable_exhausted",
            )
            self.log.warning(
                f"Skipping SELL for {instrument_id_text}: strategy sellable estimate exhausted "
                f"(net_qty={current_qty}, today_buy_qty={fills_after.buy_qty}, open_sell_qty={open_sell_qty}, "
                f"requested_qty={requested_qty})",
            )
            return Decimal("0")

        clamped_qty = min(Decimal(str(requested_qty)), sellable_qty)
        if clamped_qty < requested_qty:
            self._sellable_exhausted[instrument_id_text] = trading_date
            self.log.warning(
                f"Clamping SELL for {instrument_id_text}: requested_qty={requested_qty}, "
                f"strategy_sellable_qty={sellable_qty}, today_buy_qty={fills_after.buy_qty}, "
                f"open_sell_qty={open_sell_qty}",
            )
        return clamped_qty

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
        try:
            open_orders = self.cache.orders_open(instrument_id=instrument_id, side=OrderSide.SELL)
        except Exception:
            open_orders = []
        for order in open_orders:
            if getattr(order, "instrument_id", None) != instrument_id:
                continue
            if getattr(order, "side", None) != OrderSide.SELL:
                continue
            try:
                if order.is_pending_cancel:
                    continue
            except Exception:
                pass
            quantity = getattr(order, "leaves_qty", getattr(order, "quantity", Decimal("0")))
            total += self._decimal_quantity(quantity)
        return total

    @staticmethod
    def _decimal_quantity(quantity: Any) -> Decimal:
        if quantity is None:
            return Decimal("0")
        try:
            return Decimal(str(quantity.as_decimal()))
        except Exception:
            return Decimal(str(quantity))

    def _track_submitted_order(self, order: Any, target_weight: float) -> None:
        client_order_id = str(order.client_order_id)
        self._order_submit_ts[client_order_id] = self.clock.timestamp_ns()
        self._order_target_weights[client_order_id] = float(target_weight)
        self._order_target_versions[client_order_id] = self._target_version

    def _limit_price(self, instrument: Any, instrument_id: InstrumentId, side: OrderSide) -> Any | None:
        offset_ticks = int(self.config.price_offset_ticks)
        tick = float(instrument.price_increment)
        offset = offset_ticks * tick
        quote = self.cache.quote_tick(instrument_id)
        if quote is not None:
            base = float(quote.ask_price) if side == OrderSide.BUY else float(quote.bid_price)
            if base > 0:
                raw = base + offset if side == OrderSide.BUY else base - offset
                if raw > 0:
                    return instrument.make_price(raw)
        close_price = self._last_close.get(str(instrument_id))
        if close_price is None or close_price <= 0:
            return None
        raw = close_price + offset if side == OrderSide.BUY else close_price - offset
        if raw <= 0:
            return None
        return instrument.make_price(raw)

    def _target_quantity(self, instrument: Any, close_price: float, target_weight: float) -> Decimal:
        if target_weight <= 0:
            return Decimal("0")
        raw_qty = self._portfolio_value() * Decimal(str(target_weight)) / Decimal(str(close_price))
        if self._is_star_market(instrument):
            min_qty = Decimal(str(self.config.star_min_buy_qty))
            if raw_qty < min_qty:
                return Decimal("0")
            return Decimal(int(raw_qty))
        lot_size = Decimal(str(instrument.lot_size))
        if lot_size <= 0:
            lot_size = Decimal("1")
        return (raw_qty // lot_size) * lot_size

    @staticmethod
    def _is_star_market(instrument: Any) -> bool:
        try:
            symbol = str(getattr(instrument, "raw_symbol", "") or instrument.id)
        except Exception:
            return False
        return symbol.lstrip().startswith("688")

    def _portfolio_value(self) -> Decimal:
        venue = self._instrument_ids[0].venue if self._instrument_ids else None
        try:
            equity = self.portfolio.equity(venue=Venue(str(venue))) if venue is not None else self.portfolio.equity()
        except Exception:
            equity = {}
        if equity:
            first = next(iter(equity.values()))
            try:
                return Decimal(str(first.as_decimal()))
            except Exception:
                return Decimal(str(float(first)))
        return Decimal(str(self.config.initial_cash))

    def _free_cash(self) -> Decimal | None:
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

    def _current_quantity(self, instrument_id: InstrumentId) -> Decimal:
        try:
            qty = self.portfolio.net_position(instrument_id)
        except Exception:
            return Decimal("0")
        if qty is None:
            return Decimal("0")
        return Decimal(str(qty))

    def _current_weight(self, instrument_id_text: str) -> float | None:
        close_price = self._last_close.get(instrument_id_text)
        if close_price is None or close_price <= 0:
            return None
        portfolio_value = self._portfolio_value()
        if portfolio_value <= 0:
            return None
        qty = self._current_quantity(InstrumentId.from_str(instrument_id_text))
        return float(qty * Decimal(str(close_price)) / portfolio_value)

    def _open_order_instruments(self) -> set[str]:
        try:
            open_orders = self.cache.orders_open(strategy_id=self.id)
        except Exception:
            return set()
        return {str(order.instrument_id) for order in open_orders}

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
        side = "buy" if order.side == OrderSide.BUY else "sell"
        return self._price_limit_reason(str(order.instrument_id), side) is not None

    def _within_trading_window(self) -> bool:
        try:
            now = pd.Timestamp(self.clock.utc_now()).tz_convert(self.config.timezone_name).time()
        except Exception:
            return True
        for session in str(self.config.trading_windows).split(","):
            session = session.strip()
            if not session or "-" not in session:
                continue
            open_str, close_str = session.split("-", 1)
            try:
                open_t = pd.Timestamp(open_str.strip()).time()
                close_t = pd.Timestamp(close_str.strip()).time()
            except Exception:
                continue
            if open_t <= now <= close_t:
                return True
        return False

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
        target_weight: float,
        reason: str,
    ) -> None:
        current_weight = self._current_weight(instrument_id)
        delta_weight = None if current_weight is None else float(target_weight) - current_weight
        self.target_events.append(
            TargetWeightTargetEvent(
                target_id=f"{self._target_version}-{instrument_id}-{len(self.target_events)}",
                target_date=self._target_date or trading_date,
                execute_date=trading_date,
                instrument_id=instrument_id,
                target_weight=float(target_weight),
                current_weight=current_weight,
                delta_weight=delta_weight,
                reason=reason,
                extra={"target_version": self._target_version},
            ),
        )

    def _record_order(
        self,
        trading_date: date,
        instrument_id: str,
        side: str,
        quantity: int,
        target_weight: float,
        status: str,
        reason: str | None,
        order_id: str | None = None,
    ) -> None:
        self.order_events.append(
            TargetWeightOrderEvent(
                order_id=order_id or f"internal-{trading_date.isoformat()}-{instrument_id}-{len(self.order_events)}",
                trading_date=trading_date,
                instrument_id=instrument_id,
                side=side,
                quantity=int(quantity),
                target_weight=float(target_weight),
                status=status,
                reason=reason,
                extra={"target_version": self._target_version},
            ),
        )


_INSUFFICIENT_FUNDS_MARKERS = ("260200", "\u53ef\u7528\u8d44\u91d1\u4e0d\u8db3", "\u8d44\u91d1\u4e0d\u8db3", "insufficient")
_SELLABLE_POSITION_MARKERS = (
    "sellable volume",
    "sellable position",
    "can_use_volume",
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


def target_version(target_date: date, weights: dict[str, float], reason: str) -> str:
    total = sum(weights.values())
    return f"{target_date.isoformat()}-{reason}-{len(weights)}-{total:.8f}"
