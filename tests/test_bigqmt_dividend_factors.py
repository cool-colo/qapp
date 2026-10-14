from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from bigqmt_signal_trader.xtquant_compat import BigQmtXtData
from lives.broker_data_source import BigQmtBrokerDataSource


def source_with_xtdata(xtdata):
    source = BigQmtBrokerDataSource(SimpleNamespace(account_id="test-account"))
    source._trader = MagicMock()
    source._xtdata = xtdata
    return source


def test_dividend_events_read_new_client_frame_and_sort_shanghai_dates():
    rpc = MagicMock(account_id="test-account")
    rpc.call.return_value = {
        "1782403200000": [28.02423, 0.0, 0.0, 0.0, 0.0, 0, 1.023663],
        "1750867200000": [27.673, 0.0, 0.0, 0.0, 0.0, 0, 1.019649],
    }
    source = source_with_xtdata(BigQmtXtData(rpc))

    events = source.divid_events(["600519.SH"], "20260930")

    assert events == {
        "600519.SH": [(date(2025, 6, 26), 1.019649), (date(2026, 6, 26), 1.023663)],
    }
    rpc.call.assert_called_once_with(
        "get_divid_factors",
        {"stock_code": "600519.SH", "start_time": "", "end_time": "20260930"},
    )


def test_empty_dividend_frame_returns_empty_events():
    rpc = MagicMock(account_id="test-account")
    rpc.call.return_value = {}
    source = source_with_xtdata(BigQmtXtData(rpc))

    assert source.divid_events(["000001.SZ"]) == {"000001.SZ": []}


@pytest.mark.parametrize(
    "invalid_time, invalid_coefficient",
    [("invalid", 1.0), (float("nan"), 1.0), (float("inf"), 1.0),
     (1782403200000, float("nan")), (1782403200000, float("inf"))],
)
def test_invalid_dividend_row_does_not_discard_valid_event(invalid_time, invalid_coefficient):
    xtdata = MagicMock()
    xtdata.get_divid_factors.return_value = pd.DataFrame(
        {"time": [invalid_time, 1750867200000], "dr": [invalid_coefficient, 1.019649]},
    )
    source = source_with_xtdata(xtdata)

    assert source.divid_events(["600519.SH"]) == {
        "600519.SH": [(date(2025, 6, 26), 1.019649)],
    }


def test_dividend_rpc_failure_does_not_discard_other_symbol():
    xtdata = MagicMock()
    xtdata.get_divid_factors.side_effect = [
        RuntimeError("test RPC failure"),
        pd.DataFrame({"time": [1750867200000], "dr": [1.019649]}),
    ]
    source = source_with_xtdata(xtdata)

    assert source.divid_events(["000001.SZ", "600519.SH"]) == {
        "600519.SH": [(date(2025, 6, 26), 1.019649)],
    }
