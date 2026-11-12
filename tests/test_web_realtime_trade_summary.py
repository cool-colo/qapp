from __future__ import annotations

from datetime import date
from decimal import Decimal

from web.app import realtime_trade_summary


class FakeMySql:
    def __init__(self, asset_rows, trade_rows, slippage_rows=None) -> None:
        self.asset_rows = asset_rows
        self.trade_rows = trade_rows
        self.slippage_rows = slippage_rows or []
        self.calls = []

    def query(self, sql, params):
        self.calls.append((sql, params))
        if "FROM live_asset_snapshot" in sql:
            return self.asset_rows
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
        "trade_date": date.today().isoformat(),
        "total_asset": 1000000.0,
        "buy_amount": 120000.0,
        "buy_rate": 0.12,
        "sell_amount": 80000.0,
        "sell_rate": 0.08,
        "total_amount": 200000.0,
        "total_rate": 0.2,
        "slippage": {
            "total": {"amount": 160.0, "bps": 8.0},
            "buy": {"amount": 240.0, "bps": 20.0},
            "sell": {"amount": -80.0, "bps": -10.0},
            "stocks": [
                {
                    **mysql.slippage_rows[0],
                    "traded_amount": 120000.0,
                    "avg_open_price": 11.976,
                    "avg_trade_price": 12.0,
                    "slippage_amount": 240.0,
                    "slippage_bps": 20.0,
                },
                {
                    **mysql.slippage_rows[1],
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
    assert "snapshot_type = 'before_trading'" in asset_sql
    assert "UPPER(side) = 'BUY'" in trade_sql
    assert "UPPER(side) = 'SELL'" in trade_sql
    assert "e.event_type = 'OrderFilled'" in trade_sql
    assert "e.reconciliation = FALSE" in trade_sql
    assert "e.event_type = 'OrderFilled'" in slippage_sql
    assert "e.reconciliation = FALSE" in slippage_sql
    assert "'TOTAL'" not in slippage_sql
    assert asset_params == trade_params == slippage_params == {
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
    assert result["slippage"] == {
        "total": {"amount": 0.0, "bps": None},
        "buy": {"amount": 0.0, "bps": None},
        "sell": {"amount": 0.0, "bps": None},
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
