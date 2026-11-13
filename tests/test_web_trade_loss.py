from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from web.app import realtime_trade_loss
from web.trade_loss_report import (
    build_minute_trade_loss,
    build_preopen_trade_book,
    minute_loss_query,
    query_minute_trade_loss,
)


TARGETS = [
    {"stock_code": "000001.SZ", "target_qty": 100, "current_qty": 100, "target_version": "v1"},
    {"stock_code": "600000.SH", "target_qty": 100, "current_qty": 100, "target_version": "v1"},
    {"stock_code": "300001.SZ", "target_qty": 100, "current_qty": 100, "target_version": "v1"},
]
POSITIONS = [
    {"stock_code": "600000.SH", "volume": 200},
    {"stock_code": "300001.SZ", "volume": 100},
    # Stop-loss exit intentionally absent from the target portfolio.
    {"stock_code": "300002.SZ", "volume": 50},
]
MINUTES = [
    {"order_book_id": symbol, "ts": f"2026-11-13 {minute}", "open": opening, "close": close}
    for minute, prices in (
        ("09:31:00.000", [("000001.XSHE", 10, 11), ("600000.XSHG", 20, 18), ("300002.XSHE", 30, 27)]),
        ("09:32:00.000", [("000001.XSHE", 11, 9), ("600000.XSHG", 18, 22), ("300002.XSHE", 27, 33)]),
    )
    for symbol, opening, close in prices
]


class FakeMySql:
    def __init__(self, targets=None, positions=None, assets=None):
        self.targets = TARGETS if targets is None else targets
        self.positions = POSITIONS if positions is None else positions
        self.assets = [] if assets is None else assets
        self.calls = []

    def query(self, sql, params):
        self.calls.append((sql, params))
        if "FROM live_target_portfolio" in sql:
            return self.targets
        if "FROM live_position_snapshot" in sql:
            return self.positions
        if "FROM live_asset_snapshot" in sql:
            return self.assets
        raise AssertionError(sql)


class FakeClickHouse:
    def __init__(self, minutes=None):
        self.minutes = MINUTES if minutes is None else minutes
        self.calls = []

    def query(self, sql):
        self.calls.append(sql)
        return self.minutes


def query_report(mysql, clickhouse):
    return query_minute_trade_loss(mysql, clickhouse, account="A1", trader="T1", trading_date="2026-11-13")


def test_fixed_book_includes_stop_loss_exits_and_ignores_completed_live_quantities():
    book = build_preopen_trade_book(TARGETS, POSITIONS)

    assert book == [
        {"stock_code": "000001.SZ", "side": "BUY", "preopen_qty": 0, "target_qty": 100, "trade_qty": 100},
        {"stock_code": "300002.SZ", "side": "SELL", "preopen_qty": 50, "target_qty": 0, "trade_qty": 50},
        {"stock_code": "600000.SH", "side": "SELL", "preopen_qty": 200, "target_qty": 100, "trade_qty": 100},
    ]
    # All live current_qty values already equal target_qty, but the preopen
    # rebalance remains unchanged after execution has completed.
    changed = [{**row, "current_qty": 9999} for row in TARGETS]
    assert build_preopen_trade_book(changed, POSITIONS) == book


def test_minute_close_loss_uses_fixed_open_gross_notional_and_side_sign():
    book = build_preopen_trade_book(TARGETS, POSITIONS)
    result = build_minute_trade_loss(book, list(reversed(MINUTES)))

    assert result["open_amount"] == 4500.0
    assert result["buy_count"] == 1
    assert result["sell_count"] == 2
    assert result["missing_open_stock_codes"] == []
    first, second = result["points"]
    assert first["buy_loss_amount"] == 100.0
    assert first["sell_loss_amount"] == 350.0
    assert first["loss_amount"] == 450.0
    assert first["loss_bps"] == 1000.0
    assert second["loss_amount"] == -450.0
    assert second["loss_bps"] == -1000.0


def test_missing_minute_never_drops_a_stock_or_changes_denominator():
    minutes = [row for row in MINUTES if not (row["order_book_id"] == "300002.XSHE" and "09:32" in row["ts"])]
    result = build_minute_trade_loss(build_preopen_trade_book(TARGETS, POSITIONS), minutes)

    assert result["stock_count"] == 3
    assert result["open_amount"] == 4500.0
    assert result["points"][0]["loss_bps"] == 1000.0
    assert result["points"][1]["priced_count"] == 2
    assert result["points"][1]["loss_bps"] is None
    assert result["points"][1]["loss_amount"] is None


def test_late_first_bar_open_is_not_used_as_daily_open():
    minutes = [row for row in MINUTES if not (row["order_book_id"] == "300002.XSHE" and "09:31" in row["ts"])]
    result = build_minute_trade_loss(build_preopen_trade_book(TARGETS, POSITIONS), minutes)

    assert result["missing_open_stock_codes"] == ["300002.SZ"]
    assert result["open_amount"] is None
    assert all(point["loss_bps"] is None for point in result["points"])


def test_whole_missing_minute_is_a_gap_and_lunch_is_not_a_trading_minute():
    minutes = [
        {**row, "ts": "2026-11-13 13:01:00"} if "09:32" in row["ts"] else row
        for row in MINUTES
    ]
    result = build_minute_trade_loss(build_preopen_trade_book(TARGETS, POSITIONS), minutes)
    points = {point["ts"][11:16]: point for point in result["points"]}
    assert points["09:32"]["loss_bps"] is None
    assert points["09:32"]["priced_count"] == 0
    assert "12:00" not in points
    assert points["13:01"]["loss_bps"] == -1000.0


@pytest.mark.parametrize("close", [None, 0, -1, float("nan"), float("inf")])
def test_invalid_close_produces_a_gap(close):
    minutes = [{**row, "close": close} if row["order_book_id"] == "300002.XSHE" else row for row in MINUTES]
    result = build_minute_trade_loss(build_preopen_trade_book(TARGETS, POSITIONS), minutes)
    assert result["open_amount"] == 4500.0
    assert all(point["loss_bps"] is None for point in result["points"])


def test_query_is_scoped_to_preopen_account_snapshots_and_fixed_symbols():
    mysql, clickhouse = FakeMySql(), FakeClickHouse()
    result = query_report(mysql, clickhouse)

    assert result["status"] == "ok"
    assert result["target_versions"] == ["v1"]
    assert result["points"][0]["loss_bps"] == 1000.0
    assert len(mysql.calls) == 2
    for sql, params in mysql.calls:
        assert "snapshot_type = 'before_trading'" in sql
        assert "status = 'ok'" in sql
        assert params == {"account_id": "A1", "trader_id": "T1", "trade_date": "2026-11-13"}
    assert len(clickhouse.calls) == 1
    sql = clickhouse.calls[0]
    assert "FROM ricequant_stock_minute_realtime" in sql
    assert "trade_date = toDate('2026-11-13')" in sql
    assert "argMax(tuple(open, close), tuple(received_at, event_id))" in sql
    assert "'300002.XSHE'" in sql
    assert "'300001.XSHE'" not in sql
    assert "received_at <= now64" in sql
    assert "datetime <= now64" in sql


def test_minute_query_supports_beijing_symbol_and_rejects_invalid_input():
    assert "'920001.BJSE'" in minute_loss_query(["920001.BJ"], "2026-11-13")
    with pytest.raises(ValueError):
        minute_loss_query(["unsafe'"], "2026-11-13")
    with pytest.raises(ValueError):
        minute_loss_query(["000001.SZ"], "invalid")


def test_missing_target_snapshot_is_not_interpreted_as_full_liquidation():
    mysql, clickhouse = FakeMySql(targets=[]), FakeClickHouse()
    result = query_report(mysql, clickhouse)
    assert result["status"] == "missing_preopen_target"
    assert result["points"] == []
    assert clickhouse.calls == []


def test_missing_position_snapshot_is_not_interpreted_as_zero_holdings():
    mysql, clickhouse = FakeMySql(positions=[], assets=[{"market_value": Decimal("10000")}]), FakeClickHouse()
    result = query_report(mysql, clickhouse)
    assert result["status"] == "missing_preopen_positions"
    assert clickhouse.calls == []


def test_verified_flat_account_can_estimate_preopen_buys():
    mysql = FakeMySql(targets=TARGETS[:1], positions=[], assets=[{"market_value": Decimal("0")}])
    result = query_report(mysql, FakeClickHouse(minutes=[row for row in MINUTES if row["order_book_id"] == "000001.XSHE"]))
    assert result["status"] == "ok"
    assert result["stock_count"] == 1
    assert result["points"][0]["loss_bps"] == 1000.0


def test_unchanged_preopen_book_never_queries_market_data():
    mysql, clickhouse = FakeMySql(targets=TARGETS[2:], positions=POSITIONS[1:2]), FakeClickHouse()
    result = query_report(mysql, clickhouse)
    assert result["status"] == "no_trades"
    assert clickhouse.calls == []


def test_missing_market_data_keeps_fixed_book_visible():
    result = query_report(FakeMySql(), FakeClickHouse(minutes=[]))
    assert result["stock_count"] == 3
    assert result["missing_minute_stock_codes"] == ["000001.SZ", "300002.SZ", "600000.SH"]
    assert result["open_amount"] is None
    assert result["points"] == []


@pytest.mark.parametrize("quantity", [None, -1, 1.5, "NaN"])
def test_invalid_snapshot_quantity_fails_instead_of_changing_the_book(quantity):
    with pytest.raises(ValueError, match="invalid"):
        build_preopen_trade_book([{"stock_code": "000001.SZ", "target_qty": quantity}], [])


def test_duplicate_stock_snapshot_fails_fast():
    with pytest.raises(ValueError, match="duplicate"):
        build_preopen_trade_book(TARGETS + TARGETS[:1], POSITIONS)


def test_endpoint_uses_shanghai_day_and_source_without_a_node_api():
    class FakeData:
        def __init__(self):
            self.clickhouse = FakeClickHouse()
            self.reader = FakeMySql()

        def mysql(self, source):
            assert source == "simulation"
            return self.reader

        def attach_names(self, rows):
            return [{**row, "stock_name": "测试股票"} for row in rows]

    data = FakeData()
    with patch("web.app.datetime") as clock:
        clock.now.return_value = datetime(2026, 11, 13, 0, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        result = realtime_trade_loss("simulation", "A1", "T1", data)
        clock.now.assert_called_once_with(ZoneInfo("Asia/Shanghai"))
    assert result["trade_date"] == "2026-11-13"
    assert result["stock_count"] == 3
    assert result["trades"][0]["stock_name"] == "测试股票"


def test_stock_weights_include_both_sides_and_contributions_sum_to_total():
    result = build_minute_trade_loss(build_preopen_trade_book(TARGETS, POSITIONS), MINUTES)
    stocks = {stock["stock_code"]: stock for stock in result["stock_series"]}
    assert sum(stock["weight"] for stock in stocks.values()) == pytest.approx(1)
    assert stocks["000001.SZ"]["weight"] == pytest.approx(1000 / 4500)
    assert stocks["300002.SZ"]["weight"] == pytest.approx(1500 / 4500)
    assert stocks["600000.SH"]["weight"] == pytest.approx(2000 / 4500)
    assert stocks["000001.SZ"]["points"][0]["price_change_bps"] == 1000
    assert stocks["600000.SH"]["points"][0]["price_change_bps"] == -1000
    assert stocks["600000.SH"]["points"][0]["weighted_loss_bps"] == pytest.approx(2000 / 4500 * 1000)
    for index, total in enumerate(result["points"]):
        assert sum(stock["points"][index]["weighted_loss_bps"] for stock in stocks.values()) == pytest.approx(total["loss_bps"])


def test_missing_stock_minute_preserves_other_weights_and_contributions():
    minutes = [row for row in MINUTES if not (row["order_book_id"] == "300002.XSHE" and "09:32" in row["ts"])]
    result = build_minute_trade_loss(build_preopen_trade_book(TARGETS, POSITIONS), minutes)
    stocks = {stock["stock_code"]: stock for stock in result["stock_series"]}
    assert result["points"][1]["loss_bps"] is None
    assert stocks["300002.SZ"]["points"][1]["weighted_loss_bps"] is None
    assert stocks["000001.SZ"]["points"][1]["weighted_loss_bps"] == pytest.approx(-1000 * 1000 / 4500)
    assert stocks["000001.SZ"]["weight"] == pytest.approx(1000 / 4500)


def test_missing_open_keeps_individual_raw_bp_but_never_reweights_remaining_stocks():
    minutes = [row for row in MINUTES if row["order_book_id"] != "300002.XSHE"]
    result = build_minute_trade_loss(build_preopen_trade_book(TARGETS, POSITIONS), minutes)
    assert all(stock["weight"] is None for stock in result["stock_series"])
    buy = result["stock_series"][0]
    assert buy["points"][0]["price_change_bps"] == 1000
    assert buy["points"][0]["weighted_loss_bps"] is None
