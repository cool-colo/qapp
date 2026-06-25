from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from backtests.data_providers.clickhouse import ClickHouseBarDataProvider
from backtests.data_providers.clickhouse import ClickHouseBarSchema
from backtests.data_providers.clickhouse import ClickHouseConnectionConfig
from backtests.data_providers.clickhouse import ensure_json_each_row
from backtests.data_providers.clickhouse import quote_identifier
from backtests.data_providers.clickhouse import quote_literal
from backtests.data_providers.model_base import ModelPredictionDataProvider
from backtests.data_providers.model_base import ModelPredictionDataRequest
from backtests.data_providers.model_base import PredictionDataBundle
from backtests.data_providers.model_base import PredictionSignal


class ClickHouseModelPredictionDataProvider(ModelPredictionDataProvider):
    """
    Build model-prediction signals and eligibility metadata from ClickHouse.

    The provider intentionally returns generic stock codes and dates. Venue
    specific conversion belongs in the backtest or live runner.
    """

    def __init__(self, connection: ClickHouseConnectionConfig) -> None:
        self.connection = connection
        self._client = ClickHouseBarDataProvider(connection, ClickHouseBarSchema())

    def load(self, request: ModelPredictionDataRequest) -> PredictionDataBundle:
        start = pd.Timestamp(request.start_date).normalize()
        end = pd.Timestamp(request.end_date).normalize()
        query_start_hint = (start - pd.Timedelta(days=int(request.signal_warmup_days))).date()
        query_end = end.date()
        trading_dates = self._trading_dates(query_start_hint, query_end)
        if not trading_dates:
            raise RuntimeError("No trading dates found for prediction signal range")
        query_start = trading_dates[0].date()

        stock_codes = self._requested_stock_codes(request)
        index_weights = pd.DataFrame()
        if request.index_code:
            index_weights = self._index_weights(
                index_code=request.index_code,
                start_date=(
                    pd.Timestamp(query_start) - pd.Timedelta(days=int(request.index_weight_lookback_days))
                ).date(),
                end_date=query_end,
            )
            if index_weights.empty:
                raise RuntimeError(f"No index weights found for {request.index_code}")
            stock_codes = sorted(index_weights["stock_code"].dropna().unique().tolist())

        predictions = self._predictions(
            table=request.predictions_table,
            start_date=query_start,
            end_date=query_end,
            stock_codes=stock_codes,
        )
        if predictions.empty:
            raise RuntimeError(
                f"No predictions found in {request.predictions_table} for {query_start}~{query_end}",
            )

        excluded = {normalize_stock_code(code) for code in (request.excluded_stock_codes or set())}
        excluded.discard(None)
        if excluded:
            predictions = predictions.loc[~predictions["stock_code"].isin(excluded)].copy()
        if predictions.empty:
            raise RuntimeError("No predictions left after excluded stock code filtering")

        if request.enable_filter_bj_stock_codes:
            predictions = predictions.loc[~predictions["stock_code"].str.endswith(".BJ")].copy()
        if predictions.empty:
            raise RuntimeError("No predictions left after BJ stock code filtering")

        if request.index_code:
            predictions = filter_by_index_universe(predictions, index_weights)
        if predictions.empty:
            raise RuntimeError("No predictions left after index universe filtering")

        predictions = self._add_liquidity_filter(
            predictions=predictions,
            start_date=query_start,
            end_date=query_end,
            min_avg_amount=request.min_avg_amount,
        )
        selected = select_daily_signals(
            predictions,
            min_score=request.min_score,
            top_frac=request.top_frac,
            max_positions=request.max_positions,
        )
        if selected.empty:
            raise RuntimeError("No prediction signals generated. Check score thresholds and stock universe.")

        universe = sorted(selected["stock_code"].dropna().unique().tolist())
        listed_dates, instrument_names = self._instrument_metadata(universe, start.date(), end.date())
        st_by_date = self._st_dates(universe, query_start, query_end)
        suspended_by_date = self._suspended_dates(universe, query_start, query_end)
        signals_by_date = build_signal_map(selected)

        return PredictionDataBundle(
            signals_by_date=signals_by_date,
            universe=universe,
            trading_dates=[pd.Timestamp(value).date() for value in trading_dates],
            listed_dates=listed_dates,
            st_by_date=st_by_date,
            suspended_by_date=suspended_by_date,
            instrument_names=instrument_names,
            prediction_rows=len(predictions),
            selected_rows=len(selected),
        )

    def preview_predictions_query(self, request: ModelPredictionDataRequest) -> str:
        stock_codes = self._requested_stock_codes(request)
        start = pd.Timestamp(request.start_date).date()
        end = pd.Timestamp(request.end_date).date()
        return self._prediction_sql(request.predictions_table, start, end, stock_codes)

    def _requested_stock_codes(self, request: ModelPredictionDataRequest) -> list[str] | None:
        if request.all_stocks:
            return None
        stock_codes = [normalize_stock_code(code) for code in (request.stock_codes or [])]
        stock_codes = [code for code in stock_codes if code]
        if not stock_codes:
            raise ValueError("stock_codes must not be empty unless all_stocks is enabled")
        return sorted(set(stock_codes))

    def _fetch(self, sql: str) -> list[dict[str, Any]]:
        return self._client.fetch_json_each_row(sql)

    def _trading_dates(self, start_date: date, end_date: date) -> list[pd.Timestamp]:
        sql = f"""
SELECT DISTINCT cal_date AS date
FROM dwd_trade_calendar
WHERE exchange = 'SSE'
  AND is_open = 1
  AND cal_date >= {quote_literal(str(start_date))}
  AND cal_date <= {quote_literal(str(end_date))}
ORDER BY date
"""
        rows = self._fetch(ensure_json_each_row(sql))
        return [pd.Timestamp(row["date"]).normalize() for row in rows]

    def _predictions(
        self,
        table: str,
        start_date: date,
        end_date: date,
        stock_codes: list[str] | None,
    ) -> pd.DataFrame:
        sql = self._prediction_sql(table, start_date, end_date, stock_codes)
        rows = self._fetch(sql)
        return normalize_prediction_frame(pd.DataFrame(rows))

    def _prediction_sql(
        self,
        table: str,
        start_date: date,
        end_date: date,
        stock_codes: list[str] | None,
    ) -> str:
        where = [
            f"pred_date >= {quote_literal(str(start_date))}",
            f"pred_date <= {quote_literal(str(end_date))}",
        ]
        if stock_codes is not None:
            values = ", ".join(quote_literal(to_prediction_table_stock_code(code)) for code in stock_codes)
            where.insert(0, f"stock_code IN ({values})")
        sql = f"""
SELECT stock_code, pred_date AS date, score
FROM {quote_identifier(table)}
WHERE {" AND ".join(where)}
ORDER BY date, score DESC
"""
        return ensure_json_each_row(sql)

    def _index_weights(self, index_code: str, start_date: date, end_date: date) -> pd.DataFrame:
        sql = f"""
SELECT trade_date AS date, index_code, con_code AS stock_code, weight
FROM dwd_index_weight
WHERE index_code = {quote_literal(index_code.upper())}
  AND trade_date >= {quote_literal(str(start_date))}
  AND trade_date <= {quote_literal(str(end_date))}
ORDER BY date, stock_code
"""
        rows = self._fetch(ensure_json_each_row(sql))
        frame = pd.DataFrame(rows)
        if frame.empty:
            return pd.DataFrame(columns=["date", "index_code", "stock_code", "weight"])
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
        frame["stock_code"] = frame["stock_code"].map(normalize_stock_code)
        frame["weight"] = pd.to_numeric(frame["weight"], errors="coerce")
        return frame.dropna(subset=["date", "stock_code"]).reset_index(drop=True)

    def _add_liquidity_filter(
        self,
        predictions: pd.DataFrame,
        start_date: date,
        end_date: date,
        min_avg_amount: float,
    ) -> pd.DataFrame:
        if min_avg_amount <= 0 or predictions.empty:
            result = predictions.copy()
            result["avg_amount_20"] = np.nan
            return result

        stock_codes = sorted(predictions["stock_code"].dropna().unique().tolist())
        values = ", ".join(quote_literal(code) for code in stock_codes)
        sql = f"""
SELECT trade_date AS date, source_code AS stock_code, close, vol AS volume, amount
FROM dws_stock_factor_wide
WHERE source_code IN ({values})
  AND trade_date >= {quote_literal(str(start_date))}
  AND trade_date <= {quote_literal(str(end_date))}
ORDER BY source_code, trade_date
"""
        rows = self._fetch(ensure_json_each_row(sql))
        bars = pd.DataFrame(rows)
        if bars.empty:
            raise RuntimeError("No bars returned for MODEL_MIN_AVG_AMOUNT liquidity filter")
        bars["date"] = pd.to_datetime(bars["date"], errors="coerce").dt.normalize()
        bars["stock_code"] = bars["stock_code"].map(normalize_stock_code)
        bars["close"] = pd.to_numeric(bars["close"], errors="coerce")
        bars["volume"] = pd.to_numeric(bars["volume"], errors="coerce")
        bars["amount"] = pd.to_numeric(bars["amount"], errors="coerce")
        bars["amount"] = bars["amount"].fillna(bars["close"] * bars["volume"])
        bars["avg_amount_20"] = (
            bars.sort_values(["stock_code", "date"])
            .groupby("stock_code")["amount"]
            .transform(lambda values: values.rolling(20, min_periods=20).mean())
        )
        merged = predictions.merge(
            bars[["date", "stock_code", "avg_amount_20"]],
            on=["date", "stock_code"],
            how="left",
        )
        return merged.loc[merged["avg_amount_20"] >= float(min_avg_amount)].copy()

    def _instrument_metadata(
        self,
        stock_codes: list[str],
        start_date: date,
        end_date: date,
    ) -> tuple[dict[str, date], dict[str, str]]:
        values = ", ".join(quote_literal(code) for code in stock_codes)
        sql = f"""
SELECT source_code AS stock_code, instrument_name AS name, list_date, delist_date, event_date
FROM dwd_security_master
WHERE instrument_type = 'stock'
  AND source_code IN ({values})
ORDER BY stock_code, event_date
"""
        rows = self._fetch(ensure_json_each_row(sql))
        frame = pd.DataFrame(rows)
        if frame.empty:
            return {}, {}
        frame["stock_code"] = frame["stock_code"].map(normalize_stock_code)
        frame["list_date"] = pd.to_datetime(frame["list_date"], errors="coerce")
        frame["delist_date"] = pd.to_datetime(frame["delist_date"], errors="coerce")
        frame["event_date"] = pd.to_datetime(frame["event_date"], errors="coerce")
        frame = frame.dropna(subset=["stock_code"]).sort_values(["stock_code", "event_date"])
        frame = frame.drop_duplicates("stock_code", keep="last")
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date)
        listed = frame["list_date"].fillna(pd.Timestamp.min)
        delisted = frame["delist_date"].fillna(pd.Timestamp.max)
        frame = frame.loc[(listed <= end_ts) & (delisted >= start_ts)]
        listed_dates = {
            row.stock_code: pd.Timestamp(row.list_date).date()
            for row in frame.itertuples()
            if not pd.isna(row.list_date)
        }
        names = {
            row.stock_code: "" if pd.isna(row.name) else str(row.name)
            for row in frame.itertuples()
        }
        return listed_dates, names

    def _st_dates(self, stock_codes: list[str], start_date: date, end_date: date) -> dict[date, set[str]]:
        values = ", ".join(quote_literal(code) for code in stock_codes)
        sql = f"""
SELECT trade_date AS date, source_code AS stock_code
FROM dwd_stock_st
WHERE source_code IN ({values})
  AND trade_date >= {quote_literal(str(start_date))}
  AND trade_date <= {quote_literal(str(end_date))}
  AND coalesce(type, '') = 'ST'
ORDER BY date, stock_code
"""
        return rows_to_date_sets(self._fetch(ensure_json_each_row(sql)))

    def _suspended_dates(self, stock_codes: list[str], start_date: date, end_date: date) -> dict[date, set[str]]:
        values = ", ".join(quote_literal(code) for code in stock_codes)
        sql = f"""
SELECT trade_date AS date, ts_code AS stock_code
FROM suspend_d
WHERE ts_code IN ({values})
  AND trade_date >= {quote_literal(str(start_date))}
  AND trade_date <= {quote_literal(str(end_date))}
  AND suspend_type = 'S'
ORDER BY date, stock_code
"""
        return rows_to_date_sets(self._fetch(ensure_json_each_row(sql)))


def normalize_stock_code(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    if text.startswith("STOCK:") or text.startswith("STOCK."):
        text = text[6:]
    if text.endswith(".XSHE"):
        return text[:-5] + ".SZ"
    if text.endswith(".XSHG"):
        return text[:-5] + ".SH"
    if text.endswith(".BJSE"):
        return text[:-5] + ".BJ"
    if len(text) >= 3 and text[:2] in {"SH", "SZ", "BJ"} and "." not in text:
        return text[2:] + "." + text[:2]
    return text


def to_prediction_table_stock_code(value: object) -> str:
    text = normalize_stock_code(value)
    if text is None:
        return ""
    if text.endswith((".SH", ".SZ", ".BJ")):
        return text[-2:] + text[:-3]
    return text


def normalize_prediction_frame(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["date", "stock_code", "score"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    result = frame.copy()
    missing = sorted(set(columns).difference(result.columns))
    if missing:
        raise ValueError(f"model_predictions query result is missing columns: {missing}")
    result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.normalize()
    result["stock_code"] = result["stock_code"].map(normalize_stock_code)
    result["score"] = pd.to_numeric(result["score"], errors="coerce")
    result = result.dropna(subset=["date", "stock_code", "score"]).copy()
    result = result.loc[np.isfinite(result["score"].astype(float))].copy()
    result = result.sort_values(["date", "stock_code", "score"], ascending=[True, True, False])
    result = result.drop_duplicates(["date", "stock_code"], keep="first")
    return result.loc[:, columns].reset_index(drop=True)


def filter_by_index_universe(samples: pd.DataFrame, index_weights: pd.DataFrame) -> pd.DataFrame:
    if samples.empty or index_weights.empty:
        return samples.iloc[0:0].copy()
    frame = samples.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame["stock_code"] = frame["stock_code"].map(normalize_stock_code)
    weights = index_weights[["date", "stock_code"]].copy()
    weights["date"] = pd.to_datetime(weights["date"]).dt.normalize()
    weights["stock_code"] = weights["stock_code"].map(normalize_stock_code)
    weights = weights.dropna(subset=["date", "stock_code"]).drop_duplicates(["date", "stock_code"])
    weight_dates = pd.DatetimeIndex(weights["date"].drop_duplicates().sort_values())
    positions = weight_dates.searchsorted(frame["date"].to_numpy(dtype="datetime64[ns]"), side="right") - 1
    effective_dates = np.full(len(frame), np.datetime64("NaT"), dtype="datetime64[ns]")
    valid = positions >= 0
    effective_dates[valid] = weight_dates.to_numpy()[positions[valid]]
    frame["__index_weight_date"] = pd.to_datetime(effective_dates)
    constituents = weights.rename(columns={"date": "__index_weight_date"})
    filtered = frame.merge(constituents, on=["__index_weight_date", "stock_code"], how="inner")
    return filtered.drop(columns=["__index_weight_date"]).reset_index(drop=True)


def select_daily_signals(
    samples: pd.DataFrame,
    min_score: float | None,
    top_frac: float,
    max_positions: int,
) -> pd.DataFrame:
    frame = samples.copy()
    if min_score is not None:
        frame = frame.loc[frame["score"] >= float(min_score)].copy()
    if frame.empty:
        return frame
    selected_parts = []
    for _, part in frame.groupby("date", sort=True):
        part = part.sort_values("score", ascending=False)
        top_count = max(1, int(math.ceil(len(part) * float(top_frac))))
        if max_positions > 0:
            top_count = min(top_count, int(max_positions))
        selected = part.head(top_count).copy()
        selected["rank"] = range(1, len(selected) + 1)
        selected_parts.append(selected)
    if not selected_parts:
        return frame.iloc[0:0].copy()
    return pd.concat(selected_parts, ignore_index=True)


def build_signal_map(selected: pd.DataFrame) -> dict[date, list[PredictionSignal]]:
    signals_by_date: dict[date, list[PredictionSignal]] = {}
    for value, part in selected.groupby("date", sort=True):
        signal_date = pd.Timestamp(value).date()
        signals = []
        for row in part.sort_values("rank").itertuples():
            avg_amount = getattr(row, "avg_amount_20", None)
            signals.append(
                PredictionSignal(
                    signal_date=signal_date,
                    stock_code=str(row.stock_code),
                    score=float(row.score),
                    rank=int(row.rank),
                    avg_amount_20=None if pd.isna(avg_amount) else float(avg_amount),
                ),
            )
        signals_by_date[signal_date] = signals
    return signals_by_date


def rows_to_date_sets(rows: list[dict[str, Any]]) -> dict[date, set[str]]:
    result: dict[date, set[str]] = defaultdict(set)
    for row in rows:
        stock_code = normalize_stock_code(row.get("stock_code"))
        if not stock_code:
            continue
        result[pd.Timestamp(row["date"]).date()].add(stock_code)
    return dict(result)
