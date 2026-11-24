from __future__ import annotations

import csv
from datetime import date
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from msgspec.structs import replace as replace_config

from nautilus_trader.adapters.qmt.common import parse_equity
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.data import Bar

from backtests.data_providers import PredictionDataBundle, PredictionSignal
from backtests.data_providers.clickhouse import ClickHouseConnectionConfig
from backtests.data_providers.historical_ticks import HistoricalTickDataProvider
from backtests.data_providers.ricequant_ticks import ClickHouseRicequantTickDataProvider
from backtests.target_model_predictions import run_backtest as runner
from backtests.time_range import BacktestTimeRange
from market_data import DailyStockData
from strategies.target_quantities import TargetQuantityOrderEvent
from tests.test_opening_auction import FixedPlanner, replay_fixture, timestamp
from backtests.target_model_predictions.exposure_reporting import ExposureReplayRecorder, exposure_replay_reports


DAY = date(2026, 7, 8)
CODES = ("000001.SZ", "000002.SZ")


class TickReplayTimeRangeTest(unittest.TestCase):
    def test_correction_report_attributes_by_order_and_separates_late_fills(self):
        def order(order_id, version):
            return TargetQuantityOrderEvent(order_id, DAY, '000001.SZ.QMT', 'buy', 100,
                Decimal(100), 'submitted', None, {'target_version': version,
                    'intent_context': {'execution_phase': 'continuous'}})
        strategy = SimpleNamespace(order_events=[order('correction', 'exposure:day:1'),
            order('normal', 'formal:day:1')], config=SimpleNamespace(timezone_name='Asia/Shanghai'),
            orchestration=SimpleNamespace(config=SimpleNamespace(exposure=SimpleNamespace(
                windows='09:35-09:55')), engine=SimpleNamespace(records=[])))
        fills = pd.DataFrame([dict(client_order_id=oid, instrument_id='000001.SZ.QMT',
            ts_event=pd.Timestamp(stamp, tz='Asia/Shanghai'), order_side=side,
            last_qty=qty, last_px=price) for oid, stamp, side, qty, price in (
                ('correction', '2026-07-08 09:40', 'BUY', 100, 10),
                ('normal', '2026-07-08 09:45', 'BUY', 100, 20),
                ('correction', '2026-07-08 09:55', 'SELL', 50, 12))])
        reports = exposure_replay_reports(strategy, [{'trading_date': DAY.isoformat(),
                                                       'stage': 'session_end'}], fills)
        summary = reports['daily_exposure_correction'].iloc[0]
        self.assertEqual(summary.correction_buy_amount, Decimal(1000))
        self.assertEqual(summary.correction_sell_amount, Decimal(600))
        self.assertEqual(summary.late_correction_fill_amount, Decimal(600))
        self.assertEqual(reports['execution_fill_attribution'].phase.tolist(),
                         ['exposure_correction', 'normal_execution', 'exposure_correction'])

    def test_daily_window_repeats_and_clips_global_datetime_boundaries(self):
        window = BacktestTimeRange.parse('2026-07-08 09:20', '2026-07-09 10:00',
                                         daily_window='09:15-10:30')
        self.assertEqual([(a.strftime('%m-%d %H:%M'), b.strftime('%m-%d %H:%M'))
                          for a, b in window.sessions],
                         [('07-08 09:20', '07-08 10:30'), ('07-09 09:15', '07-09 10:00')])
        for stamp, expected in (('2026-07-08 09:19:59.999999999', False),
                                ('2026-07-08 09:20', True), ('2026-07-08 10:30', False),
                                ('2026-07-08 14:30', False), ('2026-07-09 09:15', True),
                                ('2026-07-09 10:00', False)):
            self.assertEqual(window.contains(timestamp(stamp)), expected)

    def test_invalid_daily_windows_fail(self):
        for value in ('9:15-10:30', '09:15', '10:30-09:15', '09:15-09:15', '25:00-26:00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BacktestTimeRange.parse('2026-07-08', '2026-07-09', daily_window=value)
        with self.assertRaises(ValueError):
            BacktestTimeRange.parse('2026-07-08 14:00', '2026-07-08 15:00', daily_window='09:15-10:30')

    def test_ricequant_daily_window_queries_only_each_morning(self):
        provider = ClickHouseRicequantTickDataProvider(ClickHouseConnectionConfig(), data_latency_ms=250)
        rows = [[dict(self.ricequant_row('09:30'), datetime=f'2026-07-{day} 09:30')]
                for day in ('08', '09')]
        with patch.object(provider.client, 'fetch_json_each_row', side_effect=rows) as fetch:
            ticks = provider.load(start='2026-07-08', end='2026-07-09', symbols={CODES[0]},
                                  daily_window='09:15-10:30')
        self.assertEqual(len(ticks), 2)
        for day, call in zip(('08', '09'), fetch.call_args_list):
            self.assertIn(f"datetime >= toDateTime64('2026-07-{day} 09:14:59.750000000'", call.args[0])
            self.assertIn(f"datetime < toDateTime64('2026-07-{day} 10:29:59.750000000'", call.args[0])

    def test_csv_daily_window_filters_each_day_by_receipt_time(self):
        rows = [self.csv_row(f'2026-07-{day}T{clock}+08:00')
                for day in ('08', '09') for clock in ('09:30', '10:29:59.999999999', '10:30', '14:30')]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ticks.csv'
            self.write_csv(path, rows)
            ticks = HistoricalTickDataProvider().load(str(path), start='2026-07-08', end='2026-07-09',
                                                       symbols={CODES[0]}, daily_window='09:15-10:30')
        self.assertEqual(len(ticks), 4)
        self.assertEqual({pd.Timestamp(t.ts_init, unit='ns', tz='UTC').tz_convert('Asia/Shanghai').date()
                          for t in ticks}, {DAY, date(2026, 7, 9)})

    def test_daily_replay_keeps_native_inventory_cash_and_t_plus_one(self):
        window = BacktestTimeRange.parse('2026-07-08', '2026-07-09', daily_window='09:15-10:30')
        def morning_ticks(rows):
            result = [r for r in rows if window.contains(r.ts_init)]
            for day in ('08', '09'):
                for r in rows:
                    if r.ts_init == timestamp(f'2026-07-{day} 09:25'):
                        for clock in ('10:00:01', '10:00:31', '10:01:01'):
                            stamp = timestamp(f'2026-07-{day} {clock}')
                            result.append(replace(r, kind='quote', ts_event=stamp, ts_init=stamp))
            return result
        engine, strategy, planner, _ = replay_fixture(starting_cash=50000, second_day_exit=True,
            transform_rows=morning_ticks, transform_config=lambda c: replace_config(c,
                execution_windows='10:00-10:30', exchange_execution_windows='10:00-10:30'))
        strategy.configure_replay_time_range(window)
        sellability = {}
        class SellabilityRecorder(ExposureReplayRecorder):
            def capture(self, stage):
                super().capture(stage)
                day = strategy._clock_date()
                sellability[(day, stage)] = {
                    iid: strategy.planning_sellable_quantity(iid, day)
                    for iid in strategy._held_instrument_ids()}
        recorder = SellabilityRecorder(strategy, window)
        engine.add_actor(recorder)
        try:
            runner.run_engine(engine, SimpleNamespace(start='2026-07-08', end='2026-07-09',
                exchange_timezone='Asia/Shanghai', daily_window='09:15-10:30', tick_file=None, tick_table='test'))
            fills = engine.trader.generate_fills_report()
            sell_days = [pd.Timestamp(r.ts_event).tz_convert('Asia/Shanghai').date()
                         for r in fills.itertuples() if str(r.order_side) == 'SELL']
            self.assertTrue(sell_days)
            self.assertEqual(set(sell_days), {date(2026, 7, 9)})
            self.assertEqual(len(planner.requests), 2)
            holdings = planner.requests[1].current_holdings
            self.assertEqual({h.stock_code: h.quantity for h in holdings},
                             {'000001.SZ': 1000, '000002.SZ': 500})
            self.assertTrue(all(h.can_sell for h in holdings))
            self.assertEqual(set(sellability[(DAY, 'session_end')].values()), {0})
            self.assertEqual(sellability[(date(2026, 7, 9), 'correction_0_before')],
                             {'000001.SZ.QMT': 1000, '000002.SZ.QMT': 500})
            self.assertTrue(all(window.contains(o.ts_init) for o in engine.cache.orders()))
            self.assertTrue(all(window.contains(pd.Timestamp(t).value) for t in fills.ts_event))
            self.assertTrue(recorder.rows)
            self.assertEqual({r['trading_date'] for r in recorder.rows}, {'2026-07-08', '2026-07-09'})
            cash = strategy.portfolio.account(venue=strategy._instrument_ids[0].venue).balance_total().as_decimal()
            self.assertEqual(cash, Decimal(49970))
        finally:
            engine.dispose()

    def test_date_only_range_includes_end_date(self):
        window = BacktestTimeRange.parse("2026-07-08", "2026-07-08")
        self.assertFalse(window.has_time)
        self.assertTrue(window.contains(timestamp("2026-07-08 23:59:59.999999999")))
        self.assertFalse(window.contains(timestamp("2026-07-09 00:00")))

    def test_minute_range_is_start_inclusive_end_exclusive(self):
        window = BacktestTimeRange.parse("2026-07-08 14:30", "2026-07-08 14:31")
        self.assertTrue(window.has_time)
        self.assertTrue(window.contains(timestamp("2026-07-08 14:30")))
        self.assertTrue(window.contains(timestamp("2026-07-08 14:30:59.999999999")))
        self.assertFalse(window.contains(timestamp("2026-07-08 14:31")))
        self.assertFalse(window.contains(timestamp("2026-07-08 14:29:59.999999999")))

    def test_offsets_and_midnight_use_exchange_calendar_dates(self):
        window = BacktestTimeRange.parse("2026-07-08T06:30Z", "2026-07-08T16:00Z")
        self.assertEqual(window.start, pd.Timestamp("2026-07-08 14:30", tz="Asia/Shanghai"))
        self.assertEqual(window.start_date, DAY)
        self.assertEqual(window.end_date, DAY)

    def test_invalid_and_empty_intervals_fail(self):
        for first, last in (("14:30", "14:31"), ("2026-07-08 14:30", "2026-07-08 14:30"),
                            ("2026-07-09", "2026-07-08 14:30"), ("2026-02-30", "2026-03-01")):
            with self.subTest(first=first, last=last), self.assertRaises(ValueError):
                BacktestTimeRange.parse(first, last)

    def csv_row(self, event, receive=None, *, code=CODES[0], price=10):
        return dict(symbol=code, kind="quote", ts_event=event, ts_init=receive or event,
                    bid_price=price, ask_price=price, bid_size=10000, ask_size=10000,
                    previous_close=price, open_price=price)

    def write_csv(self, path, rows):
        with path.open("w", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=sorted(HistoricalTickDataProvider.COLUMNS))
            writer.writeheader()
            writer.writerows(rows)

    def test_csv_filters_by_receive_time_at_both_boundaries(self):
        rows = [
            self.csv_row("2026-07-08T14:29:59+08:00"),
            self.csv_row("2026-07-08T14:29:59.750+08:00", "2026-07-08T14:30:00+08:00"),
            self.csv_row("2026-07-08T14:30:59.999999999+08:00"),
            self.csv_row("2026-07-08T14:30:59.750+08:00", "2026-07-08T14:31:00+08:00"),
            self.csv_row("2026-07-08T14:31:30+08:00"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ticks.csv"
            self.write_csv(path, rows)
            provider = HistoricalTickDataProvider()
            result = provider.load(str(path), start="2026-07-08 14:30", end="2026-07-08 14:31",
                                   symbols={CODES[0]})
            self.assertEqual([row.ts_init for row in result],
                             [timestamp("2026-07-08 14:30"), timestamp("2026-07-08 14:30:59.999999999")])
            self.assertEqual(len(provider.load(str(path), start="2026-07-08", end="2026-07-08",
                                               symbols={CODES[0]})), 5)
            with self.assertRaisesRegex(ValueError, "No historical ticks"):
                provider.load(str(path), start="2026-07-08 14:32", end="2026-07-08 14:33", symbols={CODES[0]})

    def ricequant_row(self, clock):
        return dict(order_book_id="000001.XSHE", datetime=f"2026-07-08 {clock}", tick_sequence=0,
                    last=10, open=10, prev_close=10, volume=1000, total_turnover=10000,
                    a1=10, b1=10, a1_v=1000, b1_v=1000)

    def test_ricequant_pushes_minute_and_receive_delay_bounds_into_sql(self):
        provider = ClickHouseRicequantTickDataProvider(ClickHouseConnectionConfig(), data_latency_ms=250)
        rows = [self.ricequant_row(clock) for clock in
                ("14:29:59.500", "14:29:59.750", "14:30:59.749999999", "14:30:59.750")]
        with patch.object(provider.client, "fetch_json_each_row", return_value=rows) as fetch:
            result = provider.load(start="2026-07-08 14:30", end="2026-07-08 14:31", symbols={CODES[0]})
        sql = fetch.call_args.args[0]
        self.assertIn("datetime >= toDateTime64('2026-07-08 14:29:59.750000000'", sql)
        self.assertIn("datetime < toDateTime64('2026-07-08 14:30:59.750000000'", sql)
        self.assertIn("PREWHERE trading_date = toDate('2026-07-08')", sql)
        self.assertEqual([row.ts_init for row in result],
                         [timestamp("2026-07-08 14:30"), timestamp("2026-07-08 14:30:59.999999999")])

    def test_ricequant_midnight_end_does_not_query_an_extra_day(self):
        provider = ClickHouseRicequantTickDataProvider(ClickHouseConnectionConfig())
        with patch.object(provider.client, "fetch_json_each_row", return_value=[self.ricequant_row("14:30")]) as fetch:
            provider.load(start="2026-07-08 14:30", end="2026-07-09 00:00", symbols={CODES[0]})
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("datetime < toDateTime64('2026-07-09 00:00:00.000000000'", fetch.call_args.args[0])

    def test_ricequant_cross_day_range_prunes_each_partition(self):
        provider = ClickHouseRicequantTickDataProvider(ClickHouseConnectionConfig())
        rows = [[self.ricequant_row("14:30")], [{**self.ricequant_row("09:30"), "datetime": "2026-07-09 09:30"}]]
        with patch.object(provider.client, "fetch_json_each_row", side_effect=rows) as fetch:
            result = provider.load(start="2026-07-08 14:30", end="2026-07-09 09:31", symbols={CODES[0]})
        self.assertEqual(len(result), 2)
        self.assertEqual(fetch.call_count, 2)
        self.assertIn("toDate('2026-07-08')", fetch.call_args_list[0].args[0])
        self.assertIn("toDate('2026-07-09')", fetch.call_args_list[1].args[0])

    def config(self, path, *, auction=True):
        path.write_text(
            f"auction_enabled: {str(auction).lower()}\n"
            "risk_manager_mode: backtest\n"
            "alpha_model_id: test\n"
            "risk_manager_base_url: http://127.0.0.1:1\n"
            "risk_manager_risk_model_id: test\n"
            "process_targets_interval_secs: 2\n"
            "resubmit_check_interval_secs: 2\n",
            encoding="utf-8",
        )

    def args(self, config, *flags):
        with patch("sys.argv", ["run_backtest", "--config", str(config), *flags]), \
             patch.dict("os.environ", {"BACKTEST_TICK_FILE": "", "BACKTEST_TICK_TABLE": ""}):
            return runner.parse_args()

    def test_cli_rejects_datetime_boundaries_without_tick_source(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "strategy.yaml"
            self.config(config, auction=False)
            with self.assertRaises(SystemExit) as error:
                self.args(config, "--start", "2026-07-08 14:30", "--end", "2026-07-08 14:31")
            self.assertEqual(error.exception.code, 2)

    def test_cli_validates_reversed_and_equal_minute_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "strategy.yaml"
            self.config(config)
            for end in ("2026-07-08 14:30", "2026-07-08 14:29"):
                with self.subTest(end=end), self.assertRaises(SystemExit):
                    self.args(config, "--tick-table", "ricequant_stock_tick", "--start", "2026-07-08 14:30", "--end", end)

    def bundle(self):
        return PredictionDataBundle(
            signals_by_date={date(2026, 7, 7): [PredictionSignal(date(2026, 7, 7), code, .1, rank, .1)
                                               for rank, code in enumerate(CODES, 1)]},
            prediction_ranks_by_date={date(2026, 7, 7): {code: rank for rank, code in enumerate(CODES, 1)}},
            universe=list(CODES), trading_dates=[date(2026, 7, 7), DAY],
            listed_dates={code: date(2020, 1, 1) for code in CODES}, st_by_date={}, suspended_by_date={},
            instrument_names={}, prediction_rows=2, selected_rows=2,
        )

    def test_daily_reference_queries_and_metadata_use_the_included_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "strategy.yaml"
            self.config(config)
            args = self.args(config, "--tick-table", "ricequant_stock_tick", "--start", "2026-07-08T06:30Z",
                             "--end", "2026-07-08T16:00Z")
            request = runner.build_prediction_request(args)
            self.assertEqual(request.start_date, "2026-07-08")
            self.assertEqual(request.end_date, "2026-07-08")
            with patch.object(runner, "ClickHouseBarDataProvider") as provider:
                provider.return_value.prepare_bars.return_value.skipped_rows = 0
                provider.return_value.prepare_bars.return_value.bars = []
                runner.load_bars(args, ClickHouseConnectionConfig(), self.bundle())
            for call in provider.return_value.prepare_bars.call_args_list:
                self.assertEqual(call.kwargs["start"], "2026-07-08")
                self.assertEqual(call.kwargs["end"], "2026-07-09")
            metadata = runner.run_config(args)
            self.assertEqual(metadata["base.start_date"], "2026-07-08")
            self.assertEqual(metadata["base.end_date"], "2026-07-08")
            self.assertEqual(metadata["base.start_time"], "2026-07-08T14:30:00+08:00")
            self.assertEqual(metadata["base.end_time_exclusive"], "2026-07-09T00:00:00+08:00")

    def test_runner_limits_midday_planning_market_data_and_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            config, data = Path(directory) / "strategy.yaml", Path(directory) / "ticks.csv"
            self.config(config)
            rows = [self.csv_row(f"2026-07-08T{clock}+08:00", code=code, price=price)
                    for clock in ("14:29:59", "14:30:00", "14:30:30", "14:31:00", "14:31:30")
                    for code, price in zip(CODES, (10, 20))]
            self.write_csv(data, rows)
            args = self.args(config, "--tick-file", str(data), "--start", "2026-07-08 14:30",
                             "--end", "2026-07-08 14:31", "--starting-cash", "25000")
            bars, bar_types, daily = {}, {}, []
            for code, price in zip(CODES, (10, 20)):
                instrument = parse_equity(code, {}, 0, 0)
                bar_type = runner.build_bar_type(code)
                px = instrument.make_price(price)
                ts = timestamp("2026-07-08 15:00")
                bars[code] = [Bar(bar_type, px, px, px, px, instrument.make_qty(100000), ts, ts)]
                bar_types[code] = bar_type
                daily.append(DailyStockData(code, DAY, up_limit=price * 1.1, down_limit=price * .9))
            with patch("nautilus_trader.config.LoggingConfig", return_value=LoggingConfig(bypass_logging=True)):
                engine, strategy, _ = runner.build_engine(args, self.bundle(), bar_types, bars, tuple(daily))
            planner, planning_times = FixedPlanner(), []

            def plan(request):
                planning_times.append(strategy.clock.timestamp_ns())
                return planner.plan(request)

            strategy._target_planner.plan = plan
            try:
                window = BacktestTimeRange.parse(args.start, args.end)
                self.assertTrue(all(window.contains(row.ts_init) for row in engine.data))
                runner.run_engine(engine, args)
                self.assertEqual(planning_times, [timestamp("2026-07-08 14:30:01")])
                self.assertTrue(engine.cache.orders())
                self.assertTrue(all(window.contains(order.ts_init) for order in engine.cache.orders()))
                self.assertFalse(any("opening_auction" in (order.tags or []) for order in engine.cache.orders()))
                fills = engine.trader.generate_fills_report()
                self.assertFalse(fills.empty)
                self.assertTrue(all(window.contains(pd.Timestamp(ts).value) for ts in fills.ts_event))
                self.assertLess(engine.backtest_end.value, window.end_ns)
            finally:
                engine.dispose()

    def test_engine_end_prevents_delayed_acceptance_after_the_interval(self):
        engine, strategy, planner, _ = replay_fixture(auction_enabled=False, continuous=True, submission_latency_ms=10000,
                                                      starting_cash=25000)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "strategy.yaml"
            self.config(config)
            args = self.args(config, "--tick-table", "ricequant_stock_tick", "--start", "2026-07-08 14:30",
                             "--end", "2026-07-08 14:30:04")
            try:
                runner.run_engine(engine, args)
                self.assertTrue(planner.requests)
                self.assertLess(engine.backtest_end.value, timestamp("2026-07-08 14:30:04"))
                self.assertTrue(engine.trader.generate_fills_report().empty)
            finally:
                engine.dispose()
