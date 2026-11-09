from __future__ import annotations

from datetime import datetime

import pytest

from web.order_execution_report import build_diagnostics
from web.order_execution_report import _automatic_window
from web.order_execution_report import minute_query
from web.order_execution_report import parse_report_window
from web.order_execution_report import query_order_execution
from web.order_execution_report import ricequant_order_book_id
from web.order_execution_report import tick_query


def test_ricequant_order_book_id_is_explicit() -> None:
    assert ricequant_order_book_id("000001.SZ") == "000001.XSHE"
    assert ricequant_order_book_id("600000.sh") == "600000.XSHG"
    with pytest.raises(ValueError, match="unsupported"):
        ricequant_order_book_id("920001.BJ")


def test_report_window_must_stay_on_one_trading_date() -> None:
    start, end = parse_report_window("2026-07-09", None, None)
    assert start == datetime(2026, 7, 9, 9, 15)
    assert end == datetime(2026, 7, 9, 15, 5)
    with pytest.raises(ValueError, match="trade_date"):
        parse_report_window("2026-07-09", "2026-07-08T14:00:00", None)


def test_automatic_window_includes_order_and_later_deal() -> None:
    start, end = _automatic_window(
        "2026-07-09",
        [{"order_time": datetime(2026, 7, 9, 10, 0)}],
        [{"trade_time": datetime(2026, 7, 9, 10, 20)}],
        [],
        [],
    )
    assert start == datetime(2026, 7, 9, 9, 55)
    assert end == datetime(2026, 7, 9, 10, 25)


def test_clickhouse_queries_use_partition_symbol_and_time_filters() -> None:
    start = datetime(2026, 7, 9, 9, 30)
    end = datetime(2026, 7, 9, 9, 35)
    minute_sql = minute_query("000001.XSHE", "2026-07-09", start, end)
    tick_sql = tick_query("000001.XSHE", "2026-07-09", start, end, 500)

    for sql in (minute_sql, tick_sql):
        assert "trading_date = toDate('2026-07-09')" in sql
        assert "order_book_id = '000001.XSHE'" in sql
        assert "datetime >=" in sql
        assert "datetime <=" in sql
    assert "LIMIT 501" in tick_sql


def test_buy_diagnostic_reports_marketable_ticks_without_promising_fill() -> None:
    broker_orders = [
        {
            "client_order_id": "O-1",
            "side": "buy",
            "status": "canceled",
            "limit_price": 10.0,
            "quantity": 100,
            "filled_qty": 0,
            "order_time": datetime(2026, 7, 9, 9, 30),
        }
    ]
    local_orders = [
        {
            **broker_orders[0],
            "accepted_at": datetime(2026, 7, 9, 9, 30, 1),
            "canceled_at": datetime(2026, 7, 9, 9, 30, 4),
        }
    ]
    ticks = [
        {"ts": "2026-07-09 09:30:02", "last": 10.01, "a1": 10.01, "b1": 9.99},
        {"ts": "2026-07-09 09:30:03", "last": 10.00, "a1": 10.00, "b1": 9.99},
    ]

    rows = build_diagnostics(
        broker_orders,
        [],
        local_orders,
        ticks,
        datetime(2026, 7, 9, 9, 29),
        datetime(2026, 7, 9, 9, 31),
        complete_ticks=True,
    )

    assert rows[0]["marketable_tick_count"] == 1
    assert rows[0]["last_crossing_tick_count"] == 1
    assert rows[0]["assessment"] == "marketable_without_fill_review_queue_or_latency"
    assert rows[0]["interval_source"] == "local.accepted_at"


def test_incomplete_tick_window_never_declares_price_unreasonable() -> None:
    rows = build_diagnostics(
        [{
            "client_order_id": "O-1", "side": "sell", "status": "canceled",
            "limit_price": 10.5, "quantity": 100, "filled_qty": 0,
        }],
        [],
        [],
        [],
        datetime(2026, 7, 9, 9, 30),
        datetime(2026, 7, 9, 9, 31),
        complete_ticks=False,
    )
    assert rows[0]["assessment"] == "incomplete_tick_coverage"


def test_report_keeps_broker_and_local_sources_separate() -> None:
    class FakeMySql:
        def query(self, sql, params):
            if "FROM live_order " in sql:
                return [{
                    "trade_date": datetime(2026, 7, 9).date(),
                    "client_order_id": "O-1",
                    "stock_code": "000001.SZ",
                    "side": "buy",
                    "limit_price": 10.0,
                    "quantity": 100,
                    "filled_qty": 0,
                    "status": "canceled",
                    "order_time": datetime(2026, 7, 9, 9, 30),
                }]
            if "FROM live_trade " in sql:
                return []
            if "FROM live_order_realtime" in sql:
                return [{
                    "client_order_id": "O-1", "stock_code": "000001.SZ",
                    "status": "canceled", "limit_price": 10.0,
                    "quantity": 100, "filled_qty": 0,
                    "accepted_at": datetime(2026, 7, 9, 9, 30, 1),
                    "canceled_at": datetime(2026, 7, 9, 9, 30, 4),
                }]
            if "FROM live_trade_realtime" in sql:
                return []
            if "FROM live_order_event" in sql:
                return [{
                    "event_id": "E-1", "event_type": "OrderAccepted",
                    "client_order_id": "O-1", "stock_code": "000001.SZ",
                    "status": "accepted",
                    "local_received_time": datetime(2026, 7, 9, 9, 30, 1),
                }]
            raise AssertionError(sql)

    class FakeClickHouse:
        def query(self, sql):
            if "ricequant_stock_minute" in sql:
                return [{"ts": "2026-07-09 09:30:00", "open": 10.0, "close": 10.0}]
            if "ricequant_stock_tick" in sql:
                return [{"ts": "2026-07-09 09:30:02", "last": 10.0, "a1": 10.0, "b1": 9.99}]
            raise AssertionError(sql)

    payload = query_order_execution(
        FakeMySql(),
        FakeClickHouse(),
        account="ACC",
        trader="TRADER",
        stock_code="000001.SZ",
        trading_date="2026-07-09",
    )

    assert payload["broker"]["orders"][0]["status"] == "canceled"
    assert payload["local"]["events"][0]["event_type"] == "OrderAccepted"
    assert payload["market"]["ticks"][0]["a1"] == 10.0
    assert payload["diagnostics"][0]["marketable_tick_count"] == 1
