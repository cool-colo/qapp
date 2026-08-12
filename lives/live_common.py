#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import pandas as pd


_LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
NAUTILUS_TRADER_PATH = Path(
    os.environ.get("NAUTILUS_TRADER_PATH", "/data/flc/code/quant/nautilus_trader"),
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if NAUTILUS_TRADER_PATH.exists() and str(NAUTILUS_TRADER_PATH) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_TRADER_PATH))

from backtests.data_providers import ClickHouseConnectionConfig  # noqa: E402
from backtests.data_providers import ClickHouseDailyStockDataProvider  # noqa: E402
from backtests.data_providers import ClickHouseModelPredictionDataProvider  # noqa: E402
from backtests.data_providers import ModelPredictionDataRequest  # noqa: E402
from backtests.data_providers import PredictionDataBundle  # noqa: E402
from backtests.data_providers.clickhouse_model_predictions import normalize_stock_code  # noqa: E402
from market_data import DailyStockData  # noqa: E402
from market_data import latest_closes  # noqa: E402



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
    daily_stock_data: tuple[DailyStockData, ...]
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
    parser.add_argument(
        "--env-file",
        default=env("QAPP_ENV_FILE"),
        help="Explicit .env path (else <script dir>/.env, else <cwd>/.env).",
    )
    parser.add_argument(
        "--config",
        default=env("STRATEGY_CONFIG_FILE"),
        help="Path to the strategy-params YAML file (see configs/strategy.yaml). Required.",
    )
    parser.add_argument(
        "--environment",
        default=env("QAPP_ENV", "live"),
        help="Deployment environment label included in operational alerts.",
    )
    parser.add_argument(
        "--dingtalk-timeout-secs",
        type=float,
        default=float(env("DINGTALK_TIMEOUT_SECS", "5") or "5"),
        help="DingTalk webhook request timeout.",
    )
    parser.add_argument("--predictions-table", default=env("MODEL_PREDICTIONS_TABLE", "daily_model_predictions"))
    parser.add_argument("--stock-codes", default=",".join(env_list("MODEL_STOCK_CODES", "000001.SZ,000002.SZ")))
    parser.add_argument("--all-stocks", action="store_true", default=env_bool("MODEL_ALL_STOCKS", False))
    parser.add_argument("--excluded-stock-codes", default=",".join(env_list("MODEL_EXCLUDED_STOCK_CODES", "")))
    parser.add_argument("--min-score", type=float, default=parse_optional_float(env("MODEL_MIN_SCORE")))
    parser.add_argument("--top-frac", type=float, default=float(env("MODEL_TOP_FRAC", "0.10")))
    parser.add_argument("--max-positions", type=int, default=int(env("MODEL_MAX_POSITIONS", "50")))
    parser.add_argument(
        "--price-offset-ticks",
        type=int,
        default=int(env("MODEL_PRICE_OFFSET_TICKS", "1")),
        help="Limit-order offset in ticks past the touch: buy at ask+N*tick, sell at bid-N*tick.",
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
        "--status-port",
        type=int,
        default=int(env("MODEL_STATUS_PORT", "9200")),
        help="In-process health status HTTP port (lives.status_server). Set to 0 to disable.",
    )
    parser.add_argument(
        "--status-addr",
        default=env("MODEL_STATUS_ADDR", "0.0.0.0"),
        help="Bind address for the health status HTTP server.",
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
        default=env("MODEL_LIVE_REFRESH_TIME", "09:10"),
        help=(
            "Daily reference-data refresh time as HH:MM in --exchange-timezone (default "
            "09:10 Beijing). Fires once per day. Set empty to disable and fall back to "
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
    parser.add_argument("--price-precision", type=int, default=int(env("QMT_PRICE_PRECISION", "2")))
    parser.add_argument("--account-id", default=env("QMT_ACCOUNT_ID"))
    parser.add_argument("--account-type", default=env("QMT_ACCOUNT_TYPE", "STOCK"))
    parser.add_argument("--base-url-http", default=env("QMT_BASE_URL_HTTP", QMT_DEFAULT_HTTP_URL))
    parser.add_argument("--base-url-ws", default=env("QMT_BASE_URL_WS"))
    parser.add_argument("--api-key", default=env("QMT_API_KEY"))
    parser.add_argument("--adjust-type", default=env("QMT_ADJUST_TYPE", "none"))
    parser.add_argument("--trader-id", default=env("QMT_TRADER_ID", "QMT-001"))
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
    if not args.config:
        parser.error("--config is required, or set STRATEGY_CONFIG_FILE (see configs/strategy.yaml)")
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
    trading_history_days: int = 0,
) -> ModelPredictionDataRequest:
    return ModelPredictionDataRequest(
        start_date=start,
        end_date=end,
        predictions_table=args.predictions_table,
        stock_codes=env_list_from_value(args.stock_codes),
        all_stocks=args.all_stocks,
        excluded_stock_codes=set(env_list_from_value(args.excluded_stock_codes)),
        min_score=args.min_score,
        top_frac=args.top_frac,
        max_positions=args.max_positions,
        signal_warmup_days=args.signal_warmup_days,
        trading_history_days=trading_history_days,
    )


def qmt_symbol(stock_code: str) -> str:
    return stock_code.strip().upper()


def stock_code_from_instrument_id(instrument_id: Any) -> str | None:
    text = str(instrument_id).strip().upper()
    for suffix in (".BIGQMT", ".QMT"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return normalize_stock_code(text)


def build_bar_type(stock_code: str, venue: str = "QMT"):
    from nautilus_trader.model.data import BarType
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.identifiers import Symbol
    from nautilus_trader.model.identifiers import Venue

    instrument_id = InstrumentId(symbol=Symbol(qmt_symbol(stock_code)), venue=Venue(venue))
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
                    "pred_return_live": signal.pred_return_live,
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
    def __init__(
        self,
        args: argparse.Namespace,
        connection: ClickHouseConnectionConfig,
        broker_source: Any = None,
    ) -> None:
        from lives.broker_data_source import build_broker_data_source

        self.args = args
        self.connection = connection
        self.prediction_provider = ClickHouseModelPredictionDataProvider(connection)
        self.daily_data_provider = ClickHouseDailyStockDataProvider(connection)
        # The broker source owns the venue-specific full-tick / broker snapshot calls
        # (QMT HTTP proxy vs Big QMT Redis RPC). Defaults to QMT for backward
        # compatibility with the existing QMT entrypoint.
        self._broker_source = broker_source or build_broker_data_source(args)

    def load(
        self,
        extra_stock_codes: set[str] | None = None,
        trading_history_days: int = 0,
    ) -> LivePredictionContext:
        start, end, now = rolling_request_dates(self.args)
        bundle = self.prediction_provider.load(
            build_prediction_request(
                self.args,
                start,
                end,
                trading_history_days=trading_history_days,
            ),
        )
        selected_codes = sorted(subscription_stock_codes(bundle, now.date()))
        if self.args.max_universe > 0:
            selected_codes = selected_codes[: self.args.max_universe]
        stock_codes = sorted(set(selected_codes).union(normalized_stock_codes(extra_stock_codes or set())))
        venue = self._broker_source.venue
        bar_types = {stock_code: build_bar_type(stock_code, venue) for stock_code in stock_codes}
        instrument_ids = [bar_types[stock_code].instrument_id for stock_code in stock_codes]
        instrument_stock_codes = {
            str(bar_types[stock_code].instrument_id): stock_code
            for stock_code in stock_codes
        }
        daily_stock_data = self.daily_data_provider.load(
            stock_codes=stock_codes,
            start_date=min(bundle.trading_dates),
            end_date=now.date(),
        )
        prior_trading_dates = [value for value in bundle.trading_dates if value < now.date()]
        close_as_of_date = max(prior_trading_dates) if prior_trading_dates else now.date()
        last_closes_by_stock = latest_closes(daily_stock_data, close_as_of_date)
        return LivePredictionContext(
            bundle=bundle,
            stock_codes=stock_codes,
            instrument_ids=instrument_ids,
            bar_types={str(bar_type.instrument_id): bar_type for bar_type in bar_types.values()},
            instrument_stock_codes=instrument_stock_codes,
            signals_by_date=signal_config(bundle, set(stock_codes)),
            daily_stock_data=daily_stock_data,
            last_closes={
                str(bar_types[stock_code].instrument_id): close
                for stock_code, close in last_closes_by_stock.items()
                if stock_code in bar_types
            },
        )

    def full_tick_snapshot(self, stock_codes: list[str]) -> dict[str, dict[str, float]]:
        """
        Authoritative full-tick snapshot per instrument id from the broker gateway.

        Infrastructure plumbing — Nautilus has no full-tick data type. The tick
        carries open/last_price/high/low/last_close/bid-ask; the whole normalized
        tick is returned per instrument id (same keying as ``last_closes``). Symbols
        that return no usable tick are omitted. Delegated to the venue broker source.
        """
        return self._broker_source.full_tick_snapshot(stock_codes)

    async def broker_position_snapshot(self) -> dict[str, dict[str, Any]]:
        """
        Broker-reported position snapshot keyed by Nautilus instrument id.

        Infrastructure plumbing for persistence only. Strategy decisions continue to
        use the Nautilus portfolio/cache state.
        """
        return await self._broker_source.broker_position_snapshot()

    async def broker_order_snapshot(self) -> list[dict[str, Any]]:
        """
        Broker-reported order list for the account, each enriched with a normalized
        ``stock_code`` and Nautilus ``instrument_id``.

        Persistence plumbing only: used by the after-close SnapshotRecorder backfill.
        """
        return await self._broker_source.broker_order_snapshot()

    async def broker_trade_snapshot(self) -> list[dict[str, Any]]:
        """
        Broker-reported trade list for the account, each enriched with a normalized
        ``stock_code`` and Nautilus ``instrument_id``.

        Persistence plumbing only: the after-close backfill counterpart for trades.
        """
        return await self._broker_source.broker_trade_snapshot()


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
