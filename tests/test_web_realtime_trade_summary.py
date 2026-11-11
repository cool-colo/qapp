from __future__ import annotations

from datetime import date
from decimal import Decimal

from web.app import realtime_trade_summary


class FakeMySql:
    def __init__(self, asset_rows, trade_rows) -> None:
        self.asset_rows = asset_rows
        self.trade_rows = trade_rows
        self.calls = []

    def query(self, sql, params):
        self.calls.append((sql, params))
        if "FROM live_asset_snapshot" in sql:
            return self.asset_rows
        if "FROM live_trade_realtime" in sql:
            return self.trade_rows
        raise AssertionError(sql)


class FakeData:
    def __init__(self, mysql) -> None:
        self._mysql = mysql

    def mysql(self, source):
        assert source == "simulation"
        return self._mysql


def test_realtime_trade_summary_uses_before_trading_asset_and_local_fills() -> None:
    mysql = FakeMySql(
        [{"total_asset": Decimal("1000000")}],
        [{"buy_amount": Decimal("120000"), "sell_amount": Decimal("80000")}],
    )

    result = realtime_trade_summary(
        source="simulation",
        account="A1",
        trader="T1",
        data=FakeData(mysql),
    )

    assert result == {
        "trade_date": date.today().isoformat(),
        "total_asset": 1000000.0,
        "buy_amount": 120000.0,
        "buy_rate": 0.12,
        "sell_amount": 80000.0,
        "sell_rate": 0.08,
        "total_amount": 200000.0,
        "total_rate": 0.2,
    }
    asset_sql, asset_params = mysql.calls[0]
    trade_sql, trade_params = mysql.calls[1]
    assert "snapshot_type = 'before_trading'" in asset_sql
    assert "UPPER(side) = 'BUY'" in trade_sql
    assert "UPPER(side) = 'SELL'" in trade_sql
    assert asset_params == trade_params == {
        "account_id": "A1",
        "trader_id": "T1",
        "trade_date": date.today().isoformat(),
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
