#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from urllib.parse import urlunsplit


NAUTILUS_TRADER_PATH = Path(
    os.environ.get("NAUTILUS_TRADER_PATH", "/data/flc/code/quant/nautilus_trader"),
)
if NAUTILUS_TRADER_PATH.exists():
    sys.path.insert(0, str(NAUTILUS_TRADER_PATH))

if TYPE_CHECKING:
    from nautilus_trader.live.node import TradingNode


QMT_CLIENT = "QMT"
QMT_DEFAULT_HTTP_URL = "https://2395ebf9eb74494ba7c720002d305ccb.hn.takin.cc/"


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


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
        description="Run the local EMA cross strategy as a Nautilus live trading node via QMT.",
    )
    parser.add_argument(
        "--symbol",
        default=env("QMT_SYMBOL", "600000.SH"),
        help="QMT stock symbol, for example 600000.SH or 000001.SZ.",
    )
    parser.add_argument(
        "--account-id",
        default=env("QMT_ACCOUNT_ID"),
        help="MiniQMT account ID. Can also be set with QMT_ACCOUNT_ID.",
    )
    parser.add_argument(
        "--account-type",
        default=env("QMT_ACCOUNT_TYPE", "STOCK"),
        help="MiniQMT account type.",
    )
    parser.add_argument(
        "--base-url-http",
        default=env("QMT_BASE_URL_HTTP", QMT_DEFAULT_HTTP_URL),
        help="quant-qmt-proxy HTTP base URL.",
    )
    parser.add_argument(
        "--base-url-ws",
        default=env("QMT_BASE_URL_WS"),
        help="quant-qmt-proxy WebSocket base URL.",
    )
    parser.add_argument(
        "--api-key",
        default=env("QMT_API_KEY"),
        help="Bearer token for quant-qmt-proxy when authentication is enabled.",
    )
    parser.add_argument(
        "--trade-size",
        type=Decimal,
        default=Decimal(env("QMT_TRADE_SIZE", "100")),
        help="Order quantity. A-share lots are normally 100 shares.",
    )
    parser.add_argument(
        "--bar-step",
        type=int,
        default=int(env("QMT_BAR_STEP", "1")),
        choices=(1, 5, 15, 30),
        help="Minute bar interval to subscribe through QMT.",
    )
    parser.add_argument(
        "--fast-ema-period",
        type=int,
        default=int(env("QMT_FAST_EMA_PERIOD", "10")),
        help="Fast EMA period.",
    )
    parser.add_argument(
        "--slow-ema-period",
        type=int,
        default=int(env("QMT_SLOW_EMA_PERIOD", "20")),
        help="Slow EMA period.",
    )
    parser.add_argument(
        "--allow-short",
        action="store_true",
        default=env("QMT_ALLOW_SHORT", "0").lower() in {"1", "true", "yes", "on"},
        help="Allow bearish signals to open short positions after flat. Disabled by default.",
    )
    parser.add_argument(
        "--flatten-on-stop",
        action="store_true",
        default=env("QMT_FLATTEN_ON_STOP", "0").lower() in {"1", "true", "yes", "on"},
        help="Submit flattening orders when the strategy stops. Disabled by default.",
    )
    parser.add_argument(
        "--no-sellable-check",
        action="store_true",
        help="Disable QMT can_use_volume validation before SELL orders.",
    )
    parser.add_argument(
        "--adjust-type",
        default=env("QMT_ADJUST_TYPE", "none"),
        help="QMT dividend adjustment mode for bars and history.",
    )
    parser.add_argument(
        "--trader-id",
        default=env("QMT_TRADER_ID", "QMT-001"),
        help="Nautilus trader ID in NAME-000 format.",
    )
    parser.add_argument(
        "--order-id-tag",
        default=env("QMT_ORDER_ID_TAG", "001"),
        help="Strategy order ID tag.",
    )
    parser.add_argument(
        "--strategy-name",
        default=env("QMT_STRATEGY_NAME", "nautilus"),
        help="Strategy name sent to the QMT execution proxy.",
    )
    parser.add_argument(
        "--poll-interval-secs",
        type=float,
        default=float(env("QMT_POLL_INTERVAL_SECS", "1.0")),
        help="QMT execution polling interval.",
    )
    parser.add_argument(
        "--log-level",
        default=env("QMT_LOG_LEVEL", "INFO"),
        help="Nautilus console log level.",
    )
    parser.add_argument(
        "--log-quote-ticks",
        action="store_true",
        default=env_bool("QMT_LOG_QUOTE_TICKS", False),
        help="Log every quote tick received by the EMA cross strategy.",
    )
    parser.add_argument(
        "--no-reconciliation",
        action="store_true",
        help="Disable startup execution reconciliation.",
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
    return args


def build_node(args: argparse.Namespace) -> "TradingNode":
    from nautilus_trader.adapters.qmt import QMTDataClientConfig
    from nautilus_trader.adapters.qmt import QMTExecClientConfig
    from nautilus_trader.adapters.qmt import QMTInstrumentProviderConfig
    from nautilus_trader.adapters.qmt import QMTLiveDataClientFactory
    from nautilus_trader.adapters.qmt import QMTLiveExecClientFactory
    from nautilus_trader.config import LiveExecEngineConfig
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.config import TradingNodeConfig
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.data import BarType
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.identifiers import TraderId

    from strategies.emac_cross import EMACross
    from strategies.emac_cross import EMACrossConfig

    symbol = args.symbol.strip().upper()
    instrument_id = InstrumentId.from_str(f"{symbol}.QMT")
    bar_type = BarType.from_str(
        f"{instrument_id}-{args.bar_step}-MINUTE-LAST-EXTERNAL",
    )
    instrument_provider = QMTInstrumentProviderConfig(
        load_ids=frozenset({instrument_id}),
    )

    config_node = TradingNodeConfig(
        trader_id=TraderId(args.trader_id),
        logging=LoggingConfig(
            log_level=args.log_level,
            log_level_file=args.log_level,
        ),
        exec_engine=LiveExecEngineConfig(
            reconciliation=not args.no_reconciliation,
            reconciliation_lookback_mins=1440,
            reconciliation_instrument_ids=[instrument_id],
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
                base_url_ws=args.base_url_ws,
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
    strategy = EMACross(
        config=EMACrossConfig(
            instrument_id=instrument_id,
            external_order_claims=[instrument_id],
            bar_type=bar_type,
            trade_size=args.trade_size,
            fast_ema_period=args.fast_ema_period,
            slow_ema_period=args.slow_ema_period,
            allow_short=args.allow_short,
            flatten_on_stop=args.flatten_on_stop,
            log_quote_ticks=args.log_quote_ticks,
            order_id_tag=args.order_id_tag,
        ),
    )

    node.trader.add_strategy(strategy)
    node.add_data_client_factory(QMT_CLIENT, QMTLiveDataClientFactory)
    node.add_exec_client_factory(QMT_CLIENT, QMTLiveExecClientFactory)
    node.build()
    return node


def main() -> None:
    args = parse_args()
    try:
        node = build_node(args)
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
