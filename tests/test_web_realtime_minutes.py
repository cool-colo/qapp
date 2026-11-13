from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from web.app import realtime_minutes
from web.realtime_minute_report import query_realtime_minutes, realtime_minute_query


class FakeClickHouse:
    def __init__(self, rows=None):
        self.rows = [] if rows is None else rows
        self.calls = []

    def query(self, sql):
        self.calls.append(sql)
        return self.rows


def minute(ts="09:31:00.000", **overrides):
    return {"ts": f"2026-11-13 {ts}", "open": 10, "high": 12, "low": 9,
            "close": 11, "volume": 0, "total_turnover": 0, **overrides}


def test_query_uses_latest_whole_realtime_bar_and_partition_symbol_filters():
    sql = realtime_minute_query("600000.SH", "2026-11-13")
    assert "FROM ricequant_stock_minute_realtime" in sql
    assert "trade_date = toDate('2026-11-13')" in sql
    assert "order_book_id = '600000.XSHG'" in sql
    assert "argMax(tuple(open, high, low, close, volume, total_turnover)" in sql
    assert "tuple(received_at, event_id)" in sql
    assert "datetime <= now64" in sql
    assert "received_at <= now64" in sql


def test_close_change_is_anchored_to_first_open_and_zero_volume_is_preserved():
    result = query_realtime_minutes(FakeClickHouse([minute("09:32:00", open=11, close=9), minute()]), "000001.SZ", "2026-11-13")
    assert result["open_price"] == 10
    assert result["bars"][0]["price_change_bps"] == pytest.approx(1000)
    assert result["bars"][1]["price_change_bps"] == pytest.approx(-1000)
    assert result["bars"][0]["volume"] == 0
    assert result["bars"][0]["total_turnover"] == 0


@pytest.mark.parametrize("close", [None, 0, -1, float("nan"), float("inf")])
def test_invalid_close_remains_missing(close):
    result = query_realtime_minutes(FakeClickHouse([minute(close=close)]), "000001.SZ", "2026-11-13")
    assert result["bars"][0]["close"] is None
    assert result["bars"][0]["price_change_bps"] is None


def test_late_first_bar_is_never_substituted_for_opening_price():
    result = query_realtime_minutes(FakeClickHouse([minute("09:32:00")]), "000001.SZ", "2026-11-13")
    assert result["open_price"] is None
    assert result["bars"][0]["price_change_bps"] is None


def test_empty_day_remains_empty_and_invalid_symbols_and_dates_fail():
    assert query_realtime_minutes(FakeClickHouse(), "920001.BJ", "2026-11-13")["bars"] == []
    with pytest.raises(ValueError):
        realtime_minute_query("invalid'", "2026-11-13")
    with pytest.raises(ValueError):
        realtime_minute_query("000001.SZ", "invalid")


def test_endpoint_defaults_to_shanghai_day_without_account_or_node_api():
    class FakeData:
        def __init__(self):
            self.clickhouse = FakeClickHouse([minute()])

        def resolve_names(self, codes):
            assert codes == ["000001.SZ"]
            return {"000001.SZ": "平安银行"}

    with patch("web.app.datetime") as clock:
        clock.now.return_value = datetime(2026, 11, 13, tzinfo=ZoneInfo("Asia/Shanghai"))
        result = realtime_minutes("000001.sz", data=FakeData())
    assert result["trade_date"] == "2026-11-13"
    assert result["stock_name"] == "平安银行"


def test_endpoint_rejects_bad_date_with_422():
    class FakeData:
        def __init__(self):
            self.clickhouse = FakeClickHouse()

    with pytest.raises(HTTPException) as error:
        realtime_minutes("000001.SZ", "invalid", data=FakeData())
    assert error.value.status_code == 422
