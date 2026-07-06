from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class ModelPredictionSignalEvent:
    signal_date: date
    instrument_id: str
    stock_code: str
    signal_name: str
    score: float | None
    rank: int | None
    side: str
    selected: bool
    extra: dict[str, Any]


def normalize_signals(raw: dict[str, list[dict[str, Any]]]) -> dict[date, list[dict[str, Any]]]:
    result: dict[date, list[dict[str, Any]]] = {}
    for key, signals in raw.items():
        signal_date = pd.Timestamp(key).date()
        normalized = []
        for signal in signals:
            normalized.append(
                {
                    "date": pd.Timestamp(signal["date"]).date(),
                    "stock_code": str(signal["stock_code"]),
                    "score": float(signal["score"]),
                    "rank": int(signal.get("rank", len(normalized) + 1)),
                    "avg_amount_20": signal.get("avg_amount_20"),
                },
            )
        result[signal_date] = normalized
    return result


def normalize_initial_active_positions(
    raw: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for instrument_id, state in (raw or {}).items():
        if not isinstance(state, dict):
            continue
        normalized = dict(state)
        for key in ("entry_price", "high_price", "score"):
            if normalized.get(key) is None:
                continue
            try:
                normalized[key] = float(normalized[key])
            except (TypeError, ValueError):
                normalized.pop(key, None)
        for key in ("entry_date", "last_signal_date"):
            if normalized.get(key) is None:
                continue
            try:
                normalized[key] = pd.Timestamp(normalized[key]).date()
            except (TypeError, ValueError):
                normalized.pop(key, None)
        result[str(instrument_id)] = normalized
    return result


def previous_trading_date(trading_dates: list[date], current_date: date) -> date | None:
    dates = pd.DatetimeIndex(pd.to_datetime(trading_dates))
    index = int(dates.searchsorted(pd.Timestamp(current_date), side="left")) - 1
    if index < 0:
        return None
    return pd.Timestamp(dates[index]).date()


def first_trading_date_on_or_after(trading_dates: list[date], start_date: date | None) -> date | None:
    if start_date is None:
        return None
    dates = pd.DatetimeIndex(pd.to_datetime(trading_dates))
    index = int(dates.searchsorted(pd.Timestamp(start_date), side="left"))
    if index >= len(dates):
        return None
    return pd.Timestamp(dates[index]).date()


def is_rebalance_day(
    trading_dates: list[date],
    rebalance_start_date: date | None,
    current_date: date,
    holding_days: int,
) -> bool:
    if rebalance_start_date is None:
        return False
    if int(holding_days) <= 1:
        return True
    today = pd.Timestamp(current_date).date()
    start = pd.Timestamp(rebalance_start_date).date()
    if today < start:
        return False
    dates = pd.DatetimeIndex(pd.to_datetime(trading_dates))
    start_index = int(dates.searchsorted(pd.Timestamp(start), side="left"))
    end_index = int(dates.searchsorted(pd.Timestamp(today), side="left"))
    return max(0, end_index - start_index) % int(holding_days) == 0
