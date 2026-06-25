from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pandas as pd

from backtests.data_providers.clickhouse import ClickHouseBarDataProvider
from backtests.data_providers.clickhouse import ClickHouseBarSchema
from backtests.data_providers.clickhouse import ClickHouseConnectionConfig
from backtests.data_providers.clickhouse import ensure_json_each_row
from backtests.data_providers.clickhouse import quote_identifier
from backtests.data_providers.clickhouse import quote_literal


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class BenchmarkConfig:
    code: str = ""
    name: str = ""
    table: str = "dwd_index_daily"
    code_column: str = "index_code"
    date_column: str = "trade_date"
    close_column: str = "close"

    @property
    def enabled(self) -> bool:
        return bool(self.code.strip())

    @property
    def display_name(self) -> str:
        return self.name.strip() or self.code.strip().upper() or "Benchmark"


def add_benchmark_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--benchmark-code",
        default=env("BACKTEST_BENCHMARK_CODE", ""),
        help="Optional benchmark code, for example 000300.SH. Empty disables benchmark output.",
    )
    parser.add_argument(
        "--benchmark-name",
        default=env("BACKTEST_BENCHMARK_NAME", ""),
        help="Display name for benchmark reports.",
    )
    parser.add_argument(
        "--benchmark-table",
        default=env("BACKTEST_BENCHMARK_TABLE", "dwd_index_daily"),
        help="ClickHouse table containing benchmark close prices.",
    )
    parser.add_argument(
        "--benchmark-code-column",
        default=env("BACKTEST_BENCHMARK_CODE_COLUMN", "index_code"),
        help="Benchmark table code column.",
    )
    parser.add_argument(
        "--benchmark-date-column",
        default=env("BACKTEST_BENCHMARK_DATE_COLUMN", "trade_date"),
        help="Benchmark table date column.",
    )
    parser.add_argument(
        "--benchmark-close-column",
        default=env("BACKTEST_BENCHMARK_CLOSE_COLUMN", "close"),
        help="Benchmark table close-price column.",
    )


def add_tearsheet_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tearsheet-path",
        default=env("BACKTEST_TEARSHEET_PATH", ""),
        help="Optional Nautilus tearsheet output path. Supports .html, .png, .jpg, .webp, .svg, .pdf.",
    )
    parser.add_argument(
        "--tearsheet-title",
        default=env("BACKTEST_TEARSHEET_TITLE", ""),
        help="Optional title for the Nautilus tearsheet.",
    )
    parser.add_argument(
        "--tearsheet-theme",
        default=env("BACKTEST_TEARSHEET_THEME", "plotly_white"),
        help="Nautilus tearsheet theme, for example plotly_white, plotly_dark, nautilus, nautilus_dark.",
    )
    parser.add_argument(
        "--tearsheet-height",
        type=int,
        default=int(env("BACKTEST_TEARSHEET_HEIGHT", "1500")),
        help="Nautilus tearsheet height in pixels.",
    )


def benchmark_config_from_args(args: argparse.Namespace) -> BenchmarkConfig:
    return BenchmarkConfig(
        code=str(getattr(args, "benchmark_code", "") or "").strip().upper(),
        name=str(getattr(args, "benchmark_name", "") or "").strip(),
        table=str(getattr(args, "benchmark_table", "dwd_index_daily") or "dwd_index_daily"),
        code_column=str(getattr(args, "benchmark_code_column", "index_code") or "index_code"),
        date_column=str(getattr(args, "benchmark_date_column", "trade_date") or "trade_date"),
        close_column=str(getattr(args, "benchmark_close_column", "close") or "close"),
    )


def benchmark_run_config(args: argparse.Namespace) -> dict[str, Any]:
    config = benchmark_config_from_args(args)
    return {
        "benchmark.code": config.code,
        "benchmark.name": config.display_name if config.enabled else "",
        "benchmark.table": config.table,
        "benchmark.code_column": config.code_column,
        "benchmark.date_column": config.date_column,
        "benchmark.close_column": config.close_column,
    }


def tearsheet_run_config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "tearsheet.path": str(getattr(args, "tearsheet_path", "") or ""),
        "tearsheet.title": str(getattr(args, "tearsheet_title", "") or ""),
        "tearsheet.theme": str(getattr(args, "tearsheet_theme", "plotly_white") or "plotly_white"),
        "tearsheet.height": int(getattr(args, "tearsheet_height", 1500) or 1500),
    }


def apply_benchmark_to_reports(
    args: argparse.Namespace,
    connection: ClickHouseConnectionConfig,
    reports: dict[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    config = benchmark_config_from_args(args)
    if not config.enabled:
        return reports
    benchmark = load_benchmark_returns(
        connection=connection,
        config=config,
        start=str(getattr(args, "start")),
        end=str(getattr(args, "end")),
    )
    updated = dict(reports)
    updated["benchmark"] = benchmark
    if "daily_portfolio" in updated:
        updated["daily_portfolio"] = enrich_daily_portfolio_with_benchmark(
            updated["daily_portfolio"],
            benchmark,
        )
    return updated


def load_benchmark_returns(
    connection: ClickHouseConnectionConfig,
    config: BenchmarkConfig,
    start: str,
    end: str,
) -> pd.DataFrame:
    if not config.enabled:
        return pd.DataFrame()
    provider = ClickHouseBarDataProvider(connection=connection, schema=ClickHouseBarSchema())
    sql = benchmark_query(config=config, start=start, end=end)
    rows = provider.fetch_json_each_row(sql)
    if not rows:
        raise RuntimeError(
            f"No benchmark rows found for {config.code} in {config.table} from {start} to {end}",
        )
    return benchmark_rows_to_frame(rows, config)


def benchmark_query(config: BenchmarkConfig, start: str, end: str) -> str:
    date_column = quote_identifier(config.date_column)
    close_column = quote_identifier(config.close_column)
    sql = f"""
SELECT
    {date_column} AS date,
    {close_column} AS close
FROM {quote_identifier(config.table)}
WHERE {quote_identifier(config.code_column)} = {quote_literal(config.code)}
  AND {date_column} >= parseDateTimeBestEffort({quote_literal(start)})
  AND {date_column} <= parseDateTimeBestEffort({quote_literal(end)})
ORDER BY {date_column} ASC
"""
    return ensure_json_each_row(sql)


def benchmark_rows_to_frame(rows: list[dict[str, Any]], config: BenchmarkConfig) -> pd.DataFrame:
    normalized = []
    previous_close: Decimal | None = None
    first_close: Decimal | None = None
    for row in rows:
        close = decimal_or_none(row.get("close"))
        if close is None or close <= 0:
            continue
        trading_date = pd.Timestamp(row["date"]).date()
        if first_close is None:
            first_close = close
        daily_return = Decimal("0") if previous_close in (None, Decimal("0")) else close / previous_close - Decimal("1")
        cum_return = Decimal("0") if first_close in (None, Decimal("0")) else close / first_close - Decimal("1")
        normalized.append(
            {
                "date": trading_date,
                "benchmark_code": config.code,
                "benchmark_name": config.display_name,
                "benchmark_close": close,
                "benchmark_return": daily_return,
                "benchmark_cum_return": cum_return,
                "benchmark_net_value": Decimal("1") + cum_return,
            },
        )
        previous_close = close
    if not normalized:
        raise RuntimeError(f"Benchmark rows for {config.code} did not contain valid positive close prices")
    return pd.DataFrame(normalized)


def enrich_daily_portfolio_with_benchmark(
    daily_portfolio: pd.DataFrame,
    benchmark: pd.DataFrame,
) -> pd.DataFrame:
    if daily_portfolio.empty or benchmark.empty:
        return daily_portfolio
    portfolio = daily_portfolio.copy()
    portfolio["date"] = portfolio["date"].map(normalize_date)
    benchmark_frame = benchmark.copy()
    benchmark_frame["date"] = benchmark_frame["date"].map(normalize_date)
    result = portfolio.merge(benchmark_frame, on="date", how="left")
    result["excess_return"] = [
        subtract_or_none(row.get("daily_return"), row.get("benchmark_return"))
        for _, row in result.iterrows()
    ]
    result["excess_cum_return"] = [
        relative_return_or_none(row.get("cum_return"), row.get("benchmark_cum_return"))
        for _, row in result.iterrows()
    ]
    return result


def final_benchmark_metrics(portfolio_report: Any) -> dict[str, Any]:
    if not isinstance(portfolio_report, pd.DataFrame) or portfolio_report.empty:
        return {}
    frame = portfolio_report.reset_index(drop=True)
    if "benchmark_cum_return" in frame:
        aligned = frame.loc[frame["benchmark_cum_return"].notna()]
        if not aligned.empty:
            frame = aligned
    final = frame.iloc[-1]
    metrics = {}
    for source, metric in (
        ("benchmark_cum_return", "final_benchmark_return"),
        ("excess_cum_return", "final_excess_return"),
        ("benchmark_name", "benchmark_name"),
        ("benchmark_code", "benchmark_code"),
    ):
        if source in final and pd.notna(final[source]):
            metrics[metric] = final[source]
    return metrics


def normalize_date(value: Any) -> Any:
    if value in (None, ""):
        return value
    return pd.Timestamp(value).date()


def decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        numeric = Decimal(str(value))
    except Exception:
        return None
    if not numeric.is_finite():
        return None
    return numeric


def subtract_or_none(left: Any, right: Any) -> Decimal | None:
    left_decimal = decimal_or_none(left)
    right_decimal = decimal_or_none(right)
    if left_decimal is None or right_decimal is None:
        return None
    return left_decimal - right_decimal


def relative_return_or_none(left_cum_return: Any, right_cum_return: Any) -> Decimal | None:
    left_decimal = decimal_or_none(left_cum_return)
    right_decimal = decimal_or_none(right_cum_return)
    if left_decimal is None or right_decimal is None:
        return None
    denominator = Decimal("1") + right_decimal
    if denominator == 0:
        return None
    return (Decimal("1") + left_decimal) / denominator - Decimal("1")
