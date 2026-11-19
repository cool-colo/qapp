from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from web.app import realtime_trade_summary
from web.order_execution_report import query_order_execution


class FakeMySql:
    def __init__(self, asset_rows, trade_rows, slippage_rows=None, order_rows=None) -> None:
        self.asset_rows = asset_rows
        self.trade_rows = trade_rows
        self.slippage_rows = slippage_rows or []
        self.order_rows = order_rows or []
        self.calls = []

    def query(self, sql, params):
        self.calls.append((sql, params))
        if "FROM live_asset_snapshot" in sql:
            return self.asset_rows
        if "FROM live_order_realtime" in sql:
            return self.order_rows
        if "FROM live_trade_realtime" in sql:
            if "AS slippage_amount" in sql:
                return self.slippage_rows
            return self.trade_rows
        raise AssertionError(sql)


class FakeData:
    def __init__(self, mysql) -> None:
        self._mysql = mysql

    def mysql(self, source):
        assert source == "simulation"
        return self._mysql

    def attach_names(self, rows):
        names = {"000001.SZ": "平安银行", "600000.SH": "浦发银行", "600824.SH": "益民集团"}
        return [{**row, "stock_name": names.get(row["stock_code"], "")} for row in rows]


def test_realtime_trade_summary_uses_before_trading_asset_and_local_fills() -> None:
    mysql = FakeMySql(
        [{"total_asset": Decimal("1000000")}],
        [{"buy_amount": Decimal("120000"), "sell_amount": Decimal("80000")}],
        [
            {
                "stock_code": "000001.SZ",
                "side": "BUY",
                "order_count": 1,
                "trade_count": 1,
                "unpriced_trade_count": 0,
                "auction_open_price": None,
                "auction_max_price": None,
                "traded_qty": 100,
                "traded_amount": Decimal("120000"),
                "avg_open_price": Decimal("11.976"),
                "avg_trade_price": Decimal("12"),
                "slippage_amount": Decimal("240"),
                "slippage_bps": Decimal("20"),
            },
            {
                "stock_code": "600000.SH",
                "side": "SELL",
                "order_count": 1,
                "trade_count": 1,
                "unpriced_trade_count": 0,
                "auction_open_price": None,
                "auction_max_price": None,
                "traded_qty": 100,
                "traded_amount": Decimal("80000"),
                "avg_open_price": Decimal("7.992"),
                "avg_trade_price": Decimal("8"),
                "slippage_amount": Decimal("-80"),
                "slippage_bps": Decimal("-10"),
            },
        ],
    )

    result = realtime_trade_summary(
        source="simulation",
        account="A1",
        trader="T1",
        data=FakeData(mysql),
    )

    assert result == {
        "trade_date": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
        "total_asset": 1000000.0,
        "buy_amount": 120000.0,
        "buy_rate": 0.12,
        "sell_amount": 80000.0,
        "sell_rate": 0.08,
        "total_amount": 200000.0,
        "total_rate": 0.2,
        "orders": [],
        "slippage": {
            "include_auction": True,
            "total": {"amount": 160.0, "bps": 8.0, "unpriced_stock_count": 0},
            "buy": {"amount": 240.0, "bps": 20.0, "unpriced_stock_count": 0},
            "sell": {"amount": -80.0, "bps": -10.0, "unpriced_stock_count": 0},
            "stocks": [
                {
                    **mysql.slippage_rows[0],
                    "stock_name": "平安银行",
                    "traded_amount": 120000.0,
                    "avg_open_price": 11.976,
                    "avg_trade_price": 12.0,
                    "slippage_amount": 240.0,
                    "slippage_bps": 20.0,
                },
                {
                    **mysql.slippage_rows[1],
                    "stock_name": "浦发银行",
                    "traded_amount": 80000.0,
                    "avg_open_price": 7.992,
                    "avg_trade_price": 8.0,
                    "slippage_amount": -80.0,
                    "slippage_bps": -10.0,
                },
            ],
        },
    }
    asset_sql, asset_params = mysql.calls[0]
    trade_sql, trade_params = mysql.calls[1]
    slippage_sql, slippage_params = mysql.calls[2]
    order_sql, order_params = mysql.calls[3]
    assert "snapshot_type = 'before_trading'" in asset_sql
    assert "UPPER(side) = 'BUY'" in trade_sql
    assert "UPPER(side) = 'SELL'" in trade_sql
    assert "e.event_type = 'OrderFilled'" in trade_sql
    assert "e.reconciliation = FALSE" in trade_sql
    assert "e.event_type = 'OrderFilled'" in slippage_sql
    assert "e.reconciliation = FALSE" in slippage_sql
    assert "'TOTAL'" not in slippage_sql
    assert "e.reconciliation = FALSE" in order_sql
    assert asset_params == trade_params == slippage_params == order_params == {
        "account_id": "A1",
        "trader_id": "T1",
        "trade_date": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
    }


def test_realtime_trade_summary_keeps_amounts_when_asset_is_unavailable() -> None:
    mysql = FakeMySql(
        [],
        [{"buy_amount": Decimal("100"), "sell_amount": Decimal("50")}],
    )

    result = realtime_trade_summary(
        source="simulation",
        account="A1",
        trader="T1",
        data=FakeData(mysql),
    )

    assert result["buy_amount"] == 100.0
    assert result["sell_amount"] == 50.0
    assert result["total_amount"] == 150.0
    assert result["total_asset"] is None
    assert result["buy_rate"] is None
    assert result["sell_rate"] is None
    assert result["total_rate"] is None
    assert result["slippage"] == {
        "include_auction": True,
        "total": {"amount": 0.0, "bps": None, "unpriced_stock_count": 0},
        "buy": {"amount": 0.0, "bps": None, "unpriced_stock_count": 0},
        "sell": {"amount": 0.0, "bps": None, "unpriced_stock_count": 0},
        "stocks": [],
    }


def test_realtime_trade_summary_treats_null_asset_as_unavailable() -> None:
    mysql = FakeMySql(
        [{"total_asset": None}],
        [{"buy_amount": None, "sell_amount": None}],
    )

    result = realtime_trade_summary(
        source="simulation",
        account="A1",
        trader="T1",
        data=FakeData(mysql),
    )

    assert result["total_asset"] is None
    assert result["buy_amount"] == 0.0
    assert result["sell_amount"] == 0.0
    assert result["total_amount"] == 0.0
    assert result["total_rate"] is None


class SqliteMySql:
    """Execute the endpoint's SQL against real rows without a database service."""

    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE live_asset_snapshot (
                account_id TEXT, trader_id TEXT, trade_date TEXT,
                snapshot_type TEXT, total_asset REAL
            );
            CREATE TABLE live_trade_realtime (
                account_id TEXT, trader_id TEXT, trade_date TEXT, client_order_id TEXT,
                trade_id TEXT, stock_code TEXT, side TEXT, price REAL, quantity REAL, amount REAL,
                venue_order_id TEXT, commission REAL, trade_time TEXT, write_time TEXT
            );
            CREATE TABLE live_order_realtime (
                account_id TEXT, trader_id TEXT, trade_date TEXT, client_order_id TEXT,
                venue_order_id TEXT, stock_code TEXT, side TEXT, open_price REAL,
                status TEXT, quantity REAL, filled_qty REAL, avg_fill_price REAL,
                limit_price REAL, order_time TEXT, created_at TEXT
            );
            CREATE TABLE live_order_event (
                account_id TEXT, trader_id TEXT, trade_date TEXT, client_order_id TEXT,
                trade_id TEXT, event_type TEXT, reconciliation INTEGER
            );
        """)

    def insert(self, table, **values):
        row = {
            "account_id": "A1", "trader_id": "T1",
            "trade_date": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            **values,
        }
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        self.connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(row.values()),
        )

    def fill(self, order_id, trade_id, stock_code, *, side="SELL", reconciliation=False,
             price=11, quantity=100, trade_time="09:30:00"):
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        self.insert(
            "live_trade_realtime", client_order_id=order_id, trade_id=trade_id,
            stock_code=stock_code, side=side, price=price, quantity=quantity, amount=price * quantity,
            trade_time=f"{day} {trade_time}" if trade_time is not None else None,
        )
        self.event(order_id, trade_id=trade_id, reconciliation=reconciliation)

    def event(self, order_id, *, trade_id=None, event_type="OrderFilled", reconciliation=False):
        self.insert(
            "live_order_event", client_order_id=order_id, trade_id=trade_id,
            event_type=event_type, reconciliation=int(reconciliation),
        )

    def query(self, sql, params):
        sql = re.sub(r"%\((\w+)\)s", r":\1", sql)
        return [dict(row) for row in self.connection.execute(sql, params)]


def test_auction_fills_have_zero_slippage_without_stored_open_or_order_row() -> None:
    mysql = SqliteMySql()
    try:
        mysql.insert("live_order_realtime", client_order_id="priced", stock_code="000707.SZ", open_price=10)
        mysql.insert("live_order_realtime", client_order_id="auction", stock_code="600824.SH", open_price=None)
        mysql.fill("priced", "F1", "000707.SZ")
        mysql.fill("auction", "F2", "600824.SH", trade_time="09:25:00")
        mysql.fill("missing-order", "F3", "001210.SZ", side="BUY", trade_time="09:25:00")
        result = realtime_trade_summary("simulation", "A1", "T1", FakeData(mysql))

        stocks = {row["stock_code"]: row for row in result["slippage"]["stocks"]}
        assert set(stocks) == {"000707.SZ", "600824.SH", "001210.SZ"}
        assert result["sell_amount"] == 2200
        assert result["buy_amount"] == 1100
        assert stocks["600824.SH"]["traded_qty"] == 100
        assert stocks["600824.SH"]["traded_amount"] == 1100
        assert stocks["000707.SZ"]["slippage_amount"] == -100
        for code in ("600824.SH", "001210.SZ"):
            assert stocks[code]["avg_open_price"] == 11
            assert stocks[code]["slippage_amount"] == 0
            assert stocks[code]["slippage_bps"] == 0
            assert stocks[code]["unpriced_trade_count"] == 0
        assert result["slippage"]["total"]["amount"] == -100
        assert result["slippage"]["total"]["bps"] == pytest.approx(-100 / 3300 * 10000)
        assert result["slippage"]["total"]["unpriced_stock_count"] == 0
    finally:
        mysql.connection.close()


def test_order_execution_filters_reconciliation_only_fills_without_losing_real_replays() -> None:
    mysql = SqliteMySql()
    try:
        mysql.fill("actual", "F1", "600824.SH")
        mysql.fill("synthetic", "F2", "600824.SH", reconciliation=True)
        mysql.fill("replayed", "F3", "600824.SH")
        mysql.event("replayed", trade_id="F3", reconciliation=True)
        mysql.insert(
            "live_trade_realtime", client_order_id="other-account", trade_id="F4",
            stock_code="600824.SH", side="SELL", price=11, quantity=100, amount=1100,
        )
        mysql.insert(
            "live_order_event", account_id="A2", client_order_id="other-account", trade_id="F4",
            event_type="OrderFilled", reconciliation=0,
        )

        class LocalTradeSource:
            def query(self, sql, params):
                if "FROM live_trade_realtime" in sql:
                    return mysql.query(sql, params)
                return []

        class EmptyMarketData:
            def query(self, sql):
                return []

        trading_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        result = query_order_execution(
            LocalTradeSource(), EmptyMarketData(), account="A1", trader="T1",
            stock_code="600824.SH", trading_date=trading_date,
            start=f"{trading_date}T09:30:00", end=f"{trading_date}T09:31:00",
        )
        assert [row["trade_id"] for row in result["local"]["trades"]] == ["F1", "F3"]
    finally:
        mysql.connection.close()


def test_missing_continuous_open_price_does_not_report_partial_slippage() -> None:
    mysql = SqliteMySql()
    try:
        for order_id, open_price in (("priced", 10), ("auction", None), ("zero", 0), ("negative", -1)):
            mysql.insert(
                "live_order_realtime", client_order_id=order_id,
                stock_code="600824.SH", open_price=open_price,
            )
            mysql.fill(order_id, f"F-{order_id}", "600824.SH")
        result = realtime_trade_summary("simulation", "A1", "T1", FakeData(mysql))

        stock = result["slippage"]["stocks"][0]
        assert stock["trade_count"] == 4
        assert stock["traded_qty"] == 400
        assert stock["traded_amount"] == 4400
        assert stock["avg_trade_price"] == 11
        assert stock["unpriced_trade_count"] == 3
        assert stock["avg_open_price"] is None
        assert stock["slippage_amount"] is None
        assert result["slippage"]["sell"]["amount"] is None
    finally:
        mysql.connection.close()


def test_auction_option_changes_slippage_weight_but_keeps_auction_open_for_later_fills() -> None:
    mysql = SqliteMySql()
    try:
        mysql.fill("auction", "A1", "600824.SH", side="BUY", price=10, trade_time="09:25:00")
        mysql.fill("later-buy", "B1", "600824.SH", side="BUY", price=11, trade_time="10:00:00")
        mysql.fill("later-sell", "S1", "600824.SH", price=9, trade_time="10:01:00")
        mysql.fill(
            "synthetic", "R1", "600824.SH", price=19,
            trade_time="09:25:00", reconciliation=True,
        )
        included = realtime_trade_summary("simulation", "A1", "T1", FakeData(mysql))
        excluded = realtime_trade_summary(
            "simulation", "A1", "T1", FakeData(mysql), include_auction=False,
        )

        assert included["slippage"]["include_auction"] is True
        assert excluded["slippage"]["include_auction"] is False
        for result in (included, excluded):
            assert result["total_amount"] == 3000
            assert result["slippage"]["total"]["amount"] == 200
            assert result["slippage"]["total"]["unpriced_stock_count"] == 0
            assert all(row["avg_open_price"] == 10 for row in result["slippage"]["stocks"])
        assert included["slippage"]["total"]["bps"] == pytest.approx(200 / 3000 * 10000)
        assert excluded["slippage"]["total"]["bps"] == 1000
        included_buy = next(row for row in included["slippage"]["stocks"] if row["side"] == "BUY")
        excluded_buy = next(row for row in excluded["slippage"]["stocks"] if row["side"] == "BUY")
        assert included_buy["traded_qty"] == 200
        assert included_buy["trade_count"] == 2
        assert excluded_buy["traded_qty"] == 100
        assert excluded_buy["trade_count"] == 1
    finally:
        mysql.connection.close()


def test_unchecking_auction_option_omits_auction_only_stocks() -> None:
    mysql = SqliteMySql()
    try:
        mysql.fill("auction", "F1", "600824.SH", trade_time="09:25:00")
        result = realtime_trade_summary(
            "simulation", "A1", "T1", FakeData(mysql), include_auction=False,
        )
        assert result["sell_amount"] == 1100
        assert result["slippage"]["stocks"] == []
        assert result["slippage"]["total"] == {
            "amount": 0.0, "bps": None, "unpriced_stock_count": 0,
        }
    finally:
        mysql.connection.close()


def test_conflicting_actual_auction_prices_fail_instead_of_inventing_an_open() -> None:
    mysql = SqliteMySql()
    try:
        mysql.fill("auction-1", "F1", "600824.SH", price=10, trade_time="09:25:00")
        mysql.fill("auction-2", "F2", "600824.SH", price=11, trade_time="09:25:00")
        with pytest.raises(ValueError, match="conflicting opening-auction prices for 600824.SH"):
            realtime_trade_summary("simulation", "A1", "T1", FakeData(mysql))
    finally:
        mysql.connection.close()


def test_order_table_keeps_filled_and_partial_cancels_excludes_synthetic_orders() -> None:
    mysql = SqliteMySql()
    try:
        for order_id, status, quantity, filled_qty in (
            ("filled", "filled", 100, 100),
            ("partial", "canceled", 4200, 2700),
            ("canceled", "canceled", 100, 0),
            ("synthetic", "filled", 4200, 4200),
        ):
            mysql.insert(
                "live_order_realtime", client_order_id=order_id, stock_code="605108.SH",
                side="sell", open_price=10, status=status, quantity=quantity, filled_qty=filled_qty,
            )
            mysql.event(order_id, event_type="OrderAccepted", reconciliation=order_id == "synthetic")
        mysql.fill("filled", "F1", "605108.SH")
        # Repeated audit events and reconciliation must not duplicate actual fills.
        mysql.event("filled", trade_id="F1")
        mysql.event("filled", trade_id="F1", reconciliation=True)
        mysql.fill("synthetic", "F2", "605108.SH", reconciliation=True)
        mysql.insert(
            "live_trade_realtime", client_order_id="stale", trade_id="F3",
            stock_code="603801.SH", side="sell", price=11, quantity=100, amount=1100,
        )
        result = realtime_trade_summary("simulation", "A1", "T1", FakeData(mysql))

        orders = {row["client_order_id"]: row for row in result["orders"]}
        assert set(orders) == {"filled", "partial", "canceled"}
        assert orders["filled"]["status"] == "filled"
        assert orders["partial"]["status"] == "canceled"
        assert orders["partial"]["filled_qty"] == 2700
        assert orders["partial"]["quantity"] == 4200
        assert orders["canceled"]["filled_qty"] == 0
        assert result["sell_amount"] == 1100
        assert result["slippage"]["stocks"][0]["trade_count"] == 1
        assert len(result["slippage"]["stocks"]) == 1
    finally:
        mysql.connection.close()
