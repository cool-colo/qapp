from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class ModelPredictionDataRequest:
    start_date: str
    end_date: str
    predictions_table: str = "model_predictions"
    stock_codes: list[str] | None = None
    all_stocks: bool = False
    excluded_stock_codes: set[str] | None = None
    min_score: float | None = None
    top_frac: float = 0.10
    max_positions: int = 30
    signal_warmup_days: int = 7


@dataclass(frozen=True)
class PredictionSignal:
    signal_date: date
    stock_code: str
    score: float
    rank: int


@dataclass(frozen=True)
class PredictionDataBundle:
    signals_by_date: dict[date, list[PredictionSignal]]
    universe: list[str]
    trading_dates: list[date]
    listed_dates: dict[str, date]
    st_by_date: dict[date, set[str]]
    suspended_by_date: dict[date, set[str]]
    instrument_names: dict[str, str]
    prediction_rows: int
    selected_rows: int

    def to_frame(self) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for signal_date, signals in self.signals_by_date.items():
            for signal in signals:
                rows.append(
                    {
                        "date": signal_date,
                        "stock_code": signal.stock_code,
                        "score": signal.score,
                        "rank": signal.rank,
                    },
                )
        return pd.DataFrame(rows)


class ModelPredictionDataProvider(ABC):
    """Base class for reusable model-prediction signal providers."""

    @abstractmethod
    def load(self, request: ModelPredictionDataRequest) -> PredictionDataBundle:
        raise NotImplementedError
