#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NAUTILUS_TRADER_PATH = Path(
    os.environ.get("NAUTILUS_TRADER_PATH", "/data/flc/code/quant/nautilus_trader"),
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if NAUTILUS_TRADER_PATH.exists() and str(NAUTILUS_TRADER_PATH) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_TRADER_PATH))

from backtests.data_providers import ClickHouseConnectionConfig  # noqa: E402
from backtests.data_providers import ClickHouseModelPredictionDataProvider  # noqa: E402
from backtests.data_providers import ModelPredictionDataRequest  # noqa: E402
from backtests.data_providers import PredictionDataBundle  # noqa: E402
from backtests.data_providers.clickhouse import ClickHouseBarDataProvider  # noqa: E402
from backtests.data_providers.clickhouse import ClickHouseBarSchema  # noqa: E402
from backtests.data_providers.clickhouse import ensure_json_each_row  # noqa: E402
from backtests.data_providers.clickhouse import quote_identifier  # noqa: E402
from backtests.data_providers.clickhouse import quote_literal  # noqa: E402
from backtests.data_providers.clickhouse_model_predictions import normalize_stock_code  # noqa: E402
from lives.monitoring import PrometheusExporter  # noqa: E402
from lives.monitoring import PrometheusExporterConfig  # noqa: E402
from nautilus_trader.common.enums import LogColor  # noqa: E402



QMT_CLIENT = "QMT"
QMT_DEFAULT_HTTP_URL = "http://172.18.193.224:8000"


@dataclass(frozen=True)
class LivePredictionContext:
    bundle: PredictionDataBundle
    stock_codes: list[str]
    instrument_ids: list[Any]
    bar_types: dict[str, Any]
    instrument_stock_codes: dict[str, str]
    signals_by_date: dict[str, list[dict[str, Any]]]
    last_closes: dict[str, float]


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in (env(name, default) or "").split(",") if item.strip()]


def build_cache_config(args: argparse.Namespace):
    """Return a Redis-backed CacheConfig when --use-redis is set, otherwise None.

    None keeps the default in-memory cache (no persistence across restarts).
    """
    if not args.use_redis:
        return None

    from urllib.parse import quote

    from nautilus_trader.config import CacheConfig
    from nautilus_trader.config import DatabaseConfig

    # Nautilus interpolates username/password directly into a redis:// URL, so
    # any URL-reserved characters (@ : / # ? % ...) must be percent-encoded here
    # or the Rust client rejects the URL with "InvalidClientConfig".
    username = quote(args.redis_username, safe="") if args.redis_username else None
    password = quote(args.redis_password, safe="") if args.redis_password else None

    return CacheConfig(
        database=DatabaseConfig(
            type="redis",
            host=args.redis_host,
            port=args.redis_port,
            username=username,
            password=password,
            ssl=args.redis_ssl,
            connection_timeout=args.redis_connection_timeout,
            response_timeout=args.redis_response_timeout,
            number_of_retries=args.redis_retries,
            max_delay=args.redis_max_delay,
        ),
        flush_on_start=args.redis_flush_on_start,
    )



def env_list_from_value(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def parse_decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def parse_optional_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def derive_ws_url(http_url: str) -> str:
    parsed = urlsplit(http_url)
    if parsed.scheme == "https":
        scheme = "wss"
    elif parsed.scheme == "http":
        scheme = "ws"
    else:
        scheme = parsed.scheme
    return urlunsplit((scheme, parsed.netloc, parsed.path, "", ""))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the model-prediction strategy as a long-running Nautilus live node via QMT.",
    )
    parser.add_argument("--predictions-table", default=env("MODEL_PREDICTIONS_TABLE", "daily_model_predictions"))
    parser.add_argument("--stock-codes", default=",".join(env_list("MODEL_STOCK_CODES", "000001.SZ,000002.SZ")))
    parser.add_argument("--all-stocks", action="store_true", default=env_bool("MODEL_ALL_STOCKS", False))
    parser.add_argument("--excluded-stock-codes", default=",".join(env_list("MODEL_EXCLUDED_STOCK_CODES", "")))
    parser.add_argument(
        "--filter-bj",
        action="store_true",
        default=env_bool("MODEL_ENABLE_FILTER_BJ_STOCK_CODES", False),
    )
    parser.add_argument("--index-code", default=env("MODEL_INDEX_CODE", ""))
    parser.add_argument(
        "--index-weight-lookback-days",
        type=int,
        default=int(env("MODEL_INDEX_WEIGHT_LOOKBACK_DAYS", "370")),
    )
    parser.add_argument("--min-score", type=float, default=parse_optional_float(env("MODEL_MIN_SCORE")))
    parser.add_argument("--top-frac", type=float, default=float(env("MODEL_TOP_FRAC", "0.10")))
    parser.add_argument("--max-positions", type=int, default=int(env("MODEL_MAX_POSITIONS", "30")))
    parser.add_argument(
        "--max-position-percent",
        type=float,
        default=float(env("MODEL_MAX_POSITION_PERCENT", "0.03")),
    )
    parser.add_argument("--holding-days", type=int, default=int(env("MODEL_HOLDING_DAYS", "10")))
    parser.add_argument("--stop-loss", type=float, default=float(env("MODEL_STOP_LOSS", "0.1")))
    parser.add_argument(
        "--trailing-take-profit",
        type=float,
        default=float(env("MODEL_TRAILING_TAKE_PROFIT", "0.0")),
    )
    parser.add_argument(
        "--trailing-take-profit-start",
        type=float,
        default=float(env("MODEL_TRAILING_TAKE_PROFIT_START", "0.0")),
    )
    parser.add_argument("--min-avg-amount", type=float, default=float(env("MODEL_MIN_AVG_AMOUNT", "0.0")))
    parser.add_argument("--min-listed-days", type=int, default=int(env("MODEL_MIN_LISTED_DAYS", "120")))
    parser.add_argument(
        "--unfilled-timeout-secs",
        type=float,
        default=float(env("MODEL_UNFILLED_TIMEOUT_SECS", "60")),
        help="Cancel and resubmit an order that stays unfilled longer than this (0 disables).",
    )
    parser.add_argument(
        "--resubmit-interval-secs",
        type=float,
        default=float(env("MODEL_RESUBMIT_INTERVAL_SECS", "10")),
        help="How often to check for stale unfilled orders to cancel/resubmit.",
    )
    parser.add_argument(
        "--cash-buffer-percent",
        type=float,
        default=float(env("MODEL_CASH_BUFFER_PERCENT", "0.00")),
        help="Fraction of free cash held back when sizing/gating buys (commission + slippage margin).",
    )
    parser.add_argument(
        "--target-cash-buffer-percent",
        type=float,
        default=float(env("MODEL_TARGET_CASH_BUFFER_PERCENT", "0.05")),
        help="Framework target cash reserve; target model weights normally sum to 1 minus this value.",
    )
    parser.add_argument(
        "--target-weight-planner",
        default=env("MODEL_TARGET_WEIGHT_PLANNER", "risk_manager"),
        choices=["equal_weight", "risk_manager"],
        help="Target weight planner used after model entry/exit selection.",
    )
    parser.add_argument(
        "--target-weight-planner-error-policy",
        default=env("MODEL_TARGET_WEIGHT_PLANNER_ERROR_POLICY", "raise"),
        choices=["raise", "equal_weight"],
        help="How to handle target planner failures.",
    )
    parser.add_argument(
        "--risk-manager-base-url",
        default=env("RISK_MANAGER_BASE_URL", "http://127.0.0.1:8000"),
        help="Base URL for risk-manager /v1/portfolio/optimize.",
    )
    parser.add_argument(
        "--risk-manager-risk-model-id",
        default=env("RISK_MANAGER_RISK_MODEL_ID", "cn_a_mean_variance"),
        help="risk-manager risk_model_id for portfolio optimization.",
    )
    parser.add_argument(
        "--risk-manager-mode",
        default=env("RISK_MANAGER_MODE", "live"),
        choices=["backtest", "simulation", "live"],
        help="risk-manager request mode.",
    )
    parser.add_argument(
        "--risk-manager-timeout-secs",
        type=float,
        default=float(env("RISK_MANAGER_TIMEOUT_SECS", "10")),
        help="Timeout for risk-manager optimize requests.",
    )
    parser.add_argument(
        "--stop-time",
        default=env("MODEL_TARGET_STOP_TIME", "14:55"),
        help="Exchange-local HH:MM time after which the target framework stops new convergence work.",
    )
    parser.add_argument(
        "--full-tick-refresh-secs",
        type=float,
        default=float(env("MODEL_FULL_TICK_REFRESH_SECS", "60") or 60),
        help=(
            "Interval in seconds for refreshing the authoritative full-tick snapshot "
            "(today's open, etc.) from the QMT proxy during the trading window. 0 disables."
        ),
    )
    parser.add_argument(
        "--full-tick-prefetch-time",
        default=env("MODEL_FULL_TICK_PREFETCH_TIME", "09:27"),
        help=(
            "Exchange-local HH:MM time (pre-open) to fetch the full-tick snapshot "
            "before the trading window opens. Empty disables the prefetch."
        ),
    )
    parser.add_argument(
        "--limit-stop-mode",
        default=env("MODEL_LIMIT_STOP_MODE", "freeze_symbol"),
        help="Target framework limit handling. Default freezes only the affected symbol.",
    )
    parser.add_argument(
        "--order-slice-notional",
        type=parse_decimal,
        default=parse_decimal(env("MODEL_ORDER_SLICE_NOTIONAL", "300000")),
        help="Target framework order split size in CNY notional. 0 disables splitting.",
    )
    parser.add_argument(
        "--leave-non-targets",
        action="store_true",
        default=env_bool("MODEL_LEAVE_NON_TARGETS", False),
        help="Do not sell holdings absent from the latest target weights.",
    )
    parser.add_argument(
        "--price-offset-ticks",
        type=int,
        default=int(env("MODEL_PRICE_OFFSET_TICKS", "1")),
        help="Limit-order offset in ticks past the touch: buy at ask+N*tick, sell at bid-N*tick.",
    )
    parser.add_argument(
        "--trade-tick-log-sample-rate",
        type=float,
        default=float(env("MODEL_TRADE_TICK_LOG_SAMPLE_RATE", "0.0") or "0.0"),
        help="Fraction (0.0-1.0) of trade ticks to log. 0 disables trade-tick logging.",
    )
    parser.add_argument(
        "--order-book-depth-log-sample-rate",
        type=float,
        default=float(env("MODEL_ORDER_BOOK_DEPTH_LOG_SAMPLE_RATE", "0.0") or "0.0"),
        help="Fraction (0.0-1.0) of order-book depth updates to log. 0 disables depth logging.",
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=int(env("MODEL_METRICS_PORT", "9100")),
        help="Prometheus metrics HTTP port. Set to 0 to disable the exporter.",
    )
    parser.add_argument(
        "--metrics-addr",
        default=env("MODEL_METRICS_ADDR", "0.0.0.0"),
        help="Bind address for the Prometheus metrics HTTP server.",
    )
    parser.add_argument(
        "--metrics-interval-secs",
        type=float,
        default=float(env("MODEL_METRICS_INTERVAL_SECS", "10")),
        help="How often the exporter snapshots portfolio/cache into gauges.",
    )
    parser.add_argument(
        "--metrics-account-label",
        default=env("MODEL_METRICS_ACCOUNT_LABEL", "default"),
        help="Prometheus label value identifying this node/account.",
    )
    parser.add_argument(
        "--excluded-name-prefixes",
        default=",".join(env_list("MODEL_EXCLUDED_NAME_PREFIXES", "*ST,ST,退市")),
        help="Never buy stocks whose instrument name starts with any of these prefixes.",
    )
    parser.add_argument("--signal-warmup-days", type=int, default=int(env("MODEL_SIGNAL_WARMUP_DAYS", "7")))
    parser.add_argument("--max-universe", type=int, default=int(env("MODEL_MAX_UNIVERSE", "0")))
    parser.add_argument(
        "--extra-stock-codes",
        default=",".join(env_list("MODEL_LIVE_EXTRA_STOCK_CODES", "")),
        help="Additional stock codes to load/manage at startup, for example current holdings.",
    )
    parser.add_argument("--history-days", type=int, default=int(env("MODEL_LIVE_HISTORY_DAYS", "45")))
    parser.add_argument(
        "--calendar-lookahead-days",
        type=int,
        default=int(env("MODEL_LIVE_CALENDAR_LOOKAHEAD_DAYS", "30")),
    )
    parser.add_argument(
        "--refresh-time",
        default=env("MODEL_LIVE_REFRESH_TIME", "09:00"),
        help=(
            "Daily reference-data refresh time as HH:MM in --exchange-timezone (default "
            "09:00 Beijing). Fires once per day. Set empty to disable and fall back to "
            "--refresh-interval-secs."
        ),
    )
    parser.add_argument(
        "--refresh-interval-secs",
        type=float,
        default=float(env("MODEL_LIVE_REFRESH_INTERVAL_SECS", "0")),
        help=(
            "Legacy periodic refresh interval in seconds. Only used when --refresh-time "
            "is empty. 0 disables periodic refresh."
        ),
    )
    parser.add_argument("--clickhouse-url", default=env("CLICKHOUSE_URL", "http://127.0.0.1:8123"))
    parser.add_argument("--clickhouse-database", default=env("CLICKHOUSE_DATABASE"))
    parser.add_argument("--clickhouse-user", default=env("CLICKHOUSE_USER", "default"))
    parser.add_argument("--clickhouse-password", default=env("CLICKHOUSE_PASSWORD"))
    parser.add_argument(
        "--clickhouse-timeout-secs",
        type=float,
        default=float(env("CLICKHOUSE_TIMEOUT_SECS", "60")),
    )
    parser.add_argument("--exchange-timezone", default=env("QMT_EXCHANGE_TIMEZONE", "Asia/Shanghai"))
    parser.add_argument(
        "--trading-windows",
        default=env("QMT_TRADING_WINDOWS", "09:29-11:30,13:00-14:55"),
        help=(
            "Live-only: comma-separated HH:MM-HH:MM order sessions (exchange tz). "
            "Orders submit only inside these ranges; the lunch break is excluded. "
            "Default '09:29-11:30,13:00-14:55'."
        ),
    )
    parser.add_argument(
        "--exchange-trading-windows",
        default=env("QMT_EXCHANGE_TRADING_WINDOWS", "09:30-11:30,13:00-14:55"),
        help=(
            "Live-only: comma-separated HH:MM-HH:MM sessions checked against market "
            "data ts_event. Orders submit only when both --trading-windows and this "
            "window match. Default '09:30-11:30,13:00-14:55'."
        ),
    )
    parser.add_argument("--price-precision", type=int, default=int(env("QMT_PRICE_PRECISION", "2")))
    parser.add_argument(
        "--initial-cash",
        type=parse_decimal,
        default=parse_decimal(env("MODEL_LIVE_INITIAL_CASH", env("BACKTEST_INIT_CASH", "1000000"))),
    )
    parser.add_argument("--account-id", default=env("QMT_ACCOUNT_ID"))
    parser.add_argument("--account-type", default=env("QMT_ACCOUNT_TYPE", "STOCK"))
    parser.add_argument("--base-url-http", default=env("QMT_BASE_URL_HTTP", QMT_DEFAULT_HTTP_URL))
    parser.add_argument("--base-url-ws", default=env("QMT_BASE_URL_WS"))
    parser.add_argument("--api-key", default=env("QMT_API_KEY"))
    parser.add_argument("--adjust-type", default=env("QMT_ADJUST_TYPE", "none"))
    parser.add_argument("--trader-id", default=env("QMT_TRADER_ID", "QMT-001"))
    parser.add_argument("--order-id-tag", default=env("QMT_ORDER_ID_TAG", "001"))
    parser.add_argument("--strategy-name", default=env("QMT_STRATEGY_NAME", "nautilus_model_predictions"))
    parser.add_argument(
        "--poll-interval-secs",
        type=float,
        default=float(env("QMT_POLL_INTERVAL_SECS", "1.0")),
    )
    parser.add_argument("--log-level", default=env("QMT_LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--log-directory",
        default=env("QMT_LOG_DIRECTORY"),
        help="Directory for Nautilus log files. Defaults to the working directory.",
    )
    parser.add_argument(
        "--log-file-name",
        default=env("QMT_LOG_FILE_NAME"),
        help="Base log file name (without extension). Defaults to an auto-generated trader-id/timestamp name.",
    )
    parser.add_argument(
        "--use-redis",
        action="store_true",
        default=env_bool("QMT_USE_REDIS", False),
        help="Back the Nautilus cache with Redis instead of the default in-memory cache (persists state across restarts).",
    )
    parser.add_argument(
        "--redis-host",
        default=env("QMT_REDIS_HOST", "127.0.0.1"),
        help="Redis host (used only with --use-redis).",
    )
    parser.add_argument(
        "--redis-port",
        type=int,
        default=int(env("QMT_REDIS_PORT", "6379")),
        help="Redis port (used only with --use-redis).",
    )
    parser.add_argument(
        "--redis-username",
        default=env("QMT_REDIS_USERNAME"),
        help="Redis username (used only with --use-redis).",
    )
    parser.add_argument(
        "--redis-password",
        default=env("QMT_REDIS_PASSWORD"),
        help="Redis password (used only with --use-redis).",
    )
    parser.add_argument(
        "--redis-ssl",
        action="store_true",
        default=env_bool("QMT_REDIS_SSL", False),
        help="Use an SSL/TLS connection to Redis (used only with --use-redis).",
    )
    parser.add_argument(
        "--redis-flush-on-start",
        action="store_true",
        default=env_bool("QMT_REDIS_FLUSH_ON_START", False),
        help="Flush the Redis database on start instead of reusing persisted state.",
    )
    parser.add_argument(
        "--redis-connection-timeout",
        type=int,
        default=int(env("QMT_REDIS_CONNECTION_TIMEOUT_SECS", "5")),
        help="Redis connection timeout in seconds (used only with --use-redis).",
    )
    parser.add_argument(
        "--redis-response-timeout",
        type=int,
        default=int(env("QMT_REDIS_RESPONSE_TIMEOUT_SECS", "5")),
        help="Redis response timeout in seconds (used only with --use-redis).",
    )
    parser.add_argument(
        "--redis-retries",
        type=int,
        default=int(env("QMT_REDIS_RETRIES", "3")),
        help="Redis connection retry attempts (used only with --use-redis).",
    )
    parser.add_argument(
        "--redis-max-delay",
        type=int,
        default=int(env("QMT_REDIS_MAX_DELAY_SECS", "5")),
        help="Maximum Redis retry backoff delay in seconds (used only with --use-redis).",
    )
    parser.add_argument(
        "--load-cache-on-start",
        action="store_true",
        default=env_bool("QMT_LOAD_CACHE_ON_START", False),
        help="Replay persisted Nautilus execution cache before live reconciliation. Defaults off for QMT live runs.",
    )
    parser.add_argument("--no-sellable-check", action="store_true")
    parser.add_argument(
        "--restrict-reconciliation",
        action="store_true",
        help="Only reconcile instruments loaded at startup. By default live reconciliation is not narrowed.",
    )
    parser.add_argument(
        "--complete-instrument-details",
        action="store_true",
        default=env_bool("QMT_COMPLETE_INSTRUMENT_DETAILS", False),
    )
    parser.add_argument(
        "--no-load-all-instruments",
        dest="load_all_instruments",
        action="store_false",
        default=env_bool("QMT_LOAD_ALL_INSTRUMENTS", True),
        help=(
            "By default the venue's full instrument set is loaded so reconciliation "
            "can import every held position into the cache (held names outside today's "
            "universe must be reconciled to be sold). Pass this to load only the "
            "universe instruments instead (positions outside it will not reconcile)."
        ),
    )
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="Build and dispose the node without connecting or running.",
    )
    args = parser.parse_args()
    if not args.base_url_ws:
        args.base_url_ws = derive_ws_url(args.base_url_http)
    if not args.account_id:
        parser.error("--account-id is required, or set QMT_ACCOUNT_ID")
    if args.history_days < max(2, args.signal_warmup_days):
        parser.error("--history-days must cover at least --signal-warmup-days and previous trading day data")
    return args


def build_connection(args: argparse.Namespace) -> ClickHouseConnectionConfig:
    return ClickHouseConnectionConfig(
        url=args.clickhouse_url,
        database=args.clickhouse_database,
        user=args.clickhouse_user,
        password=args.clickhouse_password,
        timeout_secs=args.clickhouse_timeout_secs,
    )


def build_prediction_request(
    args: argparse.Namespace,
    start: str,
    end: str,
) -> ModelPredictionDataRequest:
    return ModelPredictionDataRequest(
        start_date=start,
        end_date=end,
        predictions_table=args.predictions_table,
        stock_codes=env_list_from_value(args.stock_codes),
        all_stocks=args.all_stocks,
        excluded_stock_codes=set(env_list_from_value(args.excluded_stock_codes)),
        enable_filter_bj_stock_codes=args.filter_bj,
        index_code=args.index_code.strip().upper(),
        index_weight_lookback_days=args.index_weight_lookback_days,
        min_score=args.min_score,
        top_frac=args.top_frac,
        max_positions=args.max_positions,
        min_avg_amount=args.min_avg_amount,
        signal_warmup_days=args.signal_warmup_days,
    )


def qmt_symbol(stock_code: str) -> str:
    return stock_code.strip().upper()


def stock_code_from_instrument_id(instrument_id: Any) -> str | None:
    text = str(instrument_id).strip().upper()
    if text.endswith(".QMT"):
        text = text[:-4]
    return normalize_stock_code(text)


def build_bar_type(stock_code: str):
    from nautilus_trader.adapters.qmt.common import qmt_symbol_to_instrument_id
    from nautilus_trader.model.data import BarType

    instrument_id = qmt_symbol_to_instrument_id(qmt_symbol(stock_code))
    return BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")


def signal_config(bundle: PredictionDataBundle, loaded_stock_codes: set[str]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for signal_date, signals in bundle.signals_by_date.items():
        rows = []
        for signal in signals:
            if signal.stock_code not in loaded_stock_codes:
                continue
            rows.append(
                {
                    "date": signal.signal_date.isoformat(),
                    "stock_code": signal.stock_code,
                    "score": signal.score,
                    "rank": signal.rank,
                    "avg_amount_20": signal.avg_amount_20,
                },
            )
        if rows:
            result[signal_date.isoformat()] = rows
    return result


def rolling_request_dates(args: argparse.Namespace) -> tuple[str, str, pd.Timestamp]:
    now = pd.Timestamp.now(tz=args.exchange_timezone)
    today = now.date()
    start = (pd.Timestamp(today) - pd.Timedelta(days=int(args.history_days))).date()
    end = (pd.Timestamp(today) + pd.Timedelta(days=int(args.calendar_lookahead_days))).date()
    return start.isoformat(), end.isoformat(), now


class LivePredictionDataLoader:
    def __init__(self, args: argparse.Namespace, connection: ClickHouseConnectionConfig) -> None:
        self.args = args
        self.connection = connection
        self.prediction_provider = ClickHouseModelPredictionDataProvider(connection)
        self.bar_provider = ClickHouseBarDataProvider(connection=connection, schema=ClickHouseBarSchema())

    def load(self, extra_stock_codes: set[str] | None = None) -> LivePredictionContext:
        start, end, now = rolling_request_dates(self.args)
        bundle = self.prediction_provider.load(build_prediction_request(self.args, start, end))
        selected_codes = sorted(subscription_stock_codes(bundle, now.date()))
        if self.args.max_universe > 0:
            selected_codes = selected_codes[: self.args.max_universe]
        stock_codes = sorted(set(selected_codes).union(normalized_stock_codes(extra_stock_codes or set())))
        bar_types = {stock_code: build_bar_type(stock_code) for stock_code in stock_codes}
        instrument_ids = [bar_types[stock_code].instrument_id for stock_code in stock_codes]
        instrument_stock_codes = {
            str(bar_types[stock_code].instrument_id): stock_code
            for stock_code in stock_codes
        }
        last_closes = self.latest_closes(stock_codes, now.date())
        return LivePredictionContext(
            bundle=bundle,
            stock_codes=stock_codes,
            instrument_ids=instrument_ids,
            bar_types={str(bar_type.instrument_id): bar_type for bar_type in bar_types.values()},
            instrument_stock_codes=instrument_stock_codes,
            signals_by_date=signal_config(bundle, set(stock_codes)),
            last_closes={
                str(bar_types[stock_code].instrument_id): close
                for stock_code, close in last_closes.items()
                if stock_code in bar_types
            },
        )

    def latest_closes(self, stock_codes: list[str], as_of_date: pd.Timestamp | Any) -> dict[str, float]:
        if not stock_codes:
            return {}
        results: dict[str, float] = {}
        as_of = pd.Timestamp(as_of_date).date().isoformat()
        for chunk in chunks(stock_codes, 500):
            values = ", ".join(quote_literal(code) for code in chunk)
            sql = f"""
SELECT
    source_code AS stock_code,
    max(trade_date) AS date,
    argMax(close, trade_date) AS close
FROM {quote_identifier("dws_stock_factor_wide")}
WHERE source_code IN ({values})
  AND trade_date <= parseDateTimeBestEffort({quote_literal(as_of)})
GROUP BY source_code
"""
            for row in self.bar_provider.fetch_json_each_row(ensure_json_each_row(sql)):
                stock_code = normalize_stock_code(row.get("stock_code"))
                if not stock_code:
                    continue
                try:
                    close = float(row.get("close"))
                except (TypeError, ValueError):
                    continue
                if close > 0:
                    results[stock_code] = close
        return results

    def full_tick_snapshot(self, stock_codes: list[str]) -> dict[str, dict[str, float]]:
        """
        Authoritative full-tick snapshot per instrument id from the QMT proxy
        ``get_full_tick`` endpoint (``POST /api/v1/data/full-tick``).

        This is infrastructure plumbing — Nautilus has no full-tick data type. The
        proxy tick carries open/last_price/high/low/last_close/bid-ask; the whole
        normalized tick is returned per instrument id (same keying as
        ``last_closes``) so callers can consume whichever fields they need. Symbols
        that return no usable tick are omitted. The strategy currently uses only
        ``open`` to anchor pricing.
        """
        if not stock_codes:
            return {}
        base_url = str(getattr(self.args, "base_url_http", "") or "").rstrip("/")
        if not base_url:
            return {}
        api_key = getattr(self.args, "api_key", None)
        symbol_to_stock = {qmt_symbol(code): code for code in stock_codes}
        by_stock: dict[str, dict[str, float]] = {}
        for chunk in chunks(sorted(symbol_to_stock), 500):
            payload = self._post_full_tick(base_url, api_key, chunk)
            for item in payload:
                symbol = str(item.get("symbol", "")).strip().upper()
                stock_code = symbol_to_stock.get(symbol)
                if not stock_code:
                    continue
                tick = self._coerce_tick_fields(item.get("tick"))
                if tick:
                    by_stock[stock_code] = tick
        return {
            str(build_bar_type(stock_code).instrument_id): tick
            for stock_code, tick in by_stock.items()
        }

    async def broker_position_snapshot(self) -> dict[str, dict[str, Any]]:
        """
        Broker-reported position snapshot keyed by Nautilus instrument id.

        This is infrastructure plumbing for persistence only. Strategy decisions
        continue to use the Nautilus portfolio/cache state.
        """
        return self._normalize_broker_positions(await self._fetch_broker_positions())

    async def _fetch_broker_positions(self) -> list[dict[str, Any]]:
        base_url = str(getattr(self.args, "base_url_http", "") or "").rstrip("/")
        account_id = str(getattr(self.args, "account_id", "") or "").strip()
        if not base_url or not account_id:
            return []
        from nautilus_trader.adapters.qmt.http import QMTHttpClient

        client = QMTHttpClient(
            base_url=base_url,
            api_key=getattr(self.args, "api_key", None),
            timeout_secs=float(getattr(self.args, "clickhouse_timeout_secs", 10.0) or 10.0),
        )
        session_id: str | None = None
        try:
            await client.connect()
            session = await client.open_session(
                account_id,
                str(getattr(self.args, "account_type", "STOCK") or "STOCK"),
            )
            session_id = str(session["session_id"])
            return list(await client.get_positions(session_id))
        finally:
            if session_id is not None:
                await client.close_session(session_id)
            await client.close()

    @classmethod
    def _normalize_broker_positions(cls, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        by_instrument: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            stock_code = cls._position_stock_code(row)
            if not stock_code:
                continue
            instrument_id = str(build_bar_type(stock_code).instrument_id)
            snapshot = {
                "stock_code": stock_code,
                "volume": cls._first_position_value(row, "volume", "current_amount", "total_volume"),
                "can_use_volume": cls._first_position_value(row, "can_use_volume", "available_volume"),
                "avg_price": cls._first_position_value(row, "avg_price", "open_price", "cost_price"),
                "market_value": cls._first_position_value(row, "market_value"),
                "last_price": cls._first_position_value(row, "last_price"),
                "raw": row,
            }
            by_instrument[instrument_id] = snapshot
        return by_instrument

    @staticmethod
    def _position_stock_code(row: dict[str, Any]) -> str | None:
        for key in ("stock_code", "instrument_id", "symbol"):
            value = row.get(key)
            if value is None:
                continue
            stock_code = normalize_stock_code(str(value).strip().upper().removesuffix(".QMT"))
            stock_code = LivePredictionDataLoader._with_inferred_exchange(stock_code)
            if stock_code:
                return stock_code
        return None

    @staticmethod
    def _with_inferred_exchange(stock_code: str | None) -> str | None:
        if not stock_code or "." in stock_code:
            return stock_code
        if len(stock_code) != 6 or not stock_code.isdigit():
            return stock_code
        if stock_code.startswith(("6", "9")):
            return f"{stock_code}.SH"
        if stock_code.startswith(("0", "2", "3")):
            return f"{stock_code}.SZ"
        if stock_code.startswith(("4", "8")):
            return f"{stock_code}.BJ"
        return stock_code

    @staticmethod
    def _first_position_value(row: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _coerce_tick_fields(tick: Any) -> dict[str, float]:
        if not isinstance(tick, dict):
            return {}
        coerced: dict[str, float] = {}
        for key, value in tick.items():
            try:
                coerced[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return coerced

    def _post_full_tick(
        self,
        base_url: str,
        api_key: str | None,
        symbols: list[str],
    ) -> list[dict[str, Any]]:
        url = f"{base_url}/api/v1/data/full-tick"
        body = json.dumps({"symbols": symbols}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        timeout = float(getattr(self.args, "clickhouse_timeout_secs", 10.0) or 10.0)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError, TimeoutError) as exc:
            raise RuntimeError(f"QMT full-tick request failed: {exc}") from exc
        if isinstance(payload, dict) and not payload.get("success", True):
            raise RuntimeError(str(payload.get("message") or payload))
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(data, dict):
            return list(data.get("items", []))
        return list(data or [])


def normalized_stock_codes(values: set[str] | list[str]) -> set[str]:
    result = set()
    for value in values:
        stock_code = normalize_stock_code(value)
        if stock_code:
            result.add(stock_code)
    return result


def subscription_stock_codes(bundle: PredictionDataBundle, as_of_date: Any) -> set[str]:
    signal_date = subscription_signal_date(bundle, as_of_date)
    if signal_date is None:
        return set(bundle.universe)
    stock_codes = {
        signal.stock_code
        for signal in bundle.signals_by_date.get(signal_date, [])
        if signal.stock_code
    }
    return stock_codes or set(bundle.universe)


def subscription_signal_date(bundle: PredictionDataBundle, as_of_date: Any) -> Any | None:
    signal_dates = sorted(bundle.signals_by_date)
    if not signal_dates:
        return None

    today = pd.Timestamp(as_of_date).date()
    trading_dates = sorted(pd.Timestamp(value).date() for value in bundle.trading_dates)
    target_date = today
    if trading_dates:
        dates = pd.DatetimeIndex(pd.to_datetime(trading_dates))
        current_index = int(dates.searchsorted(pd.Timestamp(today), side="left"))
        if current_index < len(trading_dates):
            live_trading_date = trading_dates[current_index]
            previous_index = int(dates.searchsorted(pd.Timestamp(live_trading_date), side="left")) - 1
            if previous_index >= 0:
                target_date = trading_dates[previous_index]

    candidates = [value for value in signal_dates if value <= target_date]
    if candidates:
        return candidates[-1]

    candidates = [value for value in signal_dates if value <= today]
    if candidates:
        return candidates[-1]
    return signal_dates[-1]


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]
