from __future__ import annotations

import unittest
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from decimal import Decimal

import pandas as pd

from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId

from strategies.target_weights import TargetWeightStrategyConfig
from strategies.target_weights import TargetWeightStrategy
from strategies.target_weights import TodayFillSnapshot
from strategies.target_weights import NotionalOrderSplitter


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


class FakeAccount:
    def __init__(self, free_cash: Decimal | str) -> None:
        self.free_cash = Decimal(str(free_cash))

    def balance_free(self) -> FakeMoney:
        return FakeMoney(self.free_cash)


class FakePortfolio:
    def __init__(
        self,
        positions: dict[InstrumentId, Decimal] | None = None,
        equity: Decimal | str = "1000000",
        free_cash: Decimal | str = "1000000",
    ) -> None:
        self.positions = positions or {}
        self.equity_value = Decimal(str(equity))
        self.account_value = FakeAccount(free_cash)
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

    def orders_open(self, **kwargs):
        return self._filter_orders(self.open_orders, **kwargs)

    def orders(self, **kwargs):
        return self._filter_orders(self.all_orders, **kwargs)

    @staticmethod
    def _filter_orders(orders: list[FakeOrder], **kwargs):
        instrument_id = kwargs.get("instrument_id")
        side = kwargs.get("side")
        result = list(orders)
        if instrument_id is not None:
            result = [order for order in result if order.instrument_id == instrument_id]
        if side is not None:
            result = [order for order in result if order.side == side]
        return result

    def positions_open(self, **_kwargs):
        return [
            FakePosition(instrument_id=instrument_id, quantity=quantity)
            for instrument_id, quantity in self.portfolio.positions.items()
            if quantity > 0
        ]


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


class TestableTargetWeightStrategy:
    _UP_LIMIT_KEYS = TargetWeightStrategy._UP_LIMIT_KEYS
    _DOWN_LIMIT_KEYS = TargetWeightStrategy._DOWN_LIMIT_KEYS
    _PRE_OPEN_RECONCILE_ALERT = TargetWeightStrategy._PRE_OPEN_RECONCILE_ALERT
    on_start = TargetWeightStrategy.on_start
    refresh_target_instruments = TargetWeightStrategy.refresh_target_instruments
    update_target_weights = TargetWeightStrategy.update_target_weights
    on_bar = TargetWeightStrategy.on_bar
    on_trade_tick = TargetWeightStrategy.on_trade_tick
    on_order_filled = TargetWeightStrategy.on_order_filled
    on_order_rejected = TargetWeightStrategy.on_order_rejected
    _converge_to_target = TargetWeightStrategy._converge_to_target
    _order_book_depth_logging_enabled = TargetWeightStrategy._order_book_depth_logging_enabled
    _should_log_sample = staticmethod(TargetWeightStrategy._should_log_sample)
    _on_converge_timer = TargetWeightStrategy._on_converge_timer
    _desired_weights = TargetWeightStrategy._desired_weights
    _held_instrument_ids = TargetWeightStrategy._held_instrument_ids
    _refresh_symbol_freezes = TargetWeightStrategy._refresh_symbol_freezes
    _price_limit_reason = TargetWeightStrategy._price_limit_reason
    _target_achieved = TargetWeightStrategy._target_achieved
    _reconcile_unfilled_orders = TargetWeightStrategy._reconcile_unfilled_orders
    _submit_buys_within_cash = TargetWeightStrategy._submit_buys_within_cash
    _target_order_intent = TargetWeightStrategy._target_order_intent
    _order_slices = TargetWeightStrategy._order_slices
    _estimated_order_cost = staticmethod(TargetWeightStrategy._estimated_order_cost)
    _estimated_buy_cost = TargetWeightStrategy._estimated_buy_cost
    _submit_target_weight = TargetWeightStrategy._submit_target_weight
    _submit_full_exit = TargetWeightStrategy._submit_full_exit
    _submit_order_quantity = TargetWeightStrategy._submit_order_quantity
    _clamp_sell_quantity = TargetWeightStrategy._clamp_sell_quantity
    _today_fill_snapshot = TargetWeightStrategy._today_fill_snapshot
    _event_trading_date = TargetWeightStrategy._event_trading_date
    _is_reconciliation_order = staticmethod(TargetWeightStrategy._is_reconciliation_order)
    _open_sell_quantity = TargetWeightStrategy._open_sell_quantity
    _decimal_quantity = staticmethod(TargetWeightStrategy._decimal_quantity)
    _track_submitted_order = TargetWeightStrategy._track_submitted_order
    _limit_price = TargetWeightStrategy._limit_price
    _target_quantity = TargetWeightStrategy._target_quantity
    _portfolio_value = TargetWeightStrategy._portfolio_value
    _free_cash = TargetWeightStrategy._free_cash
    _current_quantity = TargetWeightStrategy._current_quantity
    _current_weight = TargetWeightStrategy._current_weight
    _open_order_instruments = TargetWeightStrategy._open_order_instruments
    _price_limits = TargetWeightStrategy._price_limits
    _at_price_limit = TargetWeightStrategy._at_price_limit
    _target_side = TargetWeightStrategy._target_side
    _stop_time_reached = TargetWeightStrategy._stop_time_reached
    _within_trading_window = TargetWeightStrategy._within_trading_window
    _clock_date = TargetWeightStrategy._clock_date
    _record_target = TargetWeightStrategy._record_target
    _record_order = TargetWeightStrategy._record_order
    _is_star_market = staticmethod(TargetWeightStrategy._is_star_market)
    configure_pre_open_reconciliation = TargetWeightStrategy.configure_pre_open_reconciliation
    _parse_hh_mm = staticmethod(TargetWeightStrategy._parse_hh_mm)
    _next_daily_time = TargetWeightStrategy._next_daily_time
    _schedule_pre_open_reconcile = TargetWeightStrategy._schedule_pre_open_reconcile
    _on_pre_open_reconcile_timer = TargetWeightStrategy._on_pre_open_reconcile_timer

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

    def submit_order(self, order) -> None:
        self.submitted_orders.append(order)
        self.cache.open_orders.append(order)
        self.cache.all_orders.append(order)

    def cancel_order(self, order) -> None:
        self.canceled_orders.append(order)
        self.cache.open_orders = [
            existing
            for existing in self.cache.open_orders
            if existing.client_order_id != order.client_order_id
        ]

    def on_target_bar(self, _bar) -> None:
        self.update_target_weights({INST_A: 0.5}, date(2026, 7, 2), "bar")


class TargetWeightStrategyTest(unittest.TestCase):
    def make_strategy(
        self,
        *,
        positions: dict[InstrumentId, Decimal] | None = None,
        equity: Decimal | str = "1000000",
        free_cash: Decimal | str = "1000000",
        prices: dict[InstrumentId, float] | None = None,
        fields: dict[InstrumentId, dict] | None = None,
        target_cash_buffer_percent: float = 0.05,
        order_slice_notional: Decimal | str = "300000",
        require_account_cash: bool = True,
    ) -> TestableTargetWeightStrategy:
        instruments = {
            instrument_id: FakeInstrument(instrument_id, fields=(fields or {}).get(instrument_id))
            for instrument_id in (INST_A, INST_B, INST_C)
        }
        strategy = TestableTargetWeightStrategy()
        strategy.config = TargetWeightStrategyConfig(
            instrument_ids=[INST_A, INST_B, INST_C],
            bar_types={},
            initial_cash=Decimal(str(equity)),
            target_cash_buffer_percent=target_cash_buffer_percent,
            cash_buffer_percent=0.0,
            unfilled_timeout_secs=1.0,
            weight_tolerance_percent=0.003,
            cash_tolerance_percent=0.01,
            stop_time=None,
            order_slice_notional=Decimal(str(order_slice_notional)),
            require_account_cash=require_account_cash,
        )
        portfolio = FakePortfolio(positions=positions, equity=equity, free_cash=free_cash)
        strategy.cache = FakeCache(instruments, portfolio)
        strategy.portfolio = portfolio
        strategy.clock = FakeClock()
        strategy.order_factory = FakeOrderFactory()
        strategy.id = "TEST-001"
        strategy.log = FakeLog()
        strategy._instrument_ids = [INST_A, INST_B, INST_C]
        strategy._bar_types = {}
        strategy._target_weights = {}
        strategy._target_date = None
        strategy._target_reason = "target_weight"
        strategy._target_version = ""
        strategy._achieved_versions = set()
        strategy._frozen_instruments = {}
        strategy._deferred_buys = {}
        strategy._rejected_order_ids = set()
        strategy._insufficient_funds = set()
        strategy._order_submit_ts = {}
        strategy._order_target_weights = {}
        strategy._order_target_versions = {}
        strategy._order_splitter = NotionalOrderSplitter(Decimal(str(order_slice_notional)))
        strategy._convergence_suspended = False
        strategy._pre_open_reconcile = None
        strategy._pre_open_reconcile_time = None
        strategy._pre_open_reconcile_timeout_secs = 30.0
        strategy._pre_open_reconcile_task = None
        strategy._sellable_exhausted = {}
        strategy.target_events = []
        strategy.order_events = []
        strategy.requested_instruments = []
        strategy.subscribed_bars = []
        strategy.subscribed_quote_ticks = []
        strategy.subscribed_trade_ticks = []
        strategy.subscribed_order_book_depths = []
        strategy.submitted_orders = []
        strategy.canceled_orders = []
        strategy._last_close = {
            str(instrument_id): price
            for instrument_id, price in (prices or {INST_A: 10.0, INST_B: 20.0, INST_C: 25.0}).items()
        }
        return strategy

    def test_on_start_subscribes_trade_ticks_with_quote_ticks(self) -> None:
        bar_type = BarType.from_str(f"{INST_A}-1-MINUTE-LAST-EXTERNAL")
        strategy = self.make_strategy()
        strategy._bar_types = {str(INST_A): bar_type}

        strategy.on_start()

        self.assertEqual(strategy.subscribed_bars, [bar_type])
        self.assertEqual(strategy.subscribed_quote_ticks, [INST_A])
        self.assertEqual(strategy.subscribed_trade_ticks, [INST_A])

    def test_refresh_target_instruments_subscribes_trade_ticks_for_new_bars(self) -> None:
        bar_type = BarType.from_str(f"{INST_B}-1-MINUTE-LAST-EXTERNAL")
        strategy = self.make_strategy()

        strategy.refresh_target_instruments([INST_B], {str(INST_B): bar_type})

        self.assertEqual(strategy.requested_instruments, [INST_B])
        self.assertEqual(strategy.subscribed_bars, [bar_type])
        self.assertEqual(strategy.subscribed_quote_ticks, [INST_B])
        self.assertEqual(strategy.subscribed_trade_ticks, [INST_B])

    def test_trade_tick_logging_uses_sample_rate_like_quote_ticks(self) -> None:
        strategy = self.make_strategy()
        strategy.config = TargetWeightStrategyConfig(
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

    def test_desired_weights_exit_non_targets_by_default(self) -> None:
        strategy = self.make_strategy(positions={INST_A: Decimal("100"), INST_C: Decimal("200")})
        strategy.update_target_weights({INST_A: 0.5}, date(2026, 7, 2), "test")

        desired = strategy._desired_weights()

        self.assertEqual(desired[str(INST_A)], 0.5)
        self.assertEqual(desired[str(INST_C)], 0.0)

    def test_update_target_replaces_deferred_buy_intent(self) -> None:
        strategy = self.make_strategy(free_cash="0")
        strategy.update_target_weights({INST_A: 0.5}, date(2026, 7, 2), "first")
        self.assertEqual(strategy._deferred_buys, {str(INST_A): 0.5})

        strategy.update_target_weights({INST_B: 0.4}, date(2026, 7, 2), "second")

        self.assertNotIn(str(INST_A), strategy._deferred_buys)
        self.assertEqual(strategy._deferred_buys, {str(INST_B): 0.4})
        self.assertEqual(strategy._target_weights, {str(INST_B): 0.4})

    def test_convergence_submits_sell_before_cash_gated_buy(self) -> None:
        strategy = self.make_strategy(
            positions={INST_C: Decimal("100")},
            free_cash="1000",
            prices={INST_A: 10.0, INST_C: 25.0},
        )

        strategy.update_target_weights({INST_A: 0.5}, date(2026, 7, 2), "rebalance")

        self.assertGreaterEqual(len(strategy.submitted_orders), 1)
        self.assertEqual(strategy.submitted_orders[0].instrument_id, INST_C)
        self.assertEqual(strategy.submitted_orders[0].side, OrderSide.SELL)
        self.assertEqual(strategy._deferred_buys, {str(INST_A): 0.5})

    def test_limit_up_freezes_only_affected_buy_symbol(self) -> None:
        strategy = self.make_strategy(
            free_cash="1000000",
            prices={INST_A: 10.0, INST_B: 20.0},
            fields={INST_A: {"UpStopPrice": 10.0}},
        )

        strategy.update_target_weights(
            {INST_A: 0.3, INST_B: 0.3},
            date(2026, 7, 2),
            "limit_test",
        )

        self.assertEqual(strategy._frozen_instruments, {str(INST_A): "up_limit"})
        submitted_instruments = {order.instrument_id for order in strategy.submitted_orders}
        self.assertNotIn(INST_A, submitted_instruments)
        self.assertIn(INST_B, submitted_instruments)

    def test_practical_achievement_accepts_cash_buffer_and_weight_tolerance(self) -> None:
        strategy = self.make_strategy(
            positions={INST_A: Decimal("95000")},
            equity="1000000",
            free_cash="50000",
            prices={INST_A: 10.0},
        )
        strategy._target_weights = {str(INST_A): 0.95}
        strategy._target_version = "achieved"
        strategy._target_date = date(2026, 7, 2)

        self.assertTrue(strategy._target_achieved())

    def test_practical_achievement_rejects_excess_cash(self) -> None:
        strategy = self.make_strategy(
            positions={INST_A: Decimal("90000")},
            equity="1000000",
            free_cash="100000",
            prices={INST_A: 10.0},
        )
        strategy._target_weights = {str(INST_A): 0.95}
        strategy._target_version = "not-achieved"
        strategy._target_date = date(2026, 7, 2)

        self.assertFalse(strategy._target_achieved())

    def test_order_slice_notional_combines_small_delta_into_one_order(self) -> None:
        strategy = self.make_strategy(
            free_cash="1000000",
            equity="1000000",
            prices={INST_A: 25.0},
        )

        strategy.update_target_weights({INST_A: 0.03}, date(2026, 7, 2), "small")

        self.assertEqual(len(strategy.submitted_orders), 1)
        self.assertEqual(strategy.submitted_orders[0].quantity, Decimal("1200"))

    def test_order_slice_notional_splits_large_delta_by_300k(self) -> None:
        strategy = self.make_strategy(
            free_cash="1000000",
            equity="1000000",
            prices={INST_A: 10.0},
        )

        strategy.update_target_weights({INST_A: 0.95}, date(2026, 7, 2), "large")

        self.assertEqual([order.quantity for order in strategy.submitted_orders], [
            Decimal("29900"),
            Decimal("29900"),
            Decimal("29900"),
            Decimal("5300"),
        ])

    def test_buy_cash_gate_uses_each_slice_and_defers_unfunded_remainder(self) -> None:
        strategy = self.make_strategy(
            free_cash="350000",
            equity="1000000",
            prices={INST_A: 10.0},
        )

        strategy.update_target_weights({INST_A: 0.95}, date(2026, 7, 2), "partial")

        self.assertEqual([order.quantity for order in strategy.submitted_orders], [Decimal("29900")])
        self.assertEqual(strategy._deferred_buys, {str(INST_A): 0.95})

    def test_weight_tolerance_skips_tiny_residual_buy(self) -> None:
        strategy = self.make_strategy(
            positions={INST_A: Decimal("94800")},
            equity="1000000",
            free_cash="52000",
            prices={INST_A: 10.0},
        )

        strategy.update_target_weights({INST_A: 0.95}, date(2026, 7, 2), "tiny")

        self.assertEqual(strategy.submitted_orders, [])

    def test_update_target_does_not_converge_when_suspended_outside_window(self) -> None:
        strategy = self.make_strategy(free_cash="1000000")
        strategy._convergence_suspended = True

        strategy.update_target_weights({INST_A: 0.5}, date(2026, 7, 2), "preopen")

        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(strategy._target_weights, {str(INST_A): 0.5})

    def test_update_target_does_not_submit_before_trading_window(self) -> None:
        strategy = self.make_strategy(free_cash="1000000")
        strategy.clock.now = pd.Timestamp("2026-07-02 01:16:00", tz="UTC")

        strategy.update_target_weights({INST_A: 0.5}, date(2026, 7, 2), "preopen")

        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(strategy._target_weights, {str(INST_A): 0.5})

    def test_order_fill_releases_only_filled_symbol_insufficient_funds_backoff(self) -> None:
        strategy = self.make_strategy()
        strategy._insufficient_funds = {str(INST_A), str(INST_B)}
        event = type("FillEvent", (), {"client_order_id": "O-1", "instrument_id": INST_A})()

        strategy.on_order_filled(event)

        self.assertEqual(strategy._insufficient_funds, {str(INST_B)})

    def test_missing_account_cash_defers_buys_without_using_equity_as_cash(self) -> None:
        strategy = self.make_strategy(require_account_cash=True)
        strategy.portfolio.has_account = False

        strategy.update_target_weights({INST_A: 0.5}, date(2026, 7, 2), "missing_cash")

        self.assertEqual(strategy.submitted_orders, [])
        self.assertEqual(strategy._deferred_buys, {str(INST_A): 0.5})

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
        self.assertEqual(strategy._sellable_exhausted, {str(INST_A): trading_date})

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
        self.assertEqual(strategy._sellable_exhausted, {str(INST_A): trading_date})
        self.assertEqual(strategy.order_events[-1].reason, "sellable_exhausted")

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

    def test_sellable_exhaustion_does_not_block_later_buy_target(self) -> None:
        trading_date = date(2026, 7, 2)
        strategy = self.make_strategy(
            positions={INST_A: Decimal("100")},
            prices={INST_A: 10.0},
        )
        strategy._sellable_exhausted = {str(INST_A): trading_date}

        strategy.update_target_weights({INST_A: 0.2}, trading_date, "revision")

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
        self.assertEqual(alert["name"], TargetWeightStrategy._PRE_OPEN_RECONCILE_ALERT)
        self.assertEqual(alert["alert_time"], pd.Timestamp("2026-07-02 09:15:00", tz="Asia/Shanghai"))
        self.assertTrue(alert["override"])


if __name__ == "__main__":
    unittest.main()
