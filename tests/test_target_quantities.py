from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from decimal import Decimal

import pandas as pd

from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.identifiers import InstrumentId

from strategies.target_quantities import TargetQuantityStrategyConfig
from strategies.target_quantities import TargetQuantityStrategy
from strategies.target_quantities import TodayFillSnapshot
from strategies.target_quantities import NotionalOrderSplitter
from strategies.pricing import OpenOffsetBuyPriceStrategy
from strategies.pricing import OpenOffsetSellPriceStrategy


INST_A = InstrumentId.from_str("000001.SZ.QMT")
INST_B = InstrumentId.from_str("000002.SZ.QMT")
INST_C = InstrumentId.from_str("000003.SZ.QMT")


def china_ts_ns(value: str) -> int:
    return pd.Timestamp(value, tz="Asia/Shanghai").tz_convert("UTC").value


class FakeMoney:
    def __init__(self, value: Decimal | str) -> None:
        self.value = Decimal(str(value))

    def as_decimal(self) -> Decimal:
        return self.value


class FakeAccountEvent:
    def __init__(self, info: dict | None = None) -> None:
        self.info = info or {}


class FakeAccount:
    def __init__(self, free_cash: Decimal | str, info: dict | None = None) -> None:
        self.free_cash = Decimal(str(free_cash))
        self.last_event = FakeAccountEvent(info)

    def balance_free(self) -> FakeMoney:
        return FakeMoney(self.free_cash)


class FakePortfolio:
    def __init__(
        self,
        positions: dict[InstrumentId, Decimal] | None = None,
        equity: Decimal | str = "1000000",
        free_cash: Decimal | str = "1000000",
        account_info: dict | None = None,
    ) -> None:
        self.positions = positions or {}
        self.equity_value = Decimal(str(equity))
        self.account_value = FakeAccount(free_cash, account_info)
        self.has_account = True

    def net_position(self, instrument_id: InstrumentId) -> Decimal:
        return self.positions.get(instrument_id, Decimal("0"))

    def equity(self, **_kwargs):
        return {"CNY": FakeMoney(self.equity_value)}

    def account(self, **_kwargs) -> FakeAccount:
        if not self.has_account:
            raise RuntimeError("missing account")
        return self.account_value


class FakeInstrument:
    def __init__(
        self,
        instrument_id: InstrumentId,
        lot_size: Decimal | str = "100",
        price_increment: Decimal | str = "0.01",
        fields: dict | None = None,
    ) -> None:
        self.id = instrument_id
        self.raw_symbol = str(instrument_id).removesuffix(".QMT")
        self.lot_size = Decimal(str(lot_size))
        self.price_increment = Decimal(str(price_increment))
        self.info = {"fields": fields or {}}

    def make_qty(self, value):
        return Decimal(str(value))

    def make_price(self, value):
        return Decimal(str(value)).quantize(Decimal("0.01"))


@dataclass
class FakePosition:
    instrument_id: InstrumentId
    quantity: Decimal
    avg_px_open: Decimal = Decimal("10")
    is_long: bool = True


@dataclass
class FakeOrder:
    client_order_id: str
    instrument_id: InstrumentId
    side: OrderSide
    quantity: Decimal = Decimal("0")
    price: Decimal | None = None
    status: OrderStatus = OrderStatus.INITIALIZED
    is_pending_cancel: bool = False
    ts_last: int = 0
    events: list = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FakeFill:
    instrument_id: InstrumentId
    order_side: OrderSide
    last_qty: Decimal
    ts_event: int


class FakeOrderFactory:
    def __init__(self) -> None:
        self.count = 0

    def limit(self, instrument_id, order_side, quantity, price) -> FakeOrder:
        self.count += 1
        return FakeOrder(
            client_order_id=f"O-{self.count}",
            instrument_id=instrument_id,
            side=order_side,
            quantity=quantity,
            price=price,
        )

    def market(self, instrument_id, order_side, quantity) -> FakeOrder:
        self.count += 1
        return FakeOrder(
            client_order_id=f"O-{self.count}",
            instrument_id=instrument_id,
            side=order_side,
            quantity=quantity,
        )


class FakeCache:
    def __init__(
        self,
        instruments: dict[InstrumentId, FakeInstrument],
        portfolio: FakePortfolio,
    ) -> None:
        self.instruments = instruments
        self.portfolio = portfolio
        self.open_orders: list[FakeOrder] = []
        self.all_orders: list[FakeOrder] = []

    def instrument(self, instrument_id: InstrumentId):
        return self.instruments.get(instrument_id)

    def quote_tick(self, _instrument_id: InstrumentId):
        return None

    def order_book(self, _instrument_id: InstrumentId):
        return None

    def orders_open(self, **kwargs):
        return self._filter_orders(self.open_orders, **kwargs)

    def orders_inflight(self, **kwargs):
        return [
            order
            for order in self._filter_orders(self.all_orders, **kwargs)
            if order.status in {OrderStatus.SUBMITTED, OrderStatus.PENDING_CANCEL, OrderStatus.PENDING_UPDATE}
        ]

    def orders(self, **kwargs):
        return self._filter_orders(self.all_orders, **kwargs)

    @staticmethod
    def _filter_orders(orders: list[FakeOrder], **kwargs):
        instrument_id = kwargs.get("instrument_id")
        side = kwargs.get("side")
        result = list(orders)
        if instrument_id is not None:
            result = [order for order in result if order.instrument_id == instrument_id]
        if side is not None and side != OrderSide.NO_ORDER_SIDE:
            result = [order for order in result if order.side == side]
        return result

    def positions_open(self, **_kwargs):
        return [
            FakePosition(instrument_id=instrument_id, quantity=quantity)
            for instrument_id, quantity in self.portfolio.positions.items()
            if quantity > 0
        ]

    def accounts(self):
        return [self.portfolio.account_value]


class FakeClock:
    def __init__(self) -> None:
        self.now = pd.Timestamp("2026-07-02 02:00:00", tz="UTC")
        self.ts = 1_000_000_000
        self.time_alerts: list[dict] = []
        self.timers: list[dict] = []

    def timestamp_ns(self) -> int:
        self.ts += 1_000_000_000
        return self.ts

    def utc_now(self):
        return self.now

    def set_time_alert(self, **kwargs) -> None:
        self.time_alerts.append(kwargs)

    def set_timer(self, **kwargs) -> None:
        self.timers.append(kwargs)


class FakeLog:
    def __init__(self) -> None:
        self.infos: list[tuple] = []
        self.warnings: list[tuple] = []

    def info(self, *args, **kwargs) -> None:
        self.infos.append((args, kwargs))

    def warning(self, *args, **kwargs) -> None:
        self.warnings.append((args, kwargs))


class TestableTargetQuantityStrategy:
    _UP_LIMIT_KEYS = TargetQuantityStrategy._UP_LIMIT_KEYS
    _DOWN_LIMIT_KEYS = TargetQuantityStrategy._DOWN_LIMIT_KEYS
    _PRE_OPEN_RECONCILE_ALERT = TargetQuantityStrategy._PRE_OPEN_RECONCILE_ALERT
    _TERMINAL_ORDER_STATUSES = TargetQuantityStrategy._TERMINAL_ORDER_STATUSES
    on_start = TargetQuantityStrategy.on_start
    refresh_target_instruments = TargetQuantityStrategy.refresh_target_instruments
    _subscribe_market_data = TargetQuantityStrategy._subscribe_market_data
    _unsubscribe_market_data = TargetQuantityStrategy._unsubscribe_market_data
    _refresh_order_book_depth_subscriptions = TargetQuantityStrategy._refresh_order_book_depth_subscriptions
    update_target_quantities = TargetQuantityStrategy.update_target_quantities
    _target_quantities_log_detail = staticmethod(TargetQuantityStrategy._target_quantities_log_detail)
    on_order_book_depth = TargetQuantityStrategy.on_order_book_depth
    on_trade_tick = TargetQuantityStrategy.on_trade_tick
    on_order_filled = TargetQuantityStrategy.on_order_filled
    on_order_denied = TargetQuantityStrategy.on_order_denied
    on_order_rejected = TargetQuantityStrategy.on_order_rejected
    _handle_order_not_accepted = TargetQuantityStrategy._handle_order_not_accepted
    _converge_to_target = TargetQuantityStrategy._converge_to_target
    _converge_to_target_locked = TargetQuantityStrategy._converge_to_target_locked
    _order_book_depth_logging_enabled = TargetQuantityStrategy._order_book_depth_logging_enabled
    _should_log_sample = staticmethod(TargetQuantityStrategy._should_log_sample)
    _on_converge_timer = TargetQuantityStrategy._on_converge_timer
    _desired_quantities = TargetQuantityStrategy._desired_quantities
    _held_instrument_ids = TargetQuantityStrategy._held_instrument_ids
    _log_convergence_summary = TargetQuantityStrategy._log_convergence_summary
    _instrument_list_sample = staticmethod(TargetQuantityStrategy._instrument_list_sample)
    _refresh_symbol_freezes = TargetQuantityStrategy._refresh_symbol_freezes
    _price_limit_reason = TargetQuantityStrategy._price_limit_reason
    _target_achieved = TargetQuantityStrategy._target_achieved
    _reconcile_unfilled_orders = TargetQuantityStrategy._reconcile_unfilled_orders
    _submit_buys_within_cash = TargetQuantityStrategy._submit_buys_within_cash
    _target_order_intent = TargetQuantityStrategy._target_order_intent
    _order_slices = TargetQuantityStrategy._order_slices
    _estimated_order_cost = staticmethod(TargetQuantityStrategy._estimated_order_cost)
    _estimated_buy_cost = TargetQuantityStrategy._estimated_buy_cost
    _submit_target_quantity = TargetQuantityStrategy._submit_target_quantity
    _submit_full_exit = TargetQuantityStrategy._submit_full_exit
    _submit_order_quantity = TargetQuantityStrategy._submit_order_quantity
    _clamp_sell_quantity = TargetQuantityStrategy._clamp_sell_quantity
    _subscribe_execution_mass_status = TargetQuantityStrategy._subscribe_execution_mass_status
    _today_fill_snapshot = TargetQuantityStrategy._today_fill_snapshot
    _event_trading_date = TargetQuantityStrategy._event_trading_date
    _is_reconciliation_order = staticmethod(TargetQuantityStrategy._is_reconciliation_order)
    _open_sell_quantity = TargetQuantityStrategy._open_sell_quantity
    _open_buy_order_notional = TargetQuantityStrategy._open_buy_order_notional
    _decimal_quantity = staticmethod(TargetQuantityStrategy._decimal_quantity)
    _track_submitted_order = TargetQuantityStrategy._track_submitted_order
    _forget_submitted_order = TargetQuantityStrategy._forget_submitted_order
    _active_orders = TargetQuantityStrategy._active_orders
    _is_active_order = TargetQuantityStrategy._is_active_order
    _limit_price = TargetQuantityStrategy._limit_price
    _build_price_context = TargetQuantityStrategy._build_price_context
    _quote_snapshot = TargetQuantityStrategy._quote_snapshot
    _book_snapshot = TargetQuantityStrategy._book_snapshot
    _ladder_levels = staticmethod(TargetQuantityStrategy._ladder_levels)
    _at_buy_price_cap = TargetQuantityStrategy._at_buy_price_cap
    _target_quantity = TargetQuantityStrategy._target_quantity
    _portfolio_value = TargetQuantityStrategy._portfolio_value
    _nautilus_portfolio_equity = TargetQuantityStrategy._nautilus_portfolio_equity
    _free_cash = TargetQuantityStrategy._free_cash
    _nautilus_free_cash = TargetQuantityStrategy._nautilus_free_cash
    _log_account_sizing_snapshot = TargetQuantityStrategy._log_account_sizing_snapshot
    _broker_account_decimal = TargetQuantityStrategy._broker_account_decimal
    _broker_accounts = TargetQuantityStrategy._broker_accounts
    _account_info = staticmethod(TargetQuantityStrategy._account_info)
    _current_quantity = TargetQuantityStrategy._current_quantity
    _current_weight = TargetQuantityStrategy._current_weight
    _open_order_instruments = TargetQuantityStrategy._open_order_instruments
    _price_limits = TargetQuantityStrategy._price_limits
    _at_price_limit = TargetQuantityStrategy._at_price_limit
    _target_side = TargetQuantityStrategy._target_side
    _stop_time_reached = TargetQuantityStrategy._stop_time_reached
    _within_trading_window = TargetQuantityStrategy._within_trading_window
    _within_trading_time = TargetQuantityStrategy._within_trading_time
    _within_pre_open_quote_log_window = TargetQuantityStrategy._within_pre_open_quote_log_window
    _quote_tick_window_gate_enabled = TargetQuantityStrategy._quote_tick_window_gate_enabled
    _note_quote_tick = TargetQuantityStrategy._note_quote_tick
    on_quote_tick = TargetQuantityStrategy.on_quote_tick
    _subscribe_quote_tick_window_probes = TargetQuantityStrategy._subscribe_quote_tick_window_probes
    investable_total_asset = TargetQuantityStrategy.investable_total_asset
    _clock_date = TargetQuantityStrategy._clock_date
    _record_target = TargetQuantityStrategy._record_target
    _record_order = TargetQuantityStrategy._record_order
    configure_pre_open_reconciliation = TargetQuantityStrategy.configure_pre_open_reconciliation
    _parse_hh_mm = staticmethod(TargetQuantityStrategy._parse_hh_mm)
    _next_daily_time = TargetQuantityStrategy._next_daily_time
    _schedule_pre_open_reconcile = TargetQuantityStrategy._schedule_pre_open_reconcile
    _on_pre_open_reconcile_timer = TargetQuantityStrategy._on_pre_open_reconcile_timer
    _run_pre_open_reconcile = TargetQuantityStrategy._run_pre_open_reconcile
    _schedule_on_loop = TargetQuantityStrategy._schedule_on_loop
    _on_pre_open_reconcile_done = TargetQuantityStrategy._on_pre_open_reconcile_done
    _log_pre_open_reconcile_result = TargetQuantityStrategy._log_pre_open_reconcile_result
    _ensure_pricing_date = TargetQuantityStrategy._ensure_pricing_date
    _update_price_state = TargetQuantityStrategy._update_price_state
    _seed_open_prices_from_last_close = TargetQuantityStrategy._seed_open_prices_from_last_close
    _set_authoritative_open = TargetQuantityStrategy._set_authoritative_open
    _apply_full_tick = TargetQuantityStrategy._apply_full_tick
    _full_tick_open = staticmethod(TargetQuantityStrategy._full_tick_open)
    configure_full_tick_source = TargetQuantityStrategy.configure_full_tick_source
    _start_full_tick_refresh = TargetQuantityStrategy._start_full_tick_refresh
    _schedule_full_tick_prefetch = TargetQuantityStrategy._schedule_full_tick_prefetch
    _on_full_tick_prefetch_timer = TargetQuantityStrategy._on_full_tick_prefetch_timer
    _on_full_tick_refresh_timer = TargetQuantityStrategy._on_full_tick_refresh_timer
    _run_full_tick_fetch = TargetQuantityStrategy._run_full_tick_fetch
    _on_full_tick_fetch_done = TargetQuantityStrategy._on_full_tick_fetch_done

    def request_instrument(self, instrument_id) -> None:
        self.requested_instruments.append(instrument_id)

    def subscribe_bars(self, bar_type) -> None:
        self.subscribed_bars.append(bar_type)

    def subscribe_quote_ticks(self, instrument_id) -> None:
        self.subscribed_quote_ticks.append(instrument_id)

    def subscribe_trade_ticks(self, instrument_id) -> None:
        self.subscribed_trade_ticks.append(instrument_id)

    def subscribe_order_book_depth(self, instrument_id, **kwargs) -> None:
        self.subscribed_order_book_depths.append((instrument_id, kwargs))

    def unsubscribe_bars(self, bar_type) -> None:
        self.unsubscribed_bars.append(bar_type)

    def unsubscribe_order_book_depth(self, instrument_id) -> None:
        self.unsubscribed_order_book_depths.append(instrument_id)

    def submit_order(self, order) -> None:
        self.submitted_orders.append(order)
        order.status = OrderStatus.INITIALIZED
        self.cache.all_orders.append(order)
        if self.submit_orders_to_cache:
            self.cache.open_orders.append(order)

    def cancel_order(self, order) -> None:
        self.canceled_orders.append(order)
        order.status = OrderStatus.CANCELED
        self.cache.open_orders = [
            existing
            for existing in self.cache.open_orders
            if existing.client_order_id != order.client_order_id
        ]

class TargetQuantityStrategyTest(unittest.TestCase):
    def make_strategy(
        self,
        *,
        positions: dict[InstrumentId, Decimal] | None = None,
        equity: Decimal | str = "1000000",
        free_cash: Decimal | str = "1000000",
        prices: dict[InstrumentId, float] | None = None,
        open_prices: dict[InstrumentId, float] | None = None,
        fields: dict[InstrumentId, dict] | None = None,
        target_cash_buffer_percent: float = 0.05,
        order_slice_notional: Decimal | str = "300000",
        require_account_cash: bool = True,
        account_info: dict | None = None,
    ) -> TestableTargetQuantityStrategy:
        instruments = {
            instrument_id: FakeInstrument(instrument_id, fields=(fields or {}).get(instrument_id))
            for instrument_id in (INST_A, INST_B, INST_C)
        }
        strategy = TestableTargetQuantityStrategy()
        strategy.config = TargetQuantityStrategyConfig(
            instrument_ids=[INST_A, INST_B, INST_C],
            bar_types={},
            initial_cash=Decimal(str(equity)),
            target_cash_buffer_percent=target_cash_buffer_percent,
            cash_buffer_percent=0.0,
            unfilled_timeout_secs=1.0,
            stop_time=None,
            order_slice_notional=Decimal(str(order_slice_notional)),
            require_account_cash=require_account_cash,
        )
        portfolio = FakePortfolio(
            positions=positions,
            equity=equity,
            free_cash=free_cash,
            account_info=account_info,
        )
        strategy.cache = FakeCache(instruments, portfolio)
        strategy.portfolio = portfolio
        strategy.clock = FakeClock()
        strategy.order_factory = FakeOrderFactory()
        strategy.id = "TEST-001"
        strategy.log = FakeLog()
        strategy._instrument_ids = [INST_A, INST_B, INST_C]
        strategy._bar_types = {}
        strategy._target_quantities = {}
        strategy._target_date = None
        strategy._target_reason = "target_quantity"
        strategy._target_version = ""
        strategy._target_total_asset = None
        strategy._achieved_versions = set()
        strategy._frozen_instruments = {}
        strategy._deferred_buys = {}
        strategy._rejected_order_ids = set()
        strategy._insufficient_funds = set()
        strategy._order_submit_ts = {}
        strategy._order_target_qty = {}
        strategy._order_target_versions = {}
        strategy._order_splitter = NotionalOrderSplitter(Decimal(str(order_slice_notional)))
        strategy._buy_pricer = OpenOffsetBuyPriceStrategy(
            offset_bps=strategy.config.buy_offset_bps,
            max_price_bps=strategy.config.buy_max_price_bps,
            cancel_threshold=strategy.config.buy_cancel_threshold,
        )
        strategy._sell_pricer = OpenOffsetSellPriceStrategy(
            offset_bps=strategy.config.sell_offset_bps,
            cancel_threshold=strategy.config.sell_cancel_threshold,
        )
        initial_prices = prices or {INST_A: 10.0, INST_B: 20.0, INST_C: 25.0}
        initial_open_prices = initial_prices if open_prices is None else open_prices
        strategy._today_open = {
            str(instrument_id): price
            for instrument_id, price in initial_open_prices.items()
        }
        strategy._authoritative_open = set()
        strategy._full_tick_source = None
        strategy._full_tick_prefetch_time = None
        strategy._full_tick_task = None
        strategy._depth_books = {}
        strategy._subscribed_order_book_depth_instruments = set()
        strategy._sleep_calls = []
        strategy._sleep = lambda seconds: strategy._sleep_calls.append(seconds)
        strategy._cancel_count_buy = {}
        strategy._cancel_count_sell = {}
        strategy._pricing_date = None
        strategy._quote_tick_window_probe_ids = set()
        strategy._subscribed_quote_tick_probe_instruments = set()
        # Mark the trading window as already unlocked by a live quote tick (the runtime
        # state during a trading session). The window gate now requires a tick when
        # subscribe_quote_ticks is on; these tests exercise convergence, not the gate.
        strategy._quote_tick_window_date = pd.Timestamp(
            strategy.clock.utc_now(),
        ).tz_convert(strategy.config.timezone_name).date()
        strategy._convergence_suspended = False
        strategy._converge_lock = threading.Lock()
        strategy._pre_open_reconcile = lambda timeout_secs: True
        strategy._pre_open_reconcile_time = (9, 15)
        strategy._pre_open_reconcile_timeout_secs = 30.0
        strategy._pre_open_reconcile_task = None
        strategy._loop = None
        strategy._sellable_exhausted = {}
        strategy._venue_sellable = {}
        strategy._venue_sellable_ts = 0
        strategy._last_account_sizing_snapshot = None
        strategy.target_events = []
        strategy.order_events = []
        strategy.requested_instruments = []
        strategy.subscribed_bars = []
        strategy.subscribed_quote_ticks = []
        strategy.subscribed_trade_ticks = []
        strategy.subscribed_order_book_depths = []
        strategy.unsubscribed_bars = []
        strategy.unsubscribed_order_book_depths = []
        strategy.submitted_orders = []
        strategy.submit_orders_to_cache = True
        strategy.canceled_orders = []
        strategy._last_close = {
            str(instrument_id): price
            for instrument_id, price in initial_prices.items()
        }
        return strategy

    def test_on_start_subscribes_ticks_for_configured_instruments(self) -> None:
        bar_type = BarType.from_str(f"{INST_A}-1-MINUTE-LAST-EXTERNAL")
        strategy = self.make_strategy()
        strategy.config = TargetQuantityStrategyConfig(
            instrument_ids=[INST_A],
            bar_types={str(INST_A): bar_type},
            subscribe_bars=False,
            unfilled_timeout_secs=1.0,
            resubmit_check_interval_secs=10.0,
        )
        strategy._bar_types = {str(INST_A): bar_type}

        strategy.on_start()

        self.assertEqual(strategy.subscribed_quote_ticks, [INST_A])
        self.assertEqual(strategy.subscribed_trade_ticks, [INST_A])

    def test_on_start_can_disable_market_data_subscriptions(self) -> None:
        bar_type = BarType.from_str(f"{INST_A}-1-MINUTE-LAST-EXTERNAL")
        strategy = self.make_strategy()
        strategy.config = TargetQuantityStrategyConfig(
            instrument_ids=[INST_A],
            bar_types={str(INST_A): bar_type},
            subscribe_bars=False,
            subscribe_quote_ticks=False,
            subscribe_trade_ticks=False,
            subscribe_order_book_depth=False,
            unfilled_timeout_secs=1.0,
            resubmit_check_interval_secs=10.0,
        )
        strategy._bar_types = {str(INST_A): bar_type}

        strategy.on_start()

        self.assertEqual(strategy.subscribed_quote_ticks, [])
        self.assertEqual(strategy.subscribed_trade_ticks, [])
        self.assertEqual(strategy.subscribed_order_book_depths, [])
        self.assertEqual(len(strategy.clock.timers), 1)

    def test_on_start_subscribes_quote_tick_window_probes_when_quote_ticks_disabled(self) -> None:
        strategy = self.make_strategy()
        strategy.config = TargetQuantityStrategyConfig(
            instrument_ids=[INST_A, INST_B, INST_C],
            bar_types={},
            subscribe_bars=False,
            subscribe_quote_ticks=False,
            subscribe_trade_ticks=False,
            quote_tick_window_probe_instrument_ids=(INST_A, INST_B),
            unfilled_timeout_secs=1.0,
            resubmit_check_interval_secs=10.0,
        )
        strategy._quote_tick_window_probe_ids = {str(INST_A), str(INST_B)}

        strategy.on_start()

        self.assertEqual(strategy.subscribed_quote_ticks, [INST_A, INST_B])
        self.assertEqual(strategy.subscribed_trade_ticks, [])

    def test_refresh_target_instruments_subscribes_ticks_for_new_instruments(self) -> None:
        bar_type = BarType.from_str(f"{INST_B}-1-MINUTE-LAST-EXTERNAL")
        strategy = self.make_strategy()
        strategy.config = TargetQuantityStrategyConfig(
            instrument_ids=[INST_A, INST_B, INST_C],
            bar_types={},
            subscribe_bars=False,
        )

        strategy.refresh_target_instruments([INST_B], {str(INST_B): bar_type})

        self.assertEqual(strategy.requested_instruments, [INST_B])
        self.assertEqual(strategy.subscribed_quote_ticks, [INST_B])
        self.assertEqual(strategy.subscribed_trade_ticks, [INST_B])

    def test_refresh_target_instruments_respects_disabled_subscriptions(self) -> None:
        bar_type = BarType.from_str(f"{INST_B}-1-MINUTE-LAST-EXTERNAL")
        strategy = self.make_strategy()
        strategy.config = TargetQuantityStrategyConfig(
            instrument_ids=[INST_A],
            bar_types={},
            subscribe_bars=False,
            subscribe_quote_ticks=False,
            subscribe_trade_ticks=False,
            subscribe_order_book_depth=False,
        )

        strategy.refresh_target_instruments([INST_B], {str(INST_B): bar_type})

        self.assertEqual(strategy.requested_instruments, [INST_B])
        self.assertEqual(strategy.subscribed_quote_ticks, [])
        self.assertEqual(strategy.subscribed_trade_ticks, [])
        self.assertEqual(strategy.subscribed_order_book_depths, [])

    def test_seed_open_prices_from_last_close_resets_new_day_counts(self) -> None:
        strategy = self.make_strategy(prices={INST_A: 10.0, INST_B: 20.0})
        strategy.config = TargetQuantityStrategyConfig(
            instrument_ids=[INST_A, INST_B],
            bar_types={},
            seed_open_from_last_close=True,
        )
        strategy._pricing_date = date(2026, 7, 1)
        strategy._today_open = {str(INST_A): 9.0}
        strategy._cancel_count_buy = {str(INST_A): 2}
        strategy._cancel_count_sell = {str(INST_A): 1}

        strategy._seed_open_prices_from_last_close(date(2026, 7, 2))

        self.assertEqual(strategy._today_open, {str(INST_A): 10.0, str(INST_B): 20.0})
        self.assertEqual(strategy._cancel_count_buy, {})
        self.assertEqual(strategy._cancel_count_sell, {})

    def test_trade_tick_logging_uses_sample_rate_like_quote_ticks(self) -> None:
        strategy = self.make_strategy()
        strategy.config = TargetQuantityStrategyConfig(
            instrument_ids=[INST_A],
            bar_types={},
            trade_tick_log_sample_rate=1.0,
        )
        tick = type(
            "Tick",
            (),
            {
                "instrument_id": INST_A,
                "price": Decimal("10.10"),
                "size": Decimal("100"),
                "aggressor_side": "BUYER",
                "trade_id": "T-1",
                "ts_event": 1,
                "ts_init": 2,
            },
        )()

        strategy.on_trade_tick(tick)

        self.assertEqual(len(strategy.log.infos), 1)
        self.assertIn("Trade tick sample", strategy.log.infos[0][0][0])

    def test_quote_tick_0925_logs_but_does_not_unlock_window(self) -> None:
        strategy = self.make_strategy()
        strategy.config = TargetQuantityStrategyConfig(
            instrument_ids=[INST_A],
            bar_types={},
            subscribe_quote_ticks=False,
            quote_tick_window_probe_instrument_ids=(INST_A,),
            trading_windows="09:29-11:30,13:00-14:55",
        )
        strategy._quote_tick_window_probe_ids = {str(INST_A)}
        strategy._quote_tick_window_date = None
        strategy.clock.now = pd.Timestamp("2026-07-02 01:25:00", tz="UTC")
        tick = type(
            "Tick",
            (),
            {
                "instrument_id": INST_A,
                "bid_price": Decimal("10.00"),
                "bid_size": Decimal("100"),
                "ask_price": Decimal("10.01"),
                "ask_size": Decimal("100"),
                "ts_event": 1,
                "ts_init": 2,
            },
        )()

        strategy.on_quote_tick(tick)

        self.assertIsNone(strategy._quote_tick_window_date)
        self.assertEqual(len(strategy.log.infos), 1)
        self.assertIn("forced pre-open diagnostics", strategy.log.infos[0][0][0])

    def test_quote_tick_0929_unlocks_window(self) -> None:
        strategy = self.make_strategy()
        strategy.config = TargetQuantityStrategyConfig(
            instrument_ids=[INST_A],
            bar_types={},
            subscribe_quote_ticks=False,
            quote_tick_window_probe_instrument_ids=(INST_A,),
            trading_windows="09:29-11:30,13:00-14:55",
        )
        strategy._quote_tick_window_probe_ids = {str(INST_A)}
        strategy._quote_tick_window_date = None
        strategy.clock.now = pd.Timestamp("2026-07-02 01:29:00", tz="UTC")
        tick = type(
            "Tick",
            (),
            {
                "instrument_id": INST_A,
                "bid_price": Decimal("10.00"),
                "bid_size": Decimal("100"),
                "ask_price": Decimal("10.01"),
                "ask_size": Decimal("100"),
                "ts_event": 1,
                "ts_init": 2,
            },
        )()

        strategy.on_quote_tick(tick)

        self.assertEqual(strategy._quote_tick_window_date, date(2026, 7, 2))
        self.assertTrue(strategy._within_trading_window())

    def test_desired_quantities_exit_non_targets_by_default(self) -> None:
        strategy = self.make_strategy(positions={INST_A: Decimal("100"), INST_C: Decimal("200")})
        strategy.update_target_quantities({INST_A: 500}, date(2026, 7, 2), "test")

        desired = strategy._desired_quantities()

        self.assertEqual(desired[str(INST_A)], Decimal("500"))
        self.assertEqual(desired[str(INST_C)], Decimal("0"))

    def test_update_target_replaces_deferred_buy_intent(self) -> None:
        strategy = self.make_strategy(free_cash="0")
        strategy.update_target_quantities({INST_A: 500}, date(2026, 7, 2), "first")
        self.assertEqual(strategy._deferred_buys, {str(INST_A): Decimal("500")})

        strategy.update_target_quantities({INST_B: 400}, date(2026, 7, 2), "second")

        self.assertNotIn(str(INST_A), strategy._deferred_buys)
        self.assertEqual(strategy._deferred_buys, {str(INST_B): Decimal("400")})
        self.assertEqual(strategy._target_quantities, {str(INST_B): Decimal("400")})

    def test_update_target_refreshes_depth_subscriptions_from_targets_and_holdings(self) -> None:
        strategy = self.make_strategy(positions={INST_B: Decimal("100")})
        strategy._subscribed_order_book_depth_instruments = {str(INST_C)}

        strategy.update_target_quantities({INST_A: 500}, date(2026, 7, 2), "depth_refresh")

        self.assertEqual(strategy.unsubscribed_order_book_depths, [INST_C])
        self.assertEqual(
            strategy.subscribed_order_book_depths,
            [
                (INST_A, {"book_type": BookType.L2_MBP, "depth": 10}),
                (INST_B, {"book_type": BookType.L2_MBP, "depth": 10}),
            ],
        )
        self.assertEqual(
            strategy._subscribed_order_book_depth_instruments,
            {str(INST_A), str(INST_B)},
        )
        self.assertEqual(strategy._sleep_calls, [10.0])

    def test_update_target_refreshes_depth_subscriptions_even_when_target_unchanged(self) -> None:
        strategy = self.make_strategy()

        strategy.update_target_quantities({INST_A: 500}, date(2026, 7, 2), "depth_refresh")
        strategy.subscribed_order_book_depths = []
        strategy.unsubscribed_order_book_depths = []
        strategy._sleep_calls = []

        strategy.update_target_quantities({INST_A: 500}, date(2026, 7, 2), "depth_refresh")

        self.assertEqual(strategy.unsubscribed_order_book_depths, [INST_A])
        self.assertEqual(
            strategy.subscribed_order_book_depths,
            [(INST_A, {"book_type": BookType.L2_MBP, "depth": 10})],
        )
        self.assertEqual(strategy._sleep_calls, [10.0])

    def test_update_target_logs_target_quantity_detail(self) -> None:
        strategy = self.make_strategy()

        strategy.update_target_quantities({INST_B: 200, INST_A: 100}, date(2026, 7, 2), "detail")

        accepted_logs = [
            args[0]
            for args, _kwargs in strategy.log.infos
            if args and str(args[0]).startswith("accepted target quantities version=")
        ]
        self.assertEqual(len(accepted_logs), 1)
        self.assertIn("detail=[000001.SZ.QMT=100, 000002.SZ.QMT=200]", accepted_logs[0])

    def test_convergence_submits_sell_before_cash_gated_buy(self) -> None:
        strategy = self.make_strategy(
            positions={INST_C: Decimal("100")},
            free_cash="1000",
            prices={INST_A: 10.0, INST_C: 25.0},
        )

        strategy.update_target_quantities({INST_A: 50000}, date(2026, 7, 2), "rebalance")

        self.assertGreaterEqual(len(strategy.submitted_orders), 1)
        self.assertEqual(strategy.submitted_orders[0].instrument_id, INST_C)
        self.assertEqual(strategy.submitted_orders[0].side, OrderSide.SELL)
        self.assertEqual(strategy._deferred_buys, {str(INST_A): Decimal("50000")})

    def test_convergence_summary_logs_missing_price_for_buy_candidate(self) -> None:
        strategy = self.make_strategy(
            free_cash="1000000",
            prices={INST_A: 10.0},
            open_prices={},
        )

        strategy.update_target_quantities({INST_A: 500}, date(2026, 7, 2), "missing_price")

        summary_logs = [
            args[0]
            for args, _kwargs in strategy.log.infos
            if args and str(args[0]).startswith("Target convergence summary ")
        ]
        self.assertEqual(len(summary_logs), 1)
        self.assertIn("missing_price=1", summary_logs[0])
        self.assertIn(f"missing_price_instruments=[{INST_A}]", summary_logs[0])
        self.assertIn("sell_targets=0 buy_targets=1", summary_logs[0])
        self.assertEqual(len(strategy.submitted_orders), 1)
        self.assertEqual(strategy.submitted_orders[0].side, OrderSide.BUY)

    def test_convergence_summary_logs_cash_gap_for_buy_candidates(self) -> None:
        strategy = self.make_strategy(
            free_cash="1000",
            equity="1000000",
            prices={INST_A: 10.0},
        )

        strategy.update_target_quantities({INST_A: 50000}, date(2026, 7, 2), "cash_gap")

        summary_logs = [
            args[0]
            for args, _kwargs in strategy.log.infos
            if args and str(args[0]).startswith("Target convergence summary ")
        ]
        self.assertEqual(len(summary_logs), 1)
        self.assertIn("sell_targets=0 buy_targets=1", summary_logs[0])
        self.assertIn("estimated_buy_cost=500000.0", summary_logs[0])
        self.assertIn("available_buy_cash=1000.0", summary_logs[0])
        self.assertIn("cash_gap=499000.0", summary_logs[0])

    def test_update_target_quantities_accepts_loaded_frozen_total_asset(self) -> None:
        strategy = self.make_strategy(
            positions={INST_A: Decimal("1000")},
            free_cash="1000000",
            equity="1000000",
            prices={INST_A: 10.0},
        )

        strategy.update_target_quantities(
            quantities={INST_A: Decimal("1000")},
            target_date=date(2026, 7, 2),
            reason="restart_frozen",
            total_asset=Decimal("500000"),
            version="frozen-v1",
        )

        self.assertEqual(strategy._target_quantities, {str(INST_A): Decimal("1000")})
        self.assertEqual(strategy._target_total_asset, Decimal("500000"))
        self.assertEqual(strategy.submitted_orders, [])

    def test_limit_up_freezes_only_affected_buy_symbol(self) -> None:
        strategy = self.make_strategy(
            free_cash="1000000",
            prices={INST_A: 10.0, INST_B: 20.0},
            fields={INST_A: {"UpStopPrice": 10.0}},
        )

        strategy.update_target_quantities(
            {INST_A: 30000, INST_B: 15000},
            date(2026, 7, 2),
            "limit_test",
        )

        self.assertEqual(strategy._frozen_instruments, {str(INST_A): "up_limit"})
        submitted_instruments = {order.instrument_id for order in strategy.submitted_orders}
        self.assertNotIn(INST_A, submitted_instruments)
        self.assertIn(INST_B, submitted_instruments)

    def test_achievement_accepts_exact_target_quantity(self) -> None:
        strategy = self.make_strategy(
            positions={INST_A: Decimal("95000")},
            equity="1000000",
            free_cash="50000",
            prices={INST_A: 10.0},
        )
        strategy._target_quantities = {str(INST_A): Decimal("95000")}
        strategy._target_version = "achieved"
        strategy._target_date = date(2026, 7, 2)

        self.assertTrue(strategy._target_achieved())

    def test_achievement_rejects_quantity_mismatch(self) -> None:
        strategy = self.make_strategy(
            positions={INST_A: Decimal("90000")},
            equity="1000000",
            free_cash="100000",
            prices={INST_A: 10.0},
        )
        strategy._target_quantities = {str(INST_A): Decimal("95000")}
        strategy._target_version = "not-achieved"
        strategy._target_date = date(2026, 7, 2)

        self.assertFalse(strategy._target_achieved())

    def test_order_slice_notional_combines_small_delta_into_one_order(self) -> None:
        strategy = self.make_strategy(
            free_cash="1000000",
            equity="1000000",
            prices={INST_A: 25.0},
        )

        strategy.update_target_quantities({INST_A: 1200}, date(2026, 7, 2), "small")

        self.assertEqual(len(strategy.submitted_orders), 1)
        self.assertEqual(strategy.submitted_orders[0].quantity, Decimal("1200"))

    def test_order_slice_notional_splits_large_delta_by_300k(self) -> None:
        strategy = self.make_strategy(
            free_cash="1000000",
            equity="1000000",
            prices={INST_A: 10.0},
        )

        strategy.update_target_quantities({INST_A: 95000}, date(2026, 7, 2), "large")

        self.assertEqual([order.quantity for order in strategy.submitted_orders], [
            Decimal("29900"),
            Decimal("29900"),
            Decimal("29900"),
            Decimal("5300"),
        ])

    def test_target_helpers_value_position_with_today_open_not_last_close(self) -> None:
        strategy = self.make_strategy(
            positions={INST_A: Decimal("7500")},
            free_cash="1000000",
            equity="1000000",
            prices={INST_A: 20.0},
            open_prices={INST_A: 10.0},
            order_slice_notional="1000000",
        )

        self.assertEqual(strategy._current_weight(str(INST_A)), 0.075)
        self.assertEqual(strategy._target_side(str(INST_A), Decimal("10000")), "buy")
        intent = strategy._target_order_intent(str(INST_A), Decimal("10000"))
        self.assertIsNotNone(intent)
        self.assertEqual(intent.side, OrderSide.BUY)
        self.assertEqual(intent.quantity, Decimal("2500"))
        self.assertEqual(strategy._estimated_buy_cost(str(INST_A), Decimal("10000")), Decimal("25000.0"))

    def test_live_sizing_prefers_broker_total_asset_over_cash_only_equity(self) -> None:
        strategy = self.make_strategy(
            positions={INST_A: Decimal("143100")},
            equity="3352261.79",
            free_cash="3352261.79",
            prices={INST_A: 3.35},
            open_prices={INST_A: 3.40},
            account_info={"total_asset": "10007406.36", "available_cash": "3352261.79"},
            order_slice_notional="1000000",
        )

        self.assertEqual(strategy._portfolio_value(), Decimal("10007406.36"))
        self.assertEqual(strategy._nautilus_portfolio_equity(), Decimal("3352261.79"))
        self.assertIsNone(strategy._target_side(str(INST_A), Decimal("143100")))

        strategy.update_target_quantities({INST_A: 143100}, date(2026, 7, 2), "broker_total_asset")

        self.assertEqual(strategy.submitted_orders, [])
        sizing_logs = [
            args[0]
            for args, _kwargs in strategy.log.infos
            if "Account sizing snapshot" in args[0]
        ]
        self.assertEqual(len(sizing_logs), 1)
        self.assertIn("value_source=account_state_info.total_asset", sizing_logs[0])
        self.assertIn("selected_portfolio_value=10007406.36", sizing_logs[0])
        self.assertIn("nautilus_portfolio_equity=3352261.79", sizing_logs[0])
        self.assertIn("broker_total_asset=10007406.36", sizing_logs[0])

    def test_broker_total_asset_allows_underweight_buy_when_portfolio_equity_is_cash_only(self) -> None:
        strategy = self.make_strategy(
            positions={INST_A: Decimal("100000")},
            equity="3352261.79",
            free_cash="3352261.79",
            prices={INST_A: 3.35},
            open_prices={INST_A: 3.40},
            account_info={"total_asset": "10007406.36", "cash": "3352261.79"},
            order_slice_notional="1000000",
        )

        self.assertEqual(strategy._target_side(str(INST_A), Decimal("147100")), "buy")

        strategy.update_target_quantities({INST_A: 147100}, date(2026, 7, 2), "broker_total_asset")

        self.assertEqual(len(strategy.submitted_orders), 1)
        self.assertEqual(strategy.submitted_orders[0].side, OrderSide.BUY)
        self.assertEqual(strategy.submitted_orders[0].quantity, Decimal("47100"))

    def test_buy_cash_gate_uses_each_slice_and_defers_unfunded_remainder(self) -> None:
        strategy = self.make_strategy(
            free_cash="350000",
            equity="1000000",
            prices={INST_A: 10.0},
        )

        strategy.update_target_quantities({INST_A: 95000}, date(2026, 7, 2), "partial")

        self.assertEqual([order.quantity for order in strategy.submitted_orders], [Decimal("29900")])
        self.assertEqual(strategy._deferred_buys, {str(INST_A): Decimal("95000")})

    def test_buy_cash_gate_reserves_open_buy_notional(self) -> None:
        strategy = self.make_strategy(
            free_cash="350000",
            equity="1000000",
            prices={INST_A: 10.0, INST_B: 10.0},
        )
        strategy.cache.open_orders.append(
            FakeOrder(
                client_order_id="B-1",
                instrument_id=INST_A,
                side=OrderSide.BUY,
                quantity=Decimal("29900"),
                price=Decimal("10"),
                status=OrderStatus.ACCEPTED,
            ),
        )

        strategy._submit_buys_within_cash(
            date(2026, 7, 2),
            {str(INST_B): Decimal("10000")},
            "cash_reserved",
        )

        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(strategy._deferred_buys, {str(INST_B): Decimal("10000")})

    def test_initialized_buy_in_cache_blocks_cache_lag_duplicate_convergence(self) -> None:
        trading_date = date(2026, 7, 2)
        strategy = self.make_strategy(
            free_cash="1000000",
            equity="1000000",
            prices={INST_A: 25.0},
        )
        strategy.submit_orders_to_cache = False

        strategy.update_target_quantities({INST_A: 1200}, trading_date, "cache_lag")
        strategy._converge_to_target(trading_date, "timer")

        self.assertEqual(len(strategy.submitted_orders), 1)
        self.assertEqual(strategy.submitted_orders[0].quantity, Decimal("1200"))
        self.assertEqual(strategy._open_order_instruments(), {str(INST_A)})

    def test_reentrant_convergence_is_skipped_while_in_progress(self) -> None:
        # Reproduces the duplicate-sell race: a second convergence trigger arrives on
        # another thread while the first pass is between reading open orders and having
        # its submitted order become visible. The just-submitted order is INITIALIZED and
        # not yet in the cache's open index, so without the lock the second pass would
        # re-submit the same full-exit. The non-blocking lock makes the reentrant pass
        # skip, so exactly one sell is submitted.
        trading_date = date(2026, 7, 2)
        strategy = self.make_strategy(
            positions={INST_A: Decimal("300")},
            equity="1000000",
            free_cash="0",
            prices={INST_A: 859.56},
        )
        # Submitted orders are NOT reflected in the open-orders view (INITIALIZED not
        # yet routed), exactly as during the live race window.
        strategy.submit_orders_to_cache = False

        reentered: list[int] = []
        original_submit = strategy.submit_order.__func__

        def submit_and_reenter(order) -> None:
            original_submit(strategy, order)
            # Simulate a concurrent timer/full-tick callback firing mid-submit. The
            # lock is already held by the outer pass, so this must be a no-op.
            before = len(strategy.submitted_orders)
            strategy._converge_to_target(trading_date, "reentrant")
            reentered.append(len(strategy.submitted_orders) - before)

        strategy.submit_order = submit_and_reenter

        strategy.update_target_quantities({INST_A: 0}, trading_date, "exit")

        self.assertEqual(len(strategy.submitted_orders), 1)
        self.assertEqual(strategy.submitted_orders[0].side, OrderSide.SELL)
        self.assertEqual(strategy.submitted_orders[0].quantity, Decimal("300"))
        # The reentrant pass ran but submitted nothing.
        self.assertEqual(reentered, [0])

    def test_quantity_delta_submits_tiny_residual_buy(self) -> None:
        strategy = self.make_strategy(
            positions={INST_A: Decimal("94800")},
            equity="1000000",
            free_cash="52000",
            prices={INST_A: 10.0},
        )

        strategy.update_target_quantities({INST_A: 95000}, date(2026, 7, 2), "tiny")

        self.assertEqual(len(strategy.submitted_orders), 1)
        self.assertEqual(strategy.submitted_orders[0].quantity, Decimal("200"))

    def test_update_target_does_not_converge_when_suspended_outside_window(self) -> None:
        strategy = self.make_strategy(free_cash="1000000")
        strategy._convergence_suspended = True

        strategy.update_target_quantities({INST_A: 500}, date(2026, 7, 2), "preopen")

        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(strategy._target_quantities, {str(INST_A): Decimal("500")})

    def test_update_target_does_not_submit_before_trading_window(self) -> None:
        strategy = self.make_strategy(free_cash="1000000")
        strategy.clock.now = pd.Timestamp("2026-07-02 01:16:00", tz="UTC")

        strategy.update_target_quantities({INST_A: 500}, date(2026, 7, 2), "preopen")

        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(strategy._target_quantities, {str(INST_A): Decimal("500")})

    def test_order_fill_releases_only_filled_symbol_insufficient_funds_backoff(self) -> None:
        strategy = self.make_strategy()
        strategy._insufficient_funds = {str(INST_A), str(INST_B)}
        event = type("FillEvent", (), {"client_order_id": "O-1", "instrument_id": INST_A})()

        strategy.on_order_filled(event)

        self.assertEqual(strategy._insufficient_funds, {str(INST_B)})

    def test_missing_account_cash_defers_buys_without_using_equity_as_cash(self) -> None:
        strategy = self.make_strategy(require_account_cash=True)
        strategy.portfolio.has_account = False

        strategy.update_target_quantities({INST_A: 50000}, date(2026, 7, 2), "missing_cash")

        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(strategy._deferred_buys, {str(INST_A): Decimal("50000")})

    def test_sell_clamps_to_net_position_less_today_buys(self) -> None:
        trading_date = date(2026, 7, 3)
        strategy = self.make_strategy(
            positions={INST_A: Decimal("31900")},
            prices={INST_A: 10.0},
        )
        strategy.cache.all_orders.append(
            FakeOrder(
                client_order_id="B-1",
                instrument_id=INST_A,
                side=OrderSide.BUY,
                quantity=Decimal("31500"),
                status=OrderStatus.FILLED,
                events=[
                    FakeFill(
                        instrument_id=INST_A,
                        order_side=OrderSide.BUY,
                        last_qty=Decimal("31500"),
                        ts_event=china_ts_ns("2026-07-03 10:00:00"),
                    ),
                ],
            ),
        )

        submitted = strategy._submit_full_exit(
            trading_date,
            INST_A,
            strategy.cache.instrument(INST_A),
            Decimal("31900"),
            "rebalance",
        )

        self.assertTrue(submitted)
        self.assertEqual([order.quantity for order in strategy.submitted_orders], [Decimal("400")])
        self.assertEqual(strategy._sellable_exhausted, {})

    def test_sell_defers_when_today_buys_exhaust_sellable_quantity(self) -> None:
        trading_date = date(2026, 7, 3)
        strategy = self.make_strategy(
            positions={INST_A: Decimal("31500")},
            prices={INST_A: 10.0},
        )
        strategy.cache.all_orders.append(
            FakeOrder(
                client_order_id="B-1",
                instrument_id=INST_A,
                side=OrderSide.BUY,
                quantity=Decimal("31500"),
                status=OrderStatus.FILLED,
                events=[
                    FakeFill(
                        instrument_id=INST_A,
                        order_side=OrderSide.BUY,
                        last_qty=Decimal("31500"),
                        ts_event=china_ts_ns("2026-07-03 10:00:00"),
                    ),
                ],
            ),
        )

        submitted = strategy._submit_full_exit(
            trading_date,
            INST_A,
            strategy.cache.instrument(INST_A),
            Decimal("31500"),
            "rebalance",
        )

        self.assertFalse(submitted)
        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(strategy._sellable_exhausted, {})
        self.assertEqual(strategy.order_events[-1].reason, "sellable_pending_broker_data")

    def test_initialized_sell_in_cache_counts_against_broker_sellable_during_cache_lag(self) -> None:
        trading_date = date(2026, 7, 3)
        strategy = self.make_strategy(
            positions={INST_A: Decimal("42400")},
            prices={INST_A: 13.9},
        )
        strategy.submit_orders_to_cache = False
        strategy._venue_sellable = {str(INST_A): Decimal("600")}

        first_submitted = strategy._submit_full_exit(
            trading_date,
            INST_A,
            strategy.cache.instrument(INST_A),
            Decimal("42400"),
            "rebalance",
        )
        second_submitted = strategy._submit_full_exit(
            trading_date,
            INST_A,
            strategy.cache.instrument(INST_A),
            Decimal("41800"),
            "rebalance",
        )

        self.assertTrue(first_submitted)
        self.assertFalse(second_submitted)
        self.assertEqual([order.quantity for order in strategy.submitted_orders], [Decimal("600")])
        self.assertEqual(strategy._open_sell_quantity(INST_A), Decimal("600"))

    def test_sellable_estimate_excludes_synthetic_reconciliation_buys(self) -> None:
        trading_date = date(2026, 7, 3)
        strategy = self.make_strategy(
            positions={INST_A: Decimal("31900")},
            prices={INST_A: 10.0},
            order_slice_notional="1000000",
        )
        strategy.cache.all_orders.append(
            FakeOrder(
                client_order_id="R-1",
                instrument_id=INST_A,
                side=OrderSide.BUY,
                quantity=Decimal("31500"),
                status=OrderStatus.FILLED,
                events=[
                    FakeFill(
                        instrument_id=INST_A,
                        order_side=OrderSide.BUY,
                        last_qty=Decimal("31500"),
                        ts_event=china_ts_ns("2026-07-03 09:15:00"),
                    ),
                ],
                tags=["RECONCILIATION"],
            ),
        )

        submitted = strategy._submit_full_exit(
            trading_date,
            INST_A,
            strategy.cache.instrument(INST_A),
            Decimal("31900"),
            "rebalance",
        )

        self.assertTrue(submitted)
        self.assertEqual([order.quantity for order in strategy.submitted_orders], [Decimal("31900")])
        self.assertEqual(strategy._sellable_exhausted, {})

    def test_sell_defers_when_today_fill_snapshot_changes(self) -> None:
        trading_date = date(2026, 7, 3)
        strategy = self.make_strategy(
            positions={INST_A: Decimal("1000")},
            prices={INST_A: 10.0},
        )
        snapshots = [
            TodayFillSnapshot(buy_qty=Decimal("0"), sell_qty=Decimal("0"), fill_count=0, latest_ts_event=0),
            TodayFillSnapshot(
                buy_qty=Decimal("100"),
                sell_qty=Decimal("0"),
                fill_count=1,
                latest_ts_event=china_ts_ns("2026-07-03 10:00:00"),
            ),
        ]

        def unstable_snapshot(_instrument_id, _trading_date):
            return snapshots.pop(0)

        strategy._today_fill_snapshot = unstable_snapshot

        submitted = strategy._submit_full_exit(
            trading_date,
            INST_A,
            strategy.cache.instrument(INST_A),
            Decimal("1000"),
            "rebalance",
        )

        self.assertFalse(submitted)
        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(strategy.order_events[-1].reason, "sellable_snapshot_unstable")

    def test_sellable_volume_rejection_backs_off_symbol_for_day(self) -> None:
        strategy = self.make_strategy()
        event = type(
            "RejectedEvent",
            (),
            {
                "client_order_id": "O-1",
                "instrument_id": INST_A,
                "reason": "QMT sellable volume is 400, requested SELL volume is 28300",
            },
        )()

        strategy.on_order_rejected(event)

        self.assertEqual(strategy._sellable_exhausted, {str(INST_A): date(2026, 7, 2)})

    def test_sellable_volume_denial_backs_off_symbol_for_day(self) -> None:
        strategy = self.make_strategy()
        event = type(
            "DeniedEvent",
            (),
            {
                "client_order_id": "O-1",
                "instrument_id": INST_A,
                "reason": "QMT sellable volume is 400, requested SELL volume is 28300",
            },
        )()

        strategy.on_order_denied(event)

        self.assertEqual(strategy._sellable_exhausted, {str(INST_A): date(2026, 7, 2)})

    def test_counter_sellable_rejection_code_backs_off_symbol_for_day(self) -> None:
        strategy = self.make_strategy()
        event = type(
            "RejectedEvent",
            (),
            {
                "client_order_id": "O-1",
                "instrument_id": INST_A,
                "reason": "[COUNTER][251005][证券可用数量不足]",
            },
        )()

        strategy.on_order_rejected(event)

        self.assertEqual(strategy._sellable_exhausted, {str(INST_A): date(2026, 7, 2)})

    def test_cum_notional_exceeds_free_balance_denial_is_treated_as_insufficient_funds(self) -> None:
        strategy = self.make_strategy()
        strategy._target_quantities = {str(INST_A): Decimal("50000")}
        event = type(
            "DeniedEvent",
            (),
            {
                "client_order_id": "O-1",
                "instrument_id": INST_A,
                "reason": "CUM_NOTIONAL_EXCEEDS_FREE_BALANCE: free=21642.68 CNY, cum_notional=299052.00 CNY",
            },
        )()

        strategy.on_order_denied(event)

        self.assertEqual(strategy._insufficient_funds, {str(INST_A)})
        self.assertEqual(strategy._deferred_buys, {str(INST_A): Decimal("50000")})

    def test_sellable_exhaustion_does_not_block_later_buy_target(self) -> None:
        trading_date = date(2026, 7, 2)
        strategy = self.make_strategy(
            positions={INST_A: Decimal("100")},
            prices={INST_A: 10.0},
        )
        strategy._sellable_exhausted = {str(INST_A): trading_date}

        strategy.update_target_quantities({INST_A: 200}, trading_date, "revision")

        self.assertGreater(len(strategy.submitted_orders), 0)
        self.assertEqual(strategy.submitted_orders[0].side, OrderSide.BUY)

    def test_pre_open_reconciliation_schedules_base_alert(self) -> None:
        strategy = self.make_strategy()
        strategy.clock.now = pd.Timestamp("2026-07-02 00:00:00", tz="UTC")

        strategy.configure_pre_open_reconciliation(
            reconcile=lambda timeout_secs: True,
            reconcile_time="09:15",
            timeout_secs=30.0,
        )
        strategy._schedule_pre_open_reconcile()

        self.assertEqual(len(strategy.clock.time_alerts), 1)
        alert = strategy.clock.time_alerts[0]
        self.assertEqual(alert["name"], TargetQuantityStrategy._PRE_OPEN_RECONCILE_ALERT)
        self.assertEqual(alert["alert_time"], pd.Timestamp("2026-07-02 09:15:00", tz="Asia/Shanghai"))
        self.assertTrue(alert["override"])

    def test_pre_open_reconciliation_async_callback_runs_without_running_loop(self) -> None:
        strategy = self.make_strategy()
        calls = []

        async def reconcile(timeout_secs: float) -> bool:
            calls.append(timeout_secs)
            return True

        strategy.configure_pre_open_reconciliation(
            reconcile=reconcile,
            reconcile_time="09:15",
            timeout_secs=30.0,
        )

        strategy._run_pre_open_reconcile()

        self.assertEqual(calls, [30.0])
        self.assertFalse(
            any("no running event loop" in args[0] for args, _kwargs in strategy.log.warnings),
        )
        self.assertTrue(
            any("Pre-open execution-state reconciliation succeeded" in args[0] for args, _kwargs in strategy.log.infos),
        )

    def _make_depth(self, instrument_id: InstrumentId, bid: float, ask: float, ts: str):
        def level(price: float, size: float):
            return type("DepthLevel", (), {"price": Decimal(str(price)), "size": Decimal(str(size))})()

        return type(
            "Depth",
            (),
            {
                "instrument_id": instrument_id,
                "bids": [level(bid, 1000)],
                "asks": [level(ask, 1200)],
                "ts_event": china_ts_ns(ts),
                "ts_init": china_ts_ns(ts),
            },
        )()

    def test_on_order_book_depth_captures_ladder_only(self) -> None:
        strategy = self.make_strategy()
        strategy._today_open = {}
        strategy._last_close = {}
        strategy.clock.now = pd.Timestamp("2026-07-02 02:00:00", tz="UTC")

        strategy.on_order_book_depth(self._make_depth(INST_A, 9.99, 10.01, "2026-07-02 09:30:01"))

        # The depth mid/best price is NOT dependable for open/last price, so the
        # depth handler must not populate _today_open / _last_close — only the
        # walk-book ladder is captured.
        self.assertNotIn(str(INST_A), strategy._today_open)
        self.assertNotIn(str(INST_A), strategy._last_close)
        bids, asks = strategy._depth_books[str(INST_A)]
        self.assertEqual(bids, [(9.99, 1000.0)])
        self.assertEqual(asks, [(10.01, 1200.0)])

    def test_full_tick_open_overrides_seeded_last_close(self) -> None:
        # Regression for the 42.14-vs-53.09 bug: a stale seeded last-close (or
        # depth mid) must be overwritten by the authoritative full-tick open.
        strategy = self.make_strategy()
        strategy._pricing_date = date(2026, 7, 2)
        strategy._today_open = {str(INST_A): 42.14}  # stale seed / depth value
        strategy._authoritative_open = set()
        strategy.clock.now = pd.Timestamp("2026-07-02 01:27:00", tz="UTC")

        strategy._apply_full_tick({str(INST_A): {"open": 53.09, "last_price": 54.0}}, "prefetch")

        self.assertEqual(strategy._today_open[str(INST_A)], 53.09)
        self.assertIn(str(INST_A), strategy._authoritative_open)

    def test_full_tick_update_does_not_trigger_convergence(self) -> None:
        strategy = self.make_strategy(
            free_cash="1000000",
            equity="1000000",
            prices={INST_A: 10.0},
            open_prices={},
        )
        strategy._target_quantities = {str(INST_A): Decimal("50000")}
        strategy._target_date = date(2026, 7, 2)
        strategy._target_reason = "loaded_target"
        strategy._target_version = "loaded-v1"

        strategy._apply_full_tick({str(INST_A): {"open": 10.0}}, "refresh")

        self.assertEqual(strategy._today_open[str(INST_A)], 10.0)
        self.assertEqual(strategy.submitted_orders, [])

    def test_full_tick_async_source_runs_without_running_loop(self) -> None:
        strategy = self.make_strategy()
        strategy.clock.now = pd.Timestamp("2026-07-02 01:27:00", tz="UTC")

        async def fetch_full_tick() -> dict[str, dict[str, float]]:
            return {str(INST_A): {"open": 53.09}}

        strategy.configure_full_tick_source(fetch_full_tick)

        strategy._run_full_tick_fetch(trigger="refresh")

        self.assertEqual(strategy._today_open[str(INST_A)], 53.09)
        self.assertFalse(
            any("no running event loop" in args[0] for args, _kwargs in strategy.log.warnings),
        )

    def test_seed_does_not_override_authoritative_open(self) -> None:
        strategy = self.make_strategy()
        strategy.config = TargetQuantityStrategyConfig(
            instrument_ids=[INST_A],
            bar_types={},
            seed_open_from_last_close=True,
        )
        strategy._pricing_date = date(2026, 7, 2)
        strategy._today_open = {str(INST_A): 53.09}
        strategy._authoritative_open = {str(INST_A)}
        strategy._last_close = {str(INST_A): 42.14}

        strategy._seed_open_prices_from_last_close(date(2026, 7, 2))

        self.assertEqual(strategy._today_open[str(INST_A)], 53.09)

    def test_full_tick_open_extraction(self) -> None:
        self.assertEqual(TargetQuantityStrategy._full_tick_open({"open": 53.09}), 53.09)
        self.assertEqual(TargetQuantityStrategy._full_tick_open(53.09), 53.09)
        self.assertIsNone(TargetQuantityStrategy._full_tick_open({"open": 0.0}))
        self.assertIsNone(TargetQuantityStrategy._full_tick_open({"open": None}))
        self.assertIsNone(TargetQuantityStrategy._full_tick_open({"last_price": 1.0}))

    def test_buy_price_uses_gap_up_open_not_stale_seed(self) -> None:
        # End-to-end: after a full-tick refresh, a gap-up open drives the buy
        # limit price (open + offset), not the stale seeded last close.
        strategy = self.make_strategy(open_prices={INST_A: 42.14})
        strategy._pricing_date = date(2026, 7, 2)
        strategy._authoritative_open = set()
        strategy._apply_full_tick({str(INST_A): {"open": 53.09}}, "prefetch")

        instrument = strategy.cache.instrument(INST_A)
        price = strategy._limit_price(instrument, INST_A, OrderSide.BUY, Decimal("100"))

        # buy price = open + max(open*offset_bps/1e4, tick); far above the stale 42.14.
        self.assertGreater(float(price), 53.0)
        self.assertLessEqual(float(price), 53.09 + 53.09 * strategy.config.buy_max_price_bps / 10_000.0)

    def test_reconcile_increments_side_specific_cancel_count(self) -> None:
        trading_date = date(2026, 7, 2)
        strategy = self.make_strategy(prices={INST_A: 10.0})
        strategy._today_open = {str(INST_A): 10.0}
        order = FakeOrder(
            client_order_id="S-1",
            instrument_id=INST_A,
            side=OrderSide.SELL,
            quantity=Decimal("100"),
            price=Decimal("9.99"),
            status=OrderStatus.ACCEPTED,
            ts_last=1,
        )
        strategy.cache.open_orders.append(order)

        strategy._reconcile_unfilled_orders(trading_date)

        self.assertEqual(strategy.canceled_orders, [order])
        self.assertEqual(strategy._cancel_count_sell, {str(INST_A): 1})
        self.assertEqual(strategy._cancel_count_buy, {})

    def test_reconcile_does_not_cancel_buy_at_max_price_cap(self) -> None:
        trading_date = date(2026, 7, 2)
        strategy = self.make_strategy(prices={INST_A: 10.0})
        strategy._today_open = {str(INST_A): 10.0}
        # cap = 10 + max(10*10bps=0.01, 0.01) = 10.01; order resting at the cap
        order = FakeOrder(
            client_order_id="B-1",
            instrument_id=INST_A,
            side=OrderSide.BUY,
            quantity=Decimal("100"),
            price=Decimal("10.01"),
            status=OrderStatus.ACCEPTED,
            ts_last=1,
        )
        strategy.cache.open_orders.append(order)

        strategy._reconcile_unfilled_orders(trading_date)

        self.assertEqual(strategy.canceled_orders, [])
        self.assertEqual(strategy._cancel_count_buy, {})

    def test_order_fill_resets_filled_side_cancel_count(self) -> None:
        strategy = self.make_strategy()
        strategy._cancel_count_buy = {str(INST_A): 4}
        strategy._cancel_count_sell = {str(INST_A): 2}
        event = type(
            "FillEvent",
            (),
            {"client_order_id": "O-1", "instrument_id": INST_A, "order_side": OrderSide.BUY},
        )()

        strategy.on_order_filled(event)

        self.assertEqual(strategy._cancel_count_buy, {})
        self.assertEqual(strategy._cancel_count_sell, {str(INST_A): 2})


if __name__ == "__main__":
    unittest.main()
