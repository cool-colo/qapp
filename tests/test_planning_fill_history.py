from __future__ import annotations

from datetime import date
from decimal import Decimal
import sqlite3
import unittest
from unittest.mock import MagicMock

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import LiquiditySide, OrderSide
from nautilus_trader.model.events import OrderAccepted, OrderFilled
from nautilus_trader.model.identifiers import AccountId, InstrumentId, TradeId, VenueOrderId
from nautilus_trader.model.objects import Money

from backtests.result_writers.live_writer import LiveSnapshotWriter
from lives.snapshot_recorder import SnapshotRecorder, SnapshotRecorderConfig
from tests.test_opening_auction import replay_fixture, timestamp


DAY = date(2026, 7, 8)
A, B = "000001.SZ.QMT", "000002.SZ.QMT"


def trade(tid, *, iid=A, side="buy", quantity=100):
    return {"trade_id": tid, "instrument_id": iid, "side": side, "quantity": quantity}


class PlanningFillHistoryTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.strategy, _, _ = replay_fixture(auction_enabled=False)
        self.strategy.clock.set_time(timestamp("2026-07-08 14:30:00"))
        self.strategy._startup_ts_ns = timestamp("2026-07-08 14:29:00")
        self.strategy._schedule_convergence = MagicMock()

    def tearDown(self):
        self.engine.dispose()

    def fill(self, tid, *, iid=A, side=OrderSide.BUY, quantity=100,
             reconciliation=False, tags=None, ts_event=None, order=None):
        instrument = self.engine.cache.instrument(InstrumentId.from_str(iid))
        account = AccountId("QMT-001")
        venue_order_id = VenueOrderId("venue-" + tid) if order is None else order.venue_order_id
        if order is None:
            order = self.strategy.order_factory.limit(
                instrument_id=instrument.id, order_side=side,
                quantity=instrument.make_qty(1000), price=instrument.make_price(10), tags=tags,
            )
            self.engine.cache.add_order(order)
            order.apply(OrderAccepted(
                trader_id=order.trader_id, strategy_id=order.strategy_id,
                instrument_id=instrument.id, client_order_id=order.client_order_id,
                venue_order_id=venue_order_id, account_id=account, event_id=UUID4(),
                ts_event=self.strategy.clock.timestamp_ns(), ts_init=self.strategy.clock.timestamp_ns(),
            ))
        event = OrderFilled(
            trader_id=order.trader_id, strategy_id=order.strategy_id,
            instrument_id=instrument.id, client_order_id=order.client_order_id,
            venue_order_id=venue_order_id, account_id=account, trade_id=TradeId(tid),
            position_id=None, order_side=side, order_type=order.order_type,
            last_qty=instrument.make_qty(quantity), last_px=instrument.make_price(10),
            currency=instrument.quote_currency, commission=Money(0, instrument.quote_currency),
            liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE, event_id=UUID4(),
            ts_event=self.strategy.clock.timestamp_ns() if ts_event is None else ts_event,
            ts_init=self.strategy.clock.timestamp_ns(), reconciliation=reconciliation,
        )
        order.apply(event)
        self.engine.cache.update_order(order)
        return order, event

    def context(self):
        return self.strategy.build_planning_context(DAY, snapshot={
            A: {"last_price": 10, "open": 10, "last_close": 10},
            B: {"last_price": 20, "open": 20, "last_close": 20},
        })

    def test_claimed_position_alignment_without_tag_never_locks_holdings(self):
        for iid in (A, B):
            _, event = self.fill("alignment-" + iid, iid=iid, quantity=1000, reconciliation=True)
            self.strategy.on_order_filled(event)
            self.assertEqual(self.strategy.planning_today_fills(iid, DAY), (0, 0))
            self.assertEqual(self.strategy._today_fill_snapshot(InstrumentId.from_str(iid), DAY).fill_count, 0)
            self.assertEqual(self.strategy._planning_permissions(iid, DAY, planning_context=self.context()),
                             (True, True))

    def test_tagged_alignment_is_filtered_even_without_event_flag(self):
        _, event = self.fill("tagged", tags=["RECONCILIATION"])
        self.strategy.on_order_filled(event)
        self.assertEqual(self.strategy.planning_today_fills(A, DAY), (0, 0))

    def test_real_partial_fill_on_recovered_order_is_still_counted(self):
        order, replay = self.fill("historical-replay", quantity=100, reconciliation=True)
        self.strategy.on_order_filled(replay)
        _, live = self.fill("live-after-restart", quantity=200, order=order)
        self.strategy.on_order_filled(live)
        self.assertEqual(self.strategy.planning_today_fills(A, DAY), (200, 0))

    def test_mysql_history_survives_empty_cache_and_locks_both_directions(self):
        self.strategy.restore_today_fills(DAY, [trade("historical-buy"), trade("historical-sell", side="sell")])
        self.assertEqual(self.engine.cache.orders(), [])
        self.assertEqual(self.strategy.planning_today_fills(A, DAY), (100, 100))
        self.assertEqual(self.strategy._planning_permissions(A, DAY, planning_context=self.context()),
                         (False, False))

    def test_disjoint_history_and_memory_are_added_and_shared_ids_count_once(self):
        self.strategy.restore_today_fills(DAY, [trade("db-only", quantity=300), trade("shared", quantity=100)])
        _, shared = self.fill("shared")
        _, memory = self.fill("memory-only", quantity=200)
        self.strategy.on_order_filled(shared)
        self.strategy.on_order_filled(shared)
        self.strategy.on_order_filled(memory)
        self.assertEqual(self.strategy.planning_today_fills(A, DAY), (600, 0))

    def test_mysql_real_history_counts_once_alongside_replayed_and_synthetic_fills(self):
        self.strategy.restore_today_fills(DAY, [trade("real-history", quantity=400)])
        _, replay = self.fill("real-history", quantity=400, reconciliation=True)
        _, alignment = self.fill("position-alignment", quantity=1000, reconciliation=True)
        self.strategy.on_order_filled(replay)
        self.strategy.on_order_filled(alignment)
        self.assertEqual(self.strategy.planning_today_fills(A, DAY), (400, 0))

    def test_planning_refreshes_mysql_and_does_not_double_count_newly_persisted_fill(self):
        loader = MagicMock(return_value=[trade("db-only", quantity=300)])
        self.strategy.configure_today_fills_loader(loader)
        request, _ = self.strategy._target_planning_request(DAY, date(2026, 7, 7), planning_context=self.context())
        self.assertTrue(next(c for c in request.candidates if c.instrument_id == A).can_buy)
        _, event = self.fill("new-sell", side=OrderSide.SELL, quantity=200)
        self.strategy.on_order_filled(event)
        loader.return_value = [trade("db-only", quantity=300), trade("new-sell", side="sell", quantity=200)]
        request, _ = self.strategy._target_planning_request(DAY, date(2026, 7, 7), planning_context=self.context())
        self.assertFalse(next(c for c in request.candidates if c.instrument_id == A).can_buy)
        self.assertEqual(self.strategy.planning_today_fills(A, DAY), (300, 200))
        self.assertEqual(loader.call_args_list[0].args, (DAY,))
        self.assertEqual(loader.call_count, 2)

    def test_late_actual_callback_uses_trade_id_even_if_time_precedes_startup(self):
        _, event = self.fill("late-real", ts_event=timestamp("2026-07-08 14:28:00"))
        self.strategy.on_order_filled(event)
        self.assertEqual(self.strategy.planning_today_fills(A, DAY), (100, 0))

    def test_previous_day_is_not_counted(self):
        _, event = self.fill("previous-day", ts_event=timestamp("2026-07-07 14:30:00"))
        self.strategy.on_order_filled(event)
        self.strategy.restore_today_fills(date(2026, 7, 7), [trade("previous-db")])
        self.assertEqual(self.strategy.planning_today_fills(A, DAY), (0, 0))

    def test_reconciliation_remains_audit_only_and_actual_fill_is_persisted(self):
        worker = MagicMock()
        recorder = SnapshotRecorder(
            SnapshotRecorderConfig(account_id="a", trader_id="t"), MagicMock(), self.strategy,
            order_event_writer=worker, order_decision_writer=MagicMock(),
        )
        self.engine.add_actor(recorder)
        for tid, reconciliation, tags in [("flagged", True, None), ("tagged", False, ["RECONCILIATION"]),
                                          ("actual", False, None)]:
            with self.subTest(tid=tid):
                _, event = self.fill(tid, reconciliation=reconciliation, tags=tags)
                recorder._on_order_event(event)
                write = worker.submit.call_args.args[0]
                self.assertEqual(write.event_record.trade_id, tid)
                self.assertEqual(write.event_record.reconciliation, tid != "actual")
                self.assertEqual(write.trade_record is None, tid != "actual")

    def test_new_broker_backfill_is_attributed_before_the_next_restart(self):
        recorder = SnapshotRecorder(
            SnapshotRecorderConfig(account_id="a", trader_id="t"), MagicMock(), self.strategy,
            order_event_writer=MagicMock(), order_decision_writer=MagicMock(),
        )
        self.engine.add_actor(recorder)
        record = recorder._trade_record_from_broker(DAY, {
            "traded_id": "broker-trade", "instrument_id": A,
            "order_type": 23, "traded_volume": 200, "traded_price": 10,
        })
        self.assertEqual(record.strategy_id, str(self.strategy.id))
        self.assertEqual(record.quantity, 200)
        self.assertEqual(record.side, "buy")


class PersistedFillHistoryTests(unittest.TestCase):
    def test_loader_filters_synthetic_audit_rows_preserves_real_replays_and_scopes_history(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        for table in ("live_trade", "live_trade_realtime"):
            connection.execute(f"CREATE TABLE {table} (account_id TEXT, trader_id TEXT, trade_date TEXT, "
                               "strategy_id TEXT, trade_id TEXT, instrument_id TEXT, side TEXT, quantity INTEGER)")
        connection.execute("CREATE TABLE live_order_event (account_id TEXT, trader_id TEXT, trade_date TEXT, "
                           "trade_id TEXT, instrument_id TEXT, event_type TEXT, reconciliation INTEGER)")

        def persist(table, tid, *, account="a", strategy="s", day=DAY, side="buy", quantity=100):
            connection.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?)",
                               (account, "t", day.isoformat(), strategy, tid, A, side, quantity))

        def audit(tid, reconciliation):
            connection.execute("INSERT INTO live_order_event VALUES (?,?,?,?,?,?,?)",
                               ("a", "t", DAY.isoformat(), tid, A, "OrderFilled", reconciliation))

        persist("live_trade_realtime", "synthetic", quantity=1000)
        audit("synthetic", True)
        persist("live_trade_realtime", "actual-then-replayed")
        audit("actual-then-replayed", False)
        audit("actual-then-replayed", True)
        persist("live_trade", "broker-history", side="sell", quantity=200)
        persist("live_trade_realtime", "broker-history", side="sell", quantity=200)
        audit("broker-history", True)
        persist("live_trade", "other-account", account="other")
        persist("live_trade_realtime", "other-strategy", strategy="other")
        persist("live_trade", "previous-day", day=date(2026, 7, 7))
        writer = LiveSnapshotWriter.for_testing(connection)
        writer._query = lambda sql, args: connection.execute(
            sql.replace("%s", "?"), tuple(arg.isoformat() if isinstance(arg, date) else arg for arg in args),
        ).fetchall()
        rows = writer.load_strategy_today_fills("a", "t", DAY, "s")
        self.assertEqual({row["trade_id"] for row in rows}, {"actual-then-replayed", "broker-history"})
        self.assertEqual(next(row for row in rows if row["trade_id"] == "broker-history")["quantity"], 200)
