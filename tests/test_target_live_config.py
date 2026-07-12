from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import date
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import call
from unittest.mock import patch

import pandas as pd

from backtests.result_writers.live_records import AFTER_TRADING
from backtests.result_writers.live_records import BEFORE_TRADING
from backtests.result_writers.live_records import CONTINUOUS_TRADING
from backtests.result_writers.live_records import LiveTargetRecord
from backtests.result_writers.live_writer import LiveSnapshotWriter
from nautilus_trader.common.enums import LogColor

from lives.live_qmt_target_model_predictions import _emit_snapshot_status
from lives.live_qmt_target_model_predictions import parse_args
from lives.live_qmt_target_model_predictions import _resolve_daily_log_file_name
from lives.snapshot_recorder import SnapshotRecorder
from lives.snapshot_recorder import SnapshotRecorderConfig
from lives.live_qmt_target_model_predictions import normalize_refresh_time
from strategies.model_prediction_targets import TargetModelPredictionsStrategyConfig


class TargetLiveConfigTest(unittest.TestCase):
    def test_model_target_default_keeps_original_position_cap(self) -> None:
        config = TargetModelPredictionsStrategyConfig(
            instrument_ids=[],
            bar_types={},
            instrument_stock_codes={},
            signals_by_date={},
            trading_dates=[],
            listed_dates={},
            st_by_date={},
            suspended_by_date={},
        )
        self.assertEqual(config.max_position_percent, 0.03)
        self.assertFalse(config.process_targets_on_timer)

    def test_normalize_refresh_time_accepts_hh_mm_and_hh_mm_ss(self) -> None:
        self.assertEqual(normalize_refresh_time("9:00"), "09:00")
        self.assertEqual(normalize_refresh_time("09:00:30"), "09:00")

    def test_normalize_refresh_time_rejects_malformed_values(self) -> None:
        with self.assertRaises(ValueError):
            normalize_refresh_time("9")
        with self.assertRaises(ValueError):
            normalize_refresh_time("25:00")

    def test_pre_open_reconcile_time_defaults_to_0915(self) -> None:
        with patch.dict(
            os.environ,
            {"QMT_ACCOUNT_ID": "TEST", "MODEL_LIVE_PRE_OPEN_RECONCILE_TIME": "09:15"},
            clear=False,
        ):
            with patch.object(sys, "argv", ["test"]):
                args = parse_args()
        self.assertEqual(args.pre_open_reconcile_time, "09:15")

    def test_pre_open_reconcile_time_can_be_disabled(self) -> None:
        with patch.dict(os.environ, {"QMT_ACCOUNT_ID": "TEST"}, clear=False):
            with patch.object(sys, "argv", ["test", "--pre-open-reconcile-time", ""]):
                args = parse_args()
        self.assertIsNone(args.pre_open_reconcile_time)

    def test_full_tick_args_default(self) -> None:
        with patch.dict(os.environ, {"QMT_ACCOUNT_ID": "TEST"}, clear=False):
            with patch.object(sys, "argv", ["test"]):
                args = parse_args()
        self.assertEqual(args.full_tick_refresh_secs, 60.0)
        self.assertEqual(args.full_tick_prefetch_time, "09:27")

    def test_full_tick_prefetch_can_be_disabled(self) -> None:
        with patch.dict(os.environ, {"QMT_ACCOUNT_ID": "TEST"}, clear=False):
            with patch.object(
                sys,
                "argv",
                ["test", "--full-tick-prefetch-time", "", "--full-tick-refresh-secs", "0"],
            ):
                args = parse_args()
        self.assertEqual(args.full_tick_prefetch_time, "")
        self.assertEqual(args.full_tick_refresh_secs, 0.0)

    def test_full_tick_config_fields_default(self) -> None:
        config = TargetModelPredictionsStrategyConfig(
            instrument_ids=[],
            bar_types={},
            instrument_stock_codes={},
            signals_by_date={},
            trading_dates=[],
            listed_dates={},
            st_by_date={},
            suspended_by_date={},
        )
        self.assertEqual(config.full_tick_refresh_secs, 60.0)
        self.assertEqual(config.full_tick_prefetch_time, "09:27")

    def test_daily_log_file_name_defaults_to_model_preds_with_exchange_date(self) -> None:
        fake_now = SimpleNamespace(strftime=lambda fmt: "2026-07-08")
        with patch("lives.live_qmt_target_model_predictions.pd.Timestamp.now", return_value=fake_now):
            self.assertEqual(
                _resolve_daily_log_file_name(None, "Asia/Shanghai"),
                "model_preds-2026-07-08",
            )

    def test_daily_log_file_name_appends_date_to_configured_base(self) -> None:
        fake_now = SimpleNamespace(strftime=lambda fmt: "2026-07-08")
        with patch("lives.live_qmt_target_model_predictions.pd.Timestamp.now", return_value=fake_now):
            self.assertEqual(
                _resolve_daily_log_file_name("model_preds", "Asia/Shanghai"),
                "model_preds-2026-07-08",
            )

    def test_daily_log_file_name_preserves_existing_date(self) -> None:
        fake_now = SimpleNamespace(strftime=lambda fmt: "2026-07-08")
        with patch("lives.live_qmt_target_model_predictions.pd.Timestamp.now", return_value=fake_now):
            self.assertEqual(
                _resolve_daily_log_file_name("model_preds-2026-07-07", "Asia/Shanghai"),
                "model_preds-2026-07-07",
            )

    def test_daily_log_file_name_supports_date_placeholder(self) -> None:
        fake_now = SimpleNamespace(strftime=lambda fmt: "2026-07-08")
        with patch("lives.live_qmt_target_model_predictions.pd.Timestamp.now", return_value=fake_now):
            self.assertEqual(
                _resolve_daily_log_file_name("model_preds-{date}", "Asia/Shanghai"),
                "model_preds-2026-07-08",
            )

    def test_emit_snapshot_status_logs_info_to_nautilus_logger(self) -> None:
        logger = MagicMock()
        node = SimpleNamespace(get_logger=lambda: logger)
        with patch("builtins.print") as mock_print:
            _emit_snapshot_status(node, "[snapshot] recorder enabled")
        mock_print.assert_called_once_with("[snapshot] recorder enabled", flush=True)
        logger.info.assert_called_once_with("[snapshot] recorder enabled", color=LogColor.BLUE)

    def test_emit_snapshot_status_logs_warning_to_nautilus_logger(self) -> None:
        logger = MagicMock()
        node = SimpleNamespace(get_logger=lambda: logger)
        with patch("builtins.print") as mock_print:
            _emit_snapshot_status(node, "[snapshot] MySQL writer init failed", is_warning=True)
        mock_print.assert_called_once_with("[snapshot] MySQL writer init failed", flush=True)
        logger.warning.assert_called_once_with(
            "[snapshot] MySQL writer init failed",
            color=LogColor.YELLOW,
        )

    def test_snapshot_recorder_startup_during_trading_uses_continuous_only(self) -> None:
        recorder = self._make_snapshot_recorder_stub("2026-07-08 14:46:19")

        SnapshotRecorder._catch_up_on_start(recorder)

        recorder._run_before_trading.assert_not_called()
        recorder._run_continuous_trading.assert_called_once_with(date(2026, 7, 8), allow_fallback=False)
        recorder._run_after_trading.assert_not_called()

    def test_snapshot_recorder_startup_after_before_window_before_open_uses_continuous(self) -> None:
        recorder = self._make_snapshot_recorder_stub("2026-07-08 09:29:30")

        SnapshotRecorder._catch_up_on_start(recorder)

        recorder._run_before_trading.assert_not_called()
        recorder._run_continuous_trading.assert_called_once_with(date(2026, 7, 8), allow_fallback=True)
        recorder._run_after_trading.assert_not_called()

    def test_snapshot_recorder_post_close_start_uses_continuous_then_after(self) -> None:
        recorder = self._make_snapshot_recorder_stub("2026-07-08 15:41:00")

        SnapshotRecorder._catch_up_on_start(recorder)

        recorder._run_before_trading.assert_not_called()
        self.assertEqual(
            recorder._run_continuous_trading.call_args_list,
            [call(date(2026, 7, 8), allow_fallback=True)],
        )
        recorder._run_after_trading.assert_called_once_with(date(2026, 7, 8), allow_fallback=True)

    def test_writer_asset_snapshot_id_falls_back_to_continuous(self) -> None:
        writer = object.__new__(LiveSnapshotWriter)
        writer._query = MagicMock(side_effect=[[], [(7,)]])

        row_id = writer.asset_snapshot_id(
            "ACC",
            "TRADER",
            date(2026, 7, 8),
            BEFORE_TRADING,
            fallback_to_continuous=True,
        )

        self.assertEqual(row_id, 7)
        self.assertEqual(writer._query.call_count, 2)

    def test_writer_has_position_snapshot_falls_back_to_continuous(self) -> None:
        writer = object.__new__(LiveSnapshotWriter)
        writer._query = MagicMock(side_effect=[[], [(1,)]])

        found = writer.has_position_snapshot(
            "ACC",
            "TRADER",
            date(2026, 7, 8),
            BEFORE_TRADING,
            fallback_to_continuous=True,
        )

        self.assertTrue(found)
        self.assertEqual(writer._query.call_count, 2)

    def test_writer_load_target_portfolios_falls_back_to_continuous(self) -> None:
        writer = object.__new__(LiveSnapshotWriter)
        signal_date = date(2026, 7, 7)
        writer._query = MagicMock(
            side_effect=[
                [],
                [(
                    "000001.SZ.QMT",  # instrument_id
                    "000001.SZ",      # stock_code
                    signal_date,      # signal_date
                    9,                # asset_snapshot_id
                    7,                # position_snapshot_id
                    1000,             # total_asset
                    950,              # investable_asset
                    "req-1",          # request_id
                    "ver-1",          # target_version
                    0.1,              # target_weight
                    10.0,             # open_price
                    "open",           # price_source
                    1000,             # target_qty
                    1.2,              # score
                    "loaded_target",  # reason
                    CONTINUOUS_TRADING,  # snapshot_type
                )],
            ],
        )

        rows = writer.load_target_portfolios(
            "ACC",
            "TRADER",
            date(2026, 7, 8),
            signal_date,
            preferred_snapshot_type=BEFORE_TRADING,
            fallback_to_continuous=True,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["snapshot_type"], CONTINUOUS_TRADING)
        self.assertEqual(writer._query.call_count, 2)

    def test_writer_execute_retries_once_after_connection_lost(self) -> None:
        writer = object.__new__(LiveSnapshotWriter)
        writer._commit = False

        cursor = MagicMock()
        # First cursor.execute raises the classic dropped-connection error, second succeeds.
        cursor.execute.side_effect = [Exception(0, ""), None]
        connection = MagicMock()
        connection.cursor.return_value = cursor
        writer._connection = connection

        writer._execute("INSERT INTO t VALUES (%s)", (1,))

        self.assertEqual(cursor.execute.call_count, 2)
        connection.ping.assert_called_once_with(reconnect=True)

    def test_writer_execute_does_not_retry_on_non_connection_error(self) -> None:
        writer = object.__new__(LiveSnapshotWriter)
        writer._commit = False

        cursor = MagicMock()
        cursor.execute.side_effect = Exception(1062, "Duplicate entry")
        connection = MagicMock()
        connection.cursor.return_value = cursor
        writer._connection = connection

        with self.assertRaises(Exception):
            writer._execute("INSERT INTO t VALUES (%s)", (1,))

        self.assertEqual(cursor.execute.call_count, 1)
        connection.ping.assert_not_called()

    def test_writer_query_retries_once_after_connection_lost(self) -> None:
        writer = object.__new__(LiveSnapshotWriter)

        cursor = MagicMock()
        cursor.execute.side_effect = [Exception(2013, "Lost connection"), None]
        cursor.fetchall.return_value = [(7,)]
        connection = MagicMock()
        connection.cursor.return_value = cursor
        writer._connection = connection

        rows = writer._query("SELECT 1", ())

        self.assertEqual(rows, [(7,)])
        self.assertEqual(cursor.execute.call_count, 2)
        connection.ping.assert_called_once_with(reconnect=True)

    def test_writer_execute_logs_reconnect_failure_details(self) -> None:
        writer = object.__new__(LiveSnapshotWriter)
        writer._commit = False

        cursor = MagicMock()
        cursor.execute.side_effect = Exception(0, "")
        connection = MagicMock()
        connection.cursor.return_value = cursor
        connection.ping.side_effect = Exception(2006, "server has gone away")
        connection.connect.side_effect = Exception(2003, "can't connect")
        writer._connection = connection

        with patch("backtests.result_writers.live_writer._LOGGER") as logger:
            with self.assertRaises(Exception):
                writer._execute("INSERT INTO t VALUES (%s)", (1,))

        logger.warning.assert_any_call(
            "LiveSnapshotWriter ping(reconnect=True) failed: %r",
            connection.ping.side_effect,
        )
        logger.warning.assert_any_call(
            "LiveSnapshotWriter connect() failed during reconnect: %r",
            connection.connect.side_effect,
        )
        logger.warning.assert_any_call(
            "LiveSnapshotWriter failed to reconnect after connection-lost error: %r",
            cursor.execute.side_effect,
        )

    def test_writer_execute_logs_retry_failure_after_reconnect(self) -> None:
        writer = object.__new__(LiveSnapshotWriter)
        writer._commit = False

        first_exc = Exception(0, "")
        retry_exc = Exception(1064, "bad sql")
        cursor = MagicMock()
        cursor.execute.side_effect = [first_exc, retry_exc]
        connection = MagicMock()
        connection.cursor.return_value = cursor
        writer._connection = connection

        with patch("backtests.result_writers.live_writer._LOGGER") as logger:
            with self.assertRaises(Exception):
                writer._execute("INSERT INTO t VALUES (%s)", (1,))

        logger.warning.assert_any_call(
            "LiveSnapshotWriter retry after reconnect failed; original=%r retry=%r",
            first_exc,
            retry_exc,
        )
        connection.ping.assert_called_once_with(reconnect=True)

    def test_writer_write_target_portfolios_keys_by_snapshot_type(self) -> None:
        writer = object.__new__(LiveSnapshotWriter)
        writer._upsert_many = MagicMock()

        writer.write_target_portfolios(
            [
                LiveTargetRecord(
                    trade_date=date(2026, 7, 10),
                    write_time=datetime(2026, 7, 10, 9, 27),
                    snapshot_type=BEFORE_TRADING,
                    account_id="ACC",
                    trader_id="TRADER",
                    signal_date=date(2026, 7, 9),
                    asset_snapshot_id=1,
                    position_snapshot_id=2,
                    total_asset=Decimal("1000000"),
                    investable_asset=Decimal("950000"),
                    request_id="req-1",
                    target_version="ver-1",
                    instrument_id="000001.SZ.QMT",
                    stock_code="000001.SZ",
                    target_weight=Decimal("0.1"),
                    open_price=Decimal("10.0"),
                    price_source="open",
                    target_qty=10000,
                    score=Decimal("0.5"),
                    reason="risk_manager_optimize",
                    extra={},
                    created_at=datetime(2026, 7, 10, 9, 27),
                ),
            ],
        )

        self.assertEqual(writer._upsert_many.call_count, 1)
        self.assertEqual(
            writer._upsert_many.call_args.kwargs["key_columns"],
            (
                "account_id",
                "trader_id",
                "trade_date",
                "signal_date",
                "snapshot_type",
                "instrument_id",
            ),
        )

    def test_writer_ensure_target_indexes_upgrades_uk_target_to_include_snapshot_type(self) -> None:
        writer = object.__new__(LiveSnapshotWriter)
        writer._query = MagicMock(
            return_value=[
                ("live_target_portfolio", 0, "uk_target", 1, "account_id"),
                ("live_target_portfolio", 0, "uk_target", 2, "trader_id"),
                ("live_target_portfolio", 0, "uk_target", 3, "trade_date"),
                ("live_target_portfolio", 0, "uk_target", 4, "signal_date"),
                ("live_target_portfolio", 0, "uk_target", 5, "instrument_id"),
            ],
        )
        writer._execute = MagicMock()

        writer._ensure_target_indexes()

        self.assertEqual(
            writer._execute.call_args_list,
            [
                call("ALTER TABLE `live_target_portfolio` DROP INDEX `uk_target`", ()),
                call(
                    "ALTER TABLE `live_target_portfolio` "
                    "ADD UNIQUE KEY `uk_target` "
                    "(`account_id`,`trader_id`,`trade_date`,`signal_date`,`snapshot_type`,`instrument_id`)",
                    (),
                ),
            ],
        )

    def test_position_snapshot_uses_qmt_market_value_for_unprefixed_columns(self) -> None:
        writer = SimpleNamespace(
            has_position_snapshot=MagicMock(return_value=False),
            write_position_snapshots=MagicMock(),
        )
        recorder = SimpleNamespace()
        recorder.config = SnapshotRecorderConfig(account_id="ACC", trader_id="TRADER")
        recorder._writer = writer
        recorder._strategy = SimpleNamespace(_stock_by_instrument={"000720.SZ.QMT": "000720.SZ"})
        recorder.log = MagicMock()
        recorder._run_position_fetch = MagicMock(
            return_value={
                "000720.SZ.QMT": {
                    "stock_code": "000720.SZ",
                    "volume": "143100",
                    "can_use_volume": "107400",
                    "avg_price": "3.40",
                    "last_price": "3.26",
                    "market_value": "466506.00",
                    "raw": {
                        "stock_code": "000720.SZ",
                        "last_price": "3.26",
                        "market_value": "466506.00",
                    },
                },
            },
        )
        recorder._open_positions = MagicMock(
            return_value=[
                SimpleNamespace(
                    instrument_id="000720.SZ.QMT",
                    quantity=Decimal("143100"),
                    avg_px_open=Decimal("3.40"),
                    is_long=True,
                ),
            ],
        )
        recorder._now_naive = MagicMock(return_value=datetime(2026, 7, 8, 15, 40))
        recorder._stock_code = lambda instrument_id: SnapshotRecorder._stock_code(recorder, instrument_id)
        recorder._venue_can_use = MagicMock(return_value=Decimal("107400"))
        recorder._decimal_or_none = SnapshotRecorder._decimal_or_none
        recorder._nt_net_qty = MagicMock(return_value=None)
        recorder._strategy_last_close = MagicMock(return_value=3.32)
        recorder._market_value = SnapshotRecorder._market_value
        recorder._broker_position_for = SnapshotRecorder._broker_position_for
        recorder._broker_decimal = SnapshotRecorder._broker_decimal
        recorder._int_or_none = SnapshotRecorder._int_or_none
        recorder._position_qmt_raw = SnapshotRecorder._position_qmt_raw
        recorder._to_str = SnapshotRecorder._to_str
        recorder._record_positions_with_broker = (
            lambda trading_date, snapshot_type, source, broker_positions:
            SnapshotRecorder._record_positions_with_broker(
                recorder,
                trading_date,
                snapshot_type,
                source,
                broker_positions,
            )
        )

        SnapshotRecorder._record_positions(recorder, date(2026, 7, 8), AFTER_TRADING, "live")

        record = writer.write_position_snapshots.call_args.args[0][0]
        self.assertEqual(record.market_value, Decimal("466506.00"))
        self.assertEqual(record.nt_market_value, Decimal("475092.00"))
        self.assertEqual(record.nt_last_price, Decimal("3.32"))
        self.assertEqual(record.qmt_raw["market_value"], "466506.00")

    def test_position_fetch_schedules_awaitable_on_running_loop(self) -> None:
        recorder = SimpleNamespace()
        recorder._fetch_positions = AsyncMock(return_value={"000720.SZ.QMT": {"market_value": "466506.00"}})
        recorder._on_position_fetch_done = MagicMock()
        recorder.log = MagicMock()

        async def run_fetch() -> None:
            result = SnapshotRecorder._run_position_fetch(
                recorder,
                date(2026, 7, 8),
                AFTER_TRADING,
                "live",
            )
            self.assertIsNone(result)
            await asyncio.sleep(0)

        asyncio.run(run_fetch())

        recorder._on_position_fetch_done.assert_called_once()
        task = recorder._on_position_fetch_done.call_args.args[0]
        self.assertEqual(task.result(), {"000720.SZ.QMT": {"market_value": "466506.00"}})

    def test_order_event_truncates_reason_for_live_order_but_keeps_full_payload(self) -> None:
        writer = SimpleNamespace(upsert_order=MagicMock())
        recorder = SimpleNamespace()
        recorder.config = SnapshotRecorderConfig(account_id="ACC", trader_id="TRADER")
        recorder._writer = writer
        recorder._strategy = SimpleNamespace(
            _stock_by_instrument={},
            _order_target_weights={},
            _order_target_versions={},
        )
        recorder.cache = SimpleNamespace(order=MagicMock(return_value=None))
        recorder._now_naive = MagicMock(return_value=datetime(2026, 7, 9, 13, 21, 43))
        recorder._event_date = MagicMock(return_value=date(2026, 7, 9))
        recorder._stock_code = lambda instrument_id: SnapshotRecorder._stock_code(recorder, instrument_id)
        recorder._order_target_weight = lambda client_order_id: SnapshotRecorder._order_target_weight(
            recorder,
            client_order_id,
        )
        recorder._order_target_version = lambda client_order_id: SnapshotRecorder._order_target_version(
            recorder,
            client_order_id,
        )
        recorder._decimal_or_none = SnapshotRecorder._decimal_or_none
        recorder._int_or_none = SnapshotRecorder._int_or_none
        recorder._maybe_str = SnapshotRecorder._maybe_str
        recorder._order_side_text = SnapshotRecorder._order_side_text
        recorder._order_type_text = SnapshotRecorder._order_type_text
        recorder._order_status_text = SnapshotRecorder._order_status_text
        recorder._bounded_order_reason = SnapshotRecorder._bounded_order_reason
        recorder._order_event_payload = SnapshotRecorder._order_event_payload

        long_reason = (
            "CUM_NOTIONAL_EXCEEDS_FREE_BALANCE: free=21642.68 CNY, "
            "cum_notional=299052.00 CNY"
        )
        event = type(
            "OrderDeniedEvent",
            (),
            {
                "client_order_id": "O-1",
                "instrument_id": "001202.SZ.QMT",
                "reason": long_reason,
                "info": {"broker_code": "CUM_NOTIONAL_EXCEEDS_FREE_BALANCE"},
            },
        )()

        SnapshotRecorder._upsert_order_from_event(recorder, event)

        record = writer.upsert_order.call_args.args[0]
        self.assertEqual(record.client_order_id, "O-1")
        self.assertEqual(len(record.reason), SnapshotRecorder._LIVE_ORDER_REASON_MAX_LEN)
        self.assertTrue(record.reason.endswith("..."))
        self.assertEqual(record.qmt_raw["reason"], long_reason)
        self.assertEqual(record.qmt_raw["broker_code"], "CUM_NOTIONAL_EXCEEDS_FREE_BALANCE")

    def test_order_status_uses_enum_name_for_live_order_status(self) -> None:
        class EnumLikeStatus:
            name = "ACCEPTED"

            def __str__(self) -> str:
                return "7"

        status = EnumLikeStatus()

        text = SnapshotRecorder._order_status_text(
            SimpleNamespace(status=status),
            SimpleNamespace(),
        )

        self.assertEqual(text, "accepted")

    def test_order_status_falls_back_to_string_when_name_missing(self) -> None:
        class StatusWithoutName:
            def __str__(self) -> str:
                return "pending_review"

        text = SnapshotRecorder._order_status_text(
            SimpleNamespace(status=StatusWithoutName()),
            SimpleNamespace(),
        )

        self.assertEqual(text, "pending_review")

    @staticmethod
    def _make_snapshot_recorder_stub(now_text: str) -> SimpleNamespace:
        recorder = SimpleNamespace()
        recorder.config = SnapshotRecorderConfig(account_id="ACC", trader_id="TRADER")
        recorder._run_full_tick_fetch = MagicMock()
        recorder._run_before_trading = MagicMock()
        recorder._run_continuous_trading = MagicMock()
        recorder._run_after_trading = MagicMock()
        recorder._now = MagicMock(return_value=pd.Timestamp(now_text, tz="Asia/Shanghai"))
        recorder._in_range = lambda current, start, end: SnapshotRecorder._in_range(recorder, current, start, end)
        recorder._past_time = lambda current, boundary: SnapshotRecorder._past_time(current, boundary)
        recorder._within_trading_window = lambda: SnapshotRecorder._within_trading_window(recorder)
        return recorder


if __name__ == "__main__":
    unittest.main()
