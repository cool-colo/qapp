#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
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
from strategies.model_predictions import ModelPredictionsStrategy  # noqa: E402
from strategies.model_predictions import ModelPredictionsStrategyConfig  # noqa: E402


QMT_CLIENT = "QMT"
QMT_DEFAULT_HTTP_URL = "https://2395ebf9eb74494ba7c720002d305ccb.hn.takin.cc/"
DEFAULT_REFRESH_INTERVAL_SECS = 60 * 60


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
    parser.add_argument("--predictions-table", default=env("MODEL_PREDICTIONS_TABLE", "model_predictions"))
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
    parser.add_argument("--stop-loss", type=float, default=float(env("MODEL_STOP_LOSS", "0.05")))
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
        default=float(env("MODEL_UNFILLED_TIMEOUT_SECS", "30")),
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
        default=float(env("MODEL_CASH_BUFFER_PERCENT", "0.01")),
        help="Fraction of free cash held back when sizing/gating buys (commission + slippage margin).",
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
        "--refresh-interval-secs",
        type=float,
        default=float(env("MODEL_LIVE_REFRESH_INTERVAL_SECS", str(DEFAULT_REFRESH_INTERVAL_SECS))),
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
    parser.add_argument("--no-sellable-check", action="store_true")
    parser.add_argument("--no-reconciliation", action="store_true")
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


class LiveModelPredictionsStrategy(ModelPredictionsStrategy):
    def __init__(
        self,
        config: ModelPredictionsStrategyConfig,
        refresh_context: Any,
        refresh_interval_secs: float,
    ) -> None:
        super().__init__(config)
        self._refresh_context = refresh_context
        self._refresh_interval_secs = float(refresh_interval_secs)

    def on_start(self) -> None:
        super().on_start()
        if self._refresh_interval_secs > 0:
            self.clock.set_timer(
                name="MODEL-PREDICTION-DATA-REFRESH",
                interval=timedelta(seconds=self._refresh_interval_secs),
                callback=self._on_refresh_timer,
                fire_immediately=False,
            )

    def _on_refresh_timer(self, _event: Any) -> None:
        try:
            context = self._refresh_context(self._active_stock_codes())
            self.refresh_reference_data(
                instrument_ids=context.instrument_ids,
                bar_types=context.bar_types,
                instrument_stock_codes=context.instrument_stock_codes,
                signals_by_date=context.signals_by_date,
                trading_dates=[value.isoformat() for value in context.bundle.trading_dates],
                listed_dates={key: value.isoformat() for key, value in context.bundle.listed_dates.items()},
                st_by_date={key.isoformat(): sorted(values) for key, values in context.bundle.st_by_date.items()},
                suspended_by_date={
                    key.isoformat(): sorted(values)
                    for key, values in context.bundle.suspended_by_date.items()
                },
                last_closes=context.last_closes,
                subscribe_new_bars=True,
                unsubscribe_removed_bars=True,
            )
            self.log.info(
                f"Refreshed model prediction data: instruments={len(context.instrument_ids)} "
                f"signals={context.bundle.selected_rows}",
            )
        except Exception as exc:
            self.log.warning(f"Model prediction data refresh failed, keeping previous data: {exc}")

    def _active_stock_codes(self) -> set[str]:
        stock_codes = set()
        for instrument_id in self._active_positions:
            stock_code = self._stock_by_instrument.get(instrument_id)
            if stock_code:
                stock_codes.add(stock_code)
        try:
            open_positions = self.cache.positions_open()
        except Exception:
            open_positions = []
        for position in open_positions:
            try:
                if not position.is_long:
                    continue
                instrument_id = str(position.instrument_id)
            except Exception:
                continue
            stock_code = self._stock_by_instrument.get(instrument_id) or stock_code_from_instrument_id(instrument_id)
            if stock_code:
                stock_codes.add(stock_code)
        return stock_codes


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


def build_node(
    args: argparse.Namespace,
    loader: LivePredictionDataLoader,
):
    from nautilus_trader.adapters.qmt import QMTDataClientConfig
    from nautilus_trader.adapters.qmt import QMTExecClientConfig
    from nautilus_trader.adapters.qmt import QMTInstrumentProviderConfig
    from nautilus_trader.adapters.qmt import QMTLiveDataClientFactory
    from nautilus_trader.adapters.qmt import QMTLiveExecClientFactory
    from nautilus_trader.config import LiveExecEngineConfig
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.config import TradingNodeConfig
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.identifiers import TraderId

    extra_stock_codes = normalized_stock_codes(env_list_from_value(args.extra_stock_codes))
    context = loader.load(extra_stock_codes=extra_stock_codes)
    print(
        "[build_node] loaded context: "
        f"stock_codes={len(context.stock_codes)} "
        f"instrument_ids={len(context.instrument_ids)} "
        f"bar_types={len(context.bar_types)} "
        f"signal_dates={len(context.signals_by_date)} "
        f"signals_total={sum(len(v) for v in context.signals_by_date.values())} "
        f"last_closes={len(context.last_closes)} "
        f"trading_dates={len(context.bundle.trading_dates)} "
        f"selected_rows={context.bundle.selected_rows} "
        f"universe={len(context.bundle.universe)}",
        flush=True,
    )
    if not context.instrument_ids:
        print(
            "[build_node] WARNING: zero instruments selected — no bars will be subscribed "
            "and no orders will ever be placed. Check --all-stocks/--stock-codes, the "
            "predictions table, and the ClickHouse data for the requested date range.",
            flush=True,
        )
    if not context.signals_by_date:
        print(
            "[build_node] WARNING: zero signal dates loaded — entries cannot be generated. "
            "Check the predictions table contents and date range.",
            flush=True,
        )
    if not context.last_closes:
        print(
            "[build_node] WARNING: zero last_closes loaded — _submit_target_weight will reject "
            "every order as 'missing_price' until live bars arrive.",
            flush=True,
        )
    instrument_provider = QMTInstrumentProviderConfig(
        load_ids=frozenset(context.instrument_ids),
        complete_details=args.complete_instrument_details,
    )
    reconciliation_ids = context.instrument_ids if args.restrict_reconciliation else None
    config_node = TradingNodeConfig(
        trader_id=TraderId(args.trader_id),
        cache=build_cache_config(args),
        logging=LoggingConfig(
            log_level=args.log_level,
            log_level_file=args.log_level,
            log_directory=args.log_directory,
            log_file_name=args.log_file_name,
        ),
        exec_engine=LiveExecEngineConfig(
            reconciliation=not args.no_reconciliation,
            reconciliation_lookback_mins=1440,
            reconciliation_instrument_ids=reconciliation_ids,
            filter_unclaimed_external_orders=True,
        ),
        data_clients={
            QMT_CLIENT: QMTDataClientConfig(
                base_url_http=args.base_url_http,
                base_url_ws=args.base_url_ws,
                api_key=args.api_key,
                instrument_provider=instrument_provider,
                adjust_type=args.adjust_type,
            ),
        },
        exec_clients={
            QMT_CLIENT: QMTExecClientConfig(
                account_id=args.account_id,
                account_type=args.account_type,
                base_url_http=args.base_url_http,
                api_key=args.api_key,
                instrument_provider=instrument_provider,
                poll_interval_secs=args.poll_interval_secs,
                strategy_name=args.strategy_name,
                enforce_sellable_position=not args.no_sellable_check,
            ),
        },
        timeout_connection=30.0,
        timeout_reconciliation=10.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=5.0,
    )
    node = TradingNode(config=config_node)
    strategy = LiveModelPredictionsStrategy(
        config=ModelPredictionsStrategyConfig(
            instrument_ids=context.instrument_ids,
            external_order_claims=context.instrument_ids,
            bar_types=context.bar_types,
            instrument_stock_codes=context.instrument_stock_codes,
            signals_by_date=context.signals_by_date,
            trading_dates=[value.isoformat() for value in context.bundle.trading_dates],
            listed_dates={key: value.isoformat() for key, value in context.bundle.listed_dates.items()},
            st_by_date={key.isoformat(): sorted(values) for key, values in context.bundle.st_by_date.items()},
            suspended_by_date={
                key.isoformat(): sorted(values)
                for key, values in context.bundle.suspended_by_date.items()
            },
            max_positions=args.max_positions,
            max_position_percent=args.max_position_percent,
            holding_days=args.holding_days,
            stop_loss=args.stop_loss,
            trailing_take_profit=args.trailing_take_profit,
            trailing_take_profit_start=args.trailing_take_profit_start,
            min_listed_days=args.min_listed_days,
            initial_cash=args.initial_cash,
            timezone_name=args.exchange_timezone,
            initial_last_closes=context.last_closes,
            excluded_name_prefixes=tuple(env_list_from_value(args.excluded_name_prefixes)),
            unfilled_timeout_secs=args.unfilled_timeout_secs,
            resubmit_check_interval_secs=args.resubmit_interval_secs,
            cash_buffer_percent=args.cash_buffer_percent,
            order_id_tag=args.order_id_tag,
        ),
        refresh_context=lambda active_stock_codes: loader.load(
            extra_stock_codes=extra_stock_codes.union(active_stock_codes),
        ),
        refresh_interval_secs=args.refresh_interval_secs,
    )
    node.trader.add_strategy(strategy)
    node.add_data_client_factory(QMT_CLIENT, QMTLiveDataClientFactory)
    node.add_exec_client_factory(QMT_CLIENT, QMTLiveExecClientFactory)
    node.build()
    return node


def main() -> None:
    args = parse_args()
    connection = build_connection(args)
    loader = LivePredictionDataLoader(args, connection)
    try:
        node = build_node(args, loader)
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Failed to import Nautilus/QMT dependencies "
            f"({exc.name!r}). Activate the Nautilus environment or install the "
            f"dependencies for {NAUTILUS_TRADER_PATH}.",
        ) from None
    try:
        if not args.build_only:
            node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
