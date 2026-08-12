from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pandas as pd

from backtests.data_providers import PredictionDataBundle
from backtests.data_providers import PredictionSignal
from lives.live_common import LivePredictionDataLoader
from market_data import DailyStockData


def test_live_loader_carries_daily_history_and_derives_latest_close() -> None:
    signal_date = date(2026, 7, 7)
    trading_date = date(2026, 7, 8)
    bundle = PredictionDataBundle(
        signals_by_date={
            signal_date: [PredictionSignal(signal_date, "000001.SZ", 0.9, 1, 0.02)],
        },
        universe=["000001.SZ"],
        trading_dates=[date(2026, 7, 4), signal_date, trading_date],
        listed_dates={},
        st_by_date={},
        suspended_by_date={},
        instrument_names={},
        prediction_rows=1,
        selected_rows=1,
    )
    history = (
        DailyStockData("000001.SZ", date(2026, 7, 4), close=9.0),
        DailyStockData("000001.SZ", signal_date, close=10.0, pre_close=9.0, up_limit=10.0),
        DailyStockData("000001.SZ", trading_date, close=99.0, pre_close=10.0),
    )
    loader = LivePredictionDataLoader.__new__(LivePredictionDataLoader)
    loader.args = SimpleNamespace(
        predictions_table="daily_model_predictions",
        stock_codes="000001.SZ",
        all_stocks=False,
        excluded_stock_codes="",
        min_score=None,
        top_frac=0.1,
        max_positions=50,
        signal_warmup_days=7,
        max_universe=0,
    )
    loader.prediction_provider = MagicMock()
    loader.prediction_provider.load.return_value = bundle
    loader.daily_data_provider = MagicMock()
    loader.daily_data_provider.load.return_value = history
    loader._broker_source = SimpleNamespace(venue="QMT")

    with patch(
        "lives.live_common.rolling_request_dates",
        return_value=(
            "2026-06-01",
            "2026-08-01",
            pd.Timestamp("2026-07-08 09:10", tz="Asia/Shanghai"),
        ),
    ):
        context = loader.load(trading_history_days=2)

    request = loader.prediction_provider.load.call_args.args[0]
    assert request.trading_history_days == 2
    loader.daily_data_provider.load.assert_called_once_with(
        stock_codes=["000001.SZ"],
        start_date=date(2026, 7, 4),
        end_date=trading_date,
    )
    assert context.daily_stock_data == history
    assert context.last_closes == {"000001.SZ.QMT": 10.0}
