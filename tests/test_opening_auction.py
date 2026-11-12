from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from nautilus_trader.adapters.qmt.common import parse_equity
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import LatencyModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import RiskEngineConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, BookType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money

from backtests.data_providers.historical_ticks import HistoricalTick, HistoricalTickDataProvider
from backtests.data_providers.ricequant_ticks import ClickHouseRicequantTickDataProvider
from backtests.data_providers.clickhouse import ClickHouseConnectionConfig
from backtests.target_model_predictions.auction_simulation import AuctionReplayFillModel, AuctionReplayModule, auction_execution_report, AShareReplayFeeModel
from backtests.target_model_predictions.run_backtest import (
    BacktestTargetModelPredictionsStrategy, PLANNING_CLIENT_ID, prepare_tick_replay_event_data, prepare_tick_replay_depth_data, build_complete_report, build_engine,
)
from market_data import DailyStockData
from strategies.model_prediction_targets import TargetModelPredictionsStrategyConfig
from strategies.model_target_planners.base import ModelTargetPlan, TargetInfo, TargetContext
from strategies.opening_auction import AuctionReference
from strategies.order_splitter import AuctionOrderSplitter
from strategies.strategy_params import StrategyParams


def timestamp(value: str) -> int:
    return pd.Timestamp(value, tz="Asia/Shanghai").value


class FixedPlanner:
    def __init__(self, *, second_day_exit=False, second_day_rotate=False):
        self.requests = []
        self.second_day_exit = second_day_exit
        self.second_day_rotate = second_day_rotate

    def plan(self, request):
        self.requests.append(request)
        if self.second_day_exit and request.trading_date == date(2026, 7, 9):
            return ModelTargetPlan(request.trading_date, request.signal_date, [], "exit")
        if self.second_day_rotate and request.trading_date == date(2026, 7, 9):
            return ModelTargetPlan(request.trading_date, request.signal_date, [
                TargetInfo("000003.SZ", None, 1000, TargetContext(), None, "000003.SZ.QMT", False),
            ], "rotate")
        return ModelTargetPlan(request.trading_date, request.signal_date, [
            TargetInfo("000001.SZ", None, 1000, TargetContext(), None, "000001.SZ.QMT", False),
            TargetInfo("000002.SZ", None, 500, TargetContext(), None, "000002.SZ.QMT", False),
        ], "test")


def replay_fixture(*, persister=None, final_results=True, continuous=False,
                   auction_enabled=True, auction_receive_delay_ms=0, submission_latency_ms=0,
                   second_day_exit=False, second_day_rotate=False,
                   closing_cancel_time=None, starting_cash=12500,
                   auction_partial_min_notional=0, transform_rows=None,
                   strategy_type=BacktestTargetModelPredictionsStrategy, transform_config=None):
    engine = BacktestEngine(BacktestEngineConfig(
        trader_id=TraderId("TEST-001"), logging=LoggingConfig(bypass_logging=True),
        risk_engine=RiskEngineConfig(bypass=True),
    ))
    model = AuctionReplayFillModel(engine.kernel.clock.timestamp_ns, 0.01)
    prices = [("000001.SZ", 10), ("000002.SZ", 20)]
    if second_day_rotate:
        prices.append(("000003.SZ", 10))
    two_days = second_day_exit or second_day_rotate
    instruments = {code: parse_equity(code, {"UpStopPrice": price * 1.1,
                                            "DownStopPrice": price * 0.9}, 0, 0)
                   for code, price in prices}
    rows = []
    for second in range(122):
        ts = timestamp("2026-07-08 09:22:58") + second * 1_000_000_000
        for code, price in prices:
            rows.append(HistoricalTick(code, "indicative", ts, ts, price, price, 10000, 10000, price, 0))
    if final_results:
        for code, price in prices:
            ts = timestamp("2026-07-08 09:25:00")
            rows.append(HistoricalTick(code, "auction", ts,
                ts + int(auction_receive_delay_ms * 1_000_000),
                price, price, 10000, 10000, price, price))
    # Always advance beyond the auction; zero executable liquidity is deliberately
    # modeled as an indicative observation if there are no continuous quotes.
    if continuous:
        for clock_time in ["09:30:00", "14:30:00", "14:30:02"]:
            for code, price in prices:
                ts = timestamp(f"2026-07-08 {clock_time}")
                rows.append(HistoricalTick(code, "quote", ts, ts, price, price + .01, 10000, 10000, price, price))
    if two_days:
        for clock_time, kind in [("09:24:01", "indicative"), ("09:24:50", "indicative"),
                                 ("09:25:00", "auction")]:
            for code, price in prices:
                ts = timestamp(f"2026-07-09 {clock_time}")
                rows.append(HistoricalTick(code, kind, ts, ts, price, price, 10000, 10000,
                                          price, price if kind == "auction" else 0))
    if closing_cancel_time is not None:
        for clock_time in ["14:56:58", closing_cancel_time]:
            for code, price in prices:
                ts = timestamp(f"2026-07-08 {clock_time}")
                rows.append(HistoricalTick(code, "quote", ts, ts, price + .20, price + .21,
                                          10000, 10000, price, price))
    if transform_rows is not None:
        rows = transform_rows(rows)
    module = AuctionReplayModule(model, {(str(instruments[r.symbol].id), r.ts_init): r for r in rows})
    engine.add_venue(Venue("QMT"), OmsType.NETTING, AccountType.CASH, [Money(starting_cash, CNY)],
                     base_currency=CNY, fill_model=model, modules=[module],
                     fee_model=AShareReplayFeeModel(.000085, 5, .0005),
                     latency_model=LatencyModel(base_latency_nanos=0,
                         insert_latency_nanos=int(submission_latency_ms * 1_000_000)),
                     bar_execution=False, trade_execution=False, liquidity_consumption=True, book_type=BookType.L2_MBP)
    for instrument in instruments.values():
        engine.add_instrument(instrument)
    config = TargetModelPredictionsStrategyConfig(
        instrument_ids=[i.id for i in instruments.values()],
        bar_types={str(i.id): BarType.from_str(f"{i.id}-1-DAY-LAST-EXTERNAL")
                   for i in instruments.values()},
        instrument_stock_codes={str(i.id): code for code, i in instruments.items()},
        signals_by_date={day: [
            {"date": day, "stock_code": "000001.SZ", "score": .1, "rank": 2, "pred_return_live": .1},
            {"date": day, "stock_code": "000002.SZ", "score": .2, "rank": 1, "pred_return_live": .2},
        ] + ([{"date": day, "stock_code": "000003.SZ", "score": .05, "rank": 3,
                "pred_return_live": .05}] if second_day_rotate else [])
            for day in (["2026-07-07", "2026-07-08"] if two_days else ["2026-07-07"])},
        prediction_ranks_by_date={day: {"000001.SZ": 2, "000002.SZ": 1,
                                      **({"000003.SZ": 3} if second_day_rotate else {})}
                                 for day in (["2026-07-07", "2026-07-08"]
                                             if two_days else ["2026-07-07"])},
        trading_dates=["2026-07-07", "2026-07-08"] + (["2026-07-09"] if two_days else []),
        listed_dates={code: "2020-01-01" for code in instruments}, st_by_date={}, suspended_by_date={},
        daily_stock_data=tuple(DailyStockData(stock_code=code, trade_date=date(2026, 7, day),
                                            up_limit=price * 1.1, down_limit=price * .9)
                               for day in ([8, 9] if two_days else [8])
                               for code, price in prices),
        auction_enabled=auction_enabled, subscribe_bars=False, subscribe_quote_ticks=True,
        auction_partial_min_notional=Decimal(str(auction_partial_min_notional)),
        price_mismatch_cancel_delay_secs=1 if closing_cancel_time else 30,
        small_leaves_notional_threshold=0 if closing_cancel_time else 20000,
        subscribe_trade_ticks=False, process_targets_on_timer=True, min_listed_days=1,
        process_targets_interval_secs=30, resubmit_check_interval_secs=30,
        initial_cash=Decimal(str(starting_cash)), cash_buffer_percent=0,
        risk_manager_base_url="http://127.0.0.1:8000",
        risk_manager_risk_model_id="cn_a_basic_constraints_integer_lots",
        alpha_model_id="test", target_weight_planner="risk_manager", execution_windows="14:30-14:55",
        exchange_execution_windows="14:30-14:55",
    )
    if transform_config is not None:
        config = transform_config(config)
    strategy = strategy_type(config)
    planner = FixedPlanner(second_day_exit=second_day_exit, second_day_rotate=second_day_rotate)
    strategy._target_planner = planner
    strategy._auction_replay_model = model
    if persister is not None:
        strategy.configure_live_target_plan_persister(persister)
    events, quotes = prepare_tick_replay_event_data(SimpleNamespace(exchange_timezone="Asia/Shanghai"), rows, instruments)
    engine.add_data(prepare_tick_replay_depth_data(rows, instruments), sort=False)
    engine.add_data(quotes, sort=False)
    engine.add_data(events, client_id=PLANNING_CLIENT_ID, sort=False)
    engine.sort_data()
    engine.add_strategy(strategy)
    return engine, strategy, planner, model


class OpeningAuctionTest(unittest.TestCase):
    def test_reference_freshness_and_config_validation(self):
        now = timestamp("2026-07-08 09:24:00")
        self.assertTrue(AuctionReference(10, now - 1, now).is_fresh(now, "Asia/Shanghai", 4))
        for ref in [AuctionReference(10, now + 1, now + 1),
                    AuctionReference(10, now - 5_000_000_000, now),
                    AuctionReference(float("nan"), now, now)]:
            self.assertFalse(ref.is_fresh(now, "Asia/Shanghai", 4))
        for kwargs in [{"auction_submit_cutoff": "09:25"}, {"auction_buy_offset_bps": -1},
                       {"auction_prepare_time": "09:24:50"}, {"auction_commission_rate": 1},
                       {"auction_partial_min_notional": -1}]:
            with self.assertRaises(ValueError):
                StrategyParams(**kwargs)

    def test_affordable_board_quantities_include_fees(self):
        instrument = parse_equity("688001.SH", {}, 0, 0)
        splitter = AuctionOrderSplitter(Decimal("0.000085"), Decimal(5), Decimal(0))
        self.assertEqual(splitter.allocate(instrument, Decimal(1000), Decimal(10), Decimal(2500)).quantities,
                         (Decimal(249),))
        self.assertEqual(splitter.allocate(instrument, Decimal(1000), Decimal(10), Decimal(1000)).quantities, ())

    def test_ranked_cash_partial_fills_and_frozen_plan(self):
        engine, strategy, planner, model = replay_fixture()
        try:
            engine.run()
            orders = engine.trader.generate_orders_report()
            fills = engine.trader.generate_fills_report()
            self.assertEqual(len(orders), 2, orders.to_string())
            quantities = {str(row.instrument_id): int(row.quantity) for _, row in orders.iterrows()}
            self.assertEqual(quantities, {"000002.SZ.QMT": 500, "000001.SZ.QMT": 200})
            self.assertEqual({o.ts_init for o in engine.cache.orders()},
                             {timestamp("2026-07-08 09:24:50")})
            self.assertEqual({str(o.instrument_id): float(o.price) for o in engine.cache.orders()},
                             {"000001.SZ.QMT": 10.05, "000002.SZ.QMT": 20.10})
            self.assertEqual(len(planner.requests), 1)
            self.assertEqual(len(fills), 2)
            for _, fill in fills.iterrows():
                self.assertEqual(int(fill.last_qty), 100)
                self.assertGreaterEqual(pd.Timestamp(fill.ts_event).value, timestamp("2026-07-08 09:25:00"))
            frozen = strategy.compute_daily_target_plan(date(2026, 7, 8))
            self.assertEqual(frozen.targets[0].target_context.price_source, "auction_indicative")
            self.assertEqual(frozen.targets[0].target_context.prediction_rank, 2)
            self.assertEqual(strategy._today_open["000001.SZ.QMT"], 10)
            self.assertLessEqual(sum(strategy._auction_committed_cost for _ in [0]), Decimal(12500))
        finally:
            engine.dispose()

    def test_no_auction_execution_without_persistence(self):
        engine, strategy, planner, model = replay_fixture(persister=lambda plan: False)
        try:
            engine.run()
            self.assertTrue(engine.trader.generate_orders_report().empty)
            self.assertNotIn(date(2026, 7, 8), strategy._auction_frozen_dates)
        finally:
            engine.dispose()

    def test_baseline_plans_with_all_simultaneous_actual_opens(self):
        engine, strategy, planner, model = replay_fixture(auction_enabled=False, continuous=True)
        try:
            engine.run()
            self.assertEqual(len(planner.requests), 1)
            self.assertEqual(planner.requests[0].open_prices,
                             {"000001.SZ.QMT": 10, "000002.SZ.QMT": 20})
            self.assertEqual({c.stock_code for c in planner.requests[0].candidates},
                             {"000001.SZ", "000002.SZ"})
            self.assertTrue(all("opening_auction" not in (o.tags or [])
                                for o in engine.cache.orders()))
        finally:
            engine.dispose()

    def test_delayed_orders_cannot_join_already_cleared_auction(self):
        engine, strategy, planner, model = replay_fixture(
            submission_latency_ms=11000, auction_receive_delay_ms=2000)
        try:
            engine.run()
            self.assertFalse(engine.trader.generate_orders_report().empty)
            self.assertTrue(engine.trader.generate_fills_report().empty)
        finally:
            engine.dispose()

    def test_no_auction_buys_means_no_auction_sells(self):
        engine, strategy, planner, model = replay_fixture(continuous=True, second_day_exit=True,
                                                        starting_cash=25000)
        try:
            engine.run()
            sells = [order for order in engine.cache.orders() if order.side == OrderSide.SELL]
            self.assertEqual(sells, [])
            self.assertEqual(len(planner.requests), 2)
            self.assertEqual(sum(h.quantity for h in planner.requests[-1].current_holdings), 1500)
            self.assertEqual(strategy._auction_buy_committed_notional, 0)
            self.assertEqual(strategy._auction_sell_committed_cost, 0)
        finally:
            engine.dispose()

    def test_auction_sells_use_submitted_buy_notional_and_worst_holding_rank(self):
        engine, strategy, planner, model = replay_fixture(
            continuous=True, second_day_rotate=True, starting_cash=45000,
            auction_partial_min_notional=58000)
        try:
            with patch.object(strategy, "venue_sellable_quantity", return_value=Decimal(0)) as venue_sellable:
                engine.run()
            venue_sellable.assert_not_called()
            sells = [o for o in engine.cache.orders() if o.side == OrderSide.SELL]
            self.assertEqual({str(o.instrument_id): int(o.quantity) for o in sells},
                             {"000001.SZ.QMT": 1000})
            self.assertTrue(all("opening_auction" in o.tags for o in sells))
            self.assertTrue(all(o.filled_qty == 100 for o in sells))
            self.assertEqual(strategy._auction_buy_committed_notional, Decimal(10050))
            self.assertEqual(strategy._auction_sell_committed_cost, Decimal(9955))
            self.assertGreater(strategy._auction_budget, Decimal(20000))
            self.assertLessEqual(strategy._auction_sell_committed_cost,
                                 strategy._auction_buy_committed_notional)
            self.assertEqual(len(planner.requests), 2)
            self.assertEqual(sum(h.quantity for h in planner.requests[-1].current_holdings), 1500)
            sell_events = [e for e in strategy.order_events if e.side == "sell" and e.status == "submitted"]
            self.assertEqual(sell_events[0].extra["intent_context"]["sellable_source"],
                             "auction_starting_position")
        finally:
            engine.dispose()

    def test_no_affordable_auction_buys_also_suppresses_sells(self):
        engine, strategy, planner, model = replay_fixture(
            continuous=True, second_day_rotate=True, starting_cash=21000,
            auction_partial_min_notional=58000)
        try:
            engine.run()
            self.assertEqual(len(planner.requests), 2)
            self.assertTrue(planner.requests[-1].current_holdings)
            self.assertFalse(any(o.side == OrderSide.SELL for o in engine.cache.orders()))
            self.assertEqual(strategy._auction_buy_committed_notional, 0)
            self.assertEqual(strategy._auction_sell_committed_cost, 0)
            self.assertTrue(any(e.instrument_id == "000003.SZ.QMT"
                                and e.reason == "partial_below_min_notional"
                                for e in strategy.order_events))
        finally:
            engine.dispose()

    def test_exchange_forced_sell_slices_reserve_own_pending_orders_and_each_fee(self):
        engine, strategy, planner, model = replay_fixture(
            continuous=True, second_day_rotate=True, starting_cash=45000,
            auction_partial_min_notional=58000)
        instrument = engine.cache.instrument(strategy._instrument_by_stock["000001.SZ"])
        instrument.info["limit_order_quantity_rules"]["max_quantity"] = 600
        try:
            engine.run()
            sells = sorted((o for o in engine.cache.orders() if o.side == OrderSide.SELL),
                           key=lambda o: str(o.client_order_id))
            self.assertEqual([int(o.quantity) for o in sells], [600, 400])
            self.assertEqual(strategy._auction_sell_committed_cost, Decimal(9960))
            self.assertLessEqual(strategy._auction_sell_committed_cost,
                                 strategy._auction_buy_committed_notional)
            submitted = [e.extra["intent_context"] for e in strategy.order_events
                         if e.side == "sell" and e.status == "submitted"]
            self.assertEqual([e["open_sell_qty"] for e in submitted], [Decimal(0), Decimal(600)])
        finally:
            engine.dispose()

    def test_missing_final_volume_never_fills_indicative_quotes(self):
        engine, strategy, planner, model = replay_fixture(final_results=False)
        try:
            engine.run()
            self.assertFalse(engine.trader.generate_orders_report().empty)
            self.assertTrue(engine.trader.generate_order_fills_report().empty)
            self.assertEqual(strategy._today_open, {})
        finally:
            engine.dispose()

    def test_residuals_fill_in_continuous_session_without_duplicate_orders(self):
        engine, strategy, planner, model = replay_fixture(continuous=True)
        try:
            engine.run()
            fills = engine.trader.generate_fills_report()
            self.assertEqual(len(planner.requests), 1)
            self.assertEqual(len(engine.cache.orders()), 2)
            auction = [f for _, f in fills.iterrows()
                       if pd.Timestamp(f.ts_event).tz_convert("Asia/Shanghai").hour == 9
                       and pd.Timestamp(f.ts_event).tz_convert("Asia/Shanghai").minute == 25]
            self.assertEqual(sum(int(f.last_qty) for f in auction), 200)
            self.assertEqual(sum(int(f.last_qty) for _, f in fills.iterrows()), 700)
            self.assertGreaterEqual(strategy._nautilus_free_cash(), 0)
            report = auction_execution_report(engine, strategy, model)
            self.assertEqual(report.auction_filled_quantity.sum(), 200)
            self.assertEqual(report.auction_remaining_quantity.sum(), 500)
        finally:
            engine.dispose()

    def test_tick_portfolio_reconstruction_includes_real_fill_fees(self):
        engine, strategy, planner, model = replay_fixture(continuous=True)
        try:
            engine.run()
            bars = {}
            for code, price in [("000001.SZ", 10), ("000002.SZ", 20)]:
                instrument = engine.cache.instrument(next(i for i in strategy._instrument_ids
                                                          if str(i) == f"{code}.QMT"))
                ts = timestamp("2026-07-08 15:00:00")
                px = instrument.make_price(price)
                bars[code] = [Bar(BarType.from_str(f"{instrument.id}-1-DAY-LAST-EXTERNAL"),
                                  px, px, px, px, instrument.make_qty(100000), ts, ts)]
            args = SimpleNamespace(exchange_timezone="Asia/Shanghai", starting_cash=Decimal(12500),
                                   start="2026-07-08", end="2026-07-08")
            report = build_complete_report(args, engine, strategy, bars)
            self.assertAlmostEqual(float(report["daily_portfolio"].iloc[-1]["cash"]),
                                   float(strategy._nautilus_free_cash()), places=2)
            self.assertEqual(report["auction_execution"].auction_filled_quantity.sum(), 200)
        finally:
            engine.dispose()

    def test_cancel_gate_blocks_all_preopen_and_outside_configured_window(self):
        engine, strategy, planner, model = replay_fixture()
        try:
            engine.run()
            order = engine.cache.orders()[0]
            for clock_time in ["09:24:30", "09:29:59", "11:30:00", "14:57:00", "15:00:00"]:
                ts = timestamp(f"2026-07-08 {clock_time}")
                strategy.clock.set_time(ts)
                strategy._last_market_data_ts_event = ts
                self.assertFalse(strategy._within_cancel_window())
                strategy.cancel_order(order)
                strategy.cancel_orders([order])
                strategy.cancel_all_orders(order.instrument_id)
                self.assertFalse(order.is_pending_cancel)
            for clock_time in ["09:30:00", "14:30:00", "14:56:59"]:
                ts = timestamp(f"2026-07-08 {clock_time}")
                strategy.clock.set_time(ts)
                strategy._last_market_data_ts_event = ts
                self.assertTrue(strategy._within_cancel_window())
        finally:
            engine.dispose()

    def test_restart_restores_rank_and_frozen_target_metadata(self):
        engine, strategy, planner, model = replay_fixture()
        try:
            rows = [{"instrument_id": "000001.SZ.QMT", "target_qty": 1000,
                     "target_version": "persisted-auction", "reason": "test",
                     "extra": {"execution_phase": "opening_auction", "prediction_rank": 2}}]
            strategy.configure_live_target_portfolio_loader(lambda day, signal: rows)
            engine.run()
            self.assertEqual(planner.requests, [])
            self.assertIn(date(2026, 7, 8), strategy._auction_frozen_dates)
            self.assertEqual(strategy._auction_prediction_ranks["000001.SZ.QMT"], 2)
            self.assertEqual(len(engine.cache.orders()), 1)
        finally:
            engine.dispose()

    def test_ricequant_ordinary_ticks_supply_indicative_and_final_auction(self):
        provider = ClickHouseRicequantTickDataProvider(ClickHouseConnectionConfig(), data_latency_ms=100)
        common = dict(order_book_id="000001.XSHE", tick_sequence=0, prev_close=11.57,
                      total_turnover=0, last=11.57, a1_v=180000, b1_v=180000)
        raw = [
            dict(common, datetime="2026-10-14 09:24:03.000000000", open=0, volume=0, a1=11.57, b1=11.57),
            dict(common, datetime="2026-10-14 09:25:00.000000000", open=11.57, volume=361200, a1=11.58, b1=11.57),
            dict(common, datetime="2026-10-14 09:30:00.000000000", open=11.57, volume=424100, a1=11.59, b1=11.58),
        ]
        ticks = provider.build_ticks(raw, {"000001.XSHE": "000001.SZ"})
        self.assertEqual([tick.kind for tick in ticks], ["indicative", "auction", "quote"])
        self.assertEqual(ticks[1].bid_price, 11.57)
        self.assertEqual(ticks[1].bid_size, 361200)
        self.assertEqual(ticks[1].ask_size, 361200)
        self.assertEqual(ticks[2].ask_size, 180000)
        self.assertEqual(ticks[0].ts_init - ticks[0].ts_event, 100_000_000)
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            provider.build_ticks([raw[0], raw[0]], {"000001.XSHE": "000001.SZ"})
        with patch.object(provider.client, "fetch_json_each_row", return_value=raw) as fetch:
            self.assertEqual(len(provider.load(start="2026-10-14", end="2026-10-14", symbols={"000001.SZ"})), 3)
            self.assertIn("000001.XSHE", fetch.call_args.args[0])
            self.assertIn("PREWHERE trading_date", fetch.call_args.args[0])
        with self.assertRaisesRegex(ValueError, "Unsupported stock code"):
            provider.load(start="2026-10-14", end="2026-10-14", symbols={"920001.BJ"})

    def test_runner_builds_tick_mode_without_daily_matching(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "strategy.yaml"
            config.write_text("auction_enabled: true\nalpha_model_id: test\nrisk_manager_mode: backtest\n")
            data = Path(directory) / "ticks.csv"
            with data.open("w", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=sorted(HistoricalTickDataProvider.COLUMNS))
                writer.writeheader()
                for clock_time, kind in [("09:24:01", "indicative"), ("09:24:50", "indicative"),
                                         ("09:25:00", "auction")]:
                    for code, price in [("000001.SZ", 10), ("000002.SZ", 20)]:
                        writer.writerow(dict(symbol=code, kind=kind,
                            ts_event=f"2026-07-08T{clock_time}+08:00", ts_init=f"2026-07-08T{clock_time}+08:00",
                            bid_price=price, ask_price=price, bid_size=10000, ask_size=10000,
                            previous_close=price, open_price=price if kind == "auction" else 0))
            args = SimpleNamespace(config=str(config), tick_file=str(data), tick_table=None, start="2026-07-08", end="2026-07-08",
                                   trader_id="TEST-001", log_level="ERROR", max_positions=50,
                                   starting_cash=Decimal(12500), exchange_timezone="Asia/Shanghai",
                                   auction_participation_rate=.01, submission_latency_ms=0,
                                   cancel_latency_ms=100, commission_rate=.0003,
                                   minimum_commission=5, stamp_duty_rate=.0005)
            bundle = SimpleNamespace(instrument_names={},
                signals_by_date={date(2026, 7, 7): [SimpleNamespace(signal_date=date(2026, 7, 7),
                    stock_code=code, score=.1, rank=rank, pred_return_live=.1)
                    for code, rank in [("000001.SZ", 2), ("000002.SZ", 1)]]},
                prediction_ranks_by_date={date(2026, 7, 7): {"000001.SZ": 2, "000002.SZ": 1}},
                trading_dates=[date(2026, 7, 7), date(2026, 7, 8)],
                listed_dates={code: date(2020, 1, 1) for code in ["000001.SZ", "000002.SZ"]},
                st_by_date={}, suspended_by_date={})
            bars = {}
            bar_types = {}
            daily = []
            for code, price in [("000001.SZ", 10), ("000002.SZ", 20)]:
                instrument = parse_equity(code, {}, 0, 0)
                bar_type = BarType.from_str(f"{instrument.id}-1-DAY-LAST-EXTERNAL")
                px = instrument.make_price(price)
                ts = timestamp("2026-07-08 15:00:00")
                bars[code] = [Bar(bar_type, px, px, px, px, instrument.make_qty(100000), ts, ts)]
                bar_types[code] = bar_type
                daily.append(DailyStockData(stock_code=code, trade_date=date(2026, 7, 8),
                                            up_limit=price * 1.1, down_limit=price * .9))
            with patch("nautilus_trader.config.LoggingConfig", return_value=LoggingConfig(bypass_logging=True)):
                engine, strategy, valuation_bars = build_engine(args, bundle, bar_types, bars, tuple(daily))
            try:
                strategy._target_planner = FixedPlanner()
                engine.run()
                self.assertEqual(len(engine.cache.orders()), 1)
                self.assertEqual(engine.trader.generate_fills_report().last_qty.astype(int).sum(), 100)
                self.assertIs(valuation_bars, bars)
                self.assertEqual(strategy.config.auction_commission_rate, args.commission_rate)
                self.assertEqual(strategy.config.auction_minimum_fee, args.minimum_commission)
                report = build_complete_report(args, engine, strategy, bars)
                self.assertIn("auction_execution", report)
            finally:
                engine.dispose()

    def test_csv_rejects_future_open_and_duplicate_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ticks.csv"
            row = dict(symbol="000001.SZ", kind="indicative", ts_event="2026-07-08T09:24:00+08:00",
                       ts_init="2026-07-08T09:24:00+08:00", bid_price=10, ask_price=10,
                       bid_size=1000, ask_size=1000, previous_close=10, open_price=0)
            def write(rows):
                with path.open("w", newline="") as target:
                    writer = csv.DictWriter(target, fieldnames=sorted(HistoricalTickDataProvider.COLUMNS))
                    writer.writeheader()
                    writer.writerows(rows)
            provider = HistoricalTickDataProvider()
            kwargs = dict(start="2026-07-08", end="2026-07-08", symbols={"000001.SZ"})
            write([row])
            self.assertEqual(len(provider.load(str(path), **kwargs)), 1)
            write([row, row])
            with self.assertRaisesRegex(ValueError, "duplicate"):
                provider.load(str(path), **kwargs)
            write([{**row, "open_price": 10}])
            with self.assertRaisesRegex(ValueError, "open_price=0"):
                provider.load(str(path), **kwargs)
