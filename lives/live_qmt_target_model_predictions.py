#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NAUTILUS_TRADER_PATH = Path(
    os.environ.get("NAUTILUS_TRADER_PATH", "/data/flc/code/quant/nautilus_trader"),
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if NAUTILUS_TRADER_PATH.exists() and str(NAUTILUS_TRADER_PATH) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_TRADER_PATH))

from lives import live_common as legacy
from lives._target_model_node import LiveTargetModelPredictionsStrategy  # noqa: F401  (re-exported for tests / callers)
from lives._target_model_node import VenueClients
from lives._target_model_node import build_target_model_node
from monitoring.dingtalk_alert import FixedTimeEventReporter
from monitoring.dingtalk_alert import load_env
from nautilus_trader.common.enums import LogColor


QMT_CLIENT = legacy.QMT_CLIENT
_DATE_TOKEN_RE = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{8})")


def _preparse_env_file(argv: list[str]) -> str | None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", default=os.environ.get("QAPP_ENV_FILE"))
    known, _ = parser.parse_known_args(argv)
    return known.env_file


def parse_args():
    original_argv = sys.argv[:]
    load_env(
        _preparse_env_file(original_argv[1:]),
        script_dir=Path(__file__).resolve().parent,
    )
    try:
        sys.argv = original_argv
        args = legacy.parse_args()
    finally:
        sys.argv = original_argv
    args.log_file_name = _resolve_daily_log_file_name(args.log_file_name, args.exchange_timezone)
    try:
        args.refresh_time = normalize_refresh_time(args.refresh_time)
    except ValueError as exc:
        raise SystemExit(f"invalid configured HH:MM time: {exc}") from exc
    _apply_snapshot_args(args)
    args.broker_name = "QMT"
    return args


def _apply_snapshot_args(args: Any) -> None:
    """
    Attach daily-snapshot / MySQL settings to args from the environment.

    These are target-model-specific and not defined by the shared
    ``live_common.parse_args()``, so they are resolved here rather than added to
    the shared parser.
    """
    env = os.environ.get
    args.snapshot_before_time = env("MODEL_SNAPSHOT_BEFORE_TIME", "09:27")
    args.snapshot_after_time = env("MODEL_SNAPSHOT_AFTER_TIME", "15:02")
    args.mysql_host = env("MYSQL_HOST", "127.0.0.1")
    args.mysql_port = int(env("MYSQL_PORT", "3306"))
    args.mysql_user = env("MYSQL_USER", "root")
    args.mysql_password = env("MYSQL_PASSWORD", "")
    args.mysql_database = env("MYSQL_DATABASE", "")


def _resolve_daily_log_file_name(log_file_name: str | None, timezone_name: str) -> str | None:
    base_name = (log_file_name or "model_preds").strip() or "model_preds"
    if "{date}" in base_name:
        date_text = pd.Timestamp.now(tz=timezone_name).strftime("%Y-%m-%d")
        return base_name.replace("{date}", date_text)
    if _DATE_TOKEN_RE.search(base_name):
        return base_name
    return None


def normalize_refresh_time(value: str | None) -> str | None:
    if not value or not str(value).strip():
        return None
    parts = str(value).strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError("expected HH:MM or HH:MM:SS")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise ValueError("hour, minute, and second must be integers") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError("time must be within 00:00:00 and 23:59:59")
    return f"{hour:02d}:{minute:02d}"


def _emit_snapshot_status(
    node: Any,
    message: str,
    *,
    is_warning: bool = False,
    color: LogColor = LogColor.BLUE,
) -> None:
    """Mirror snapshot startup status to stdout and the Nautilus file logger."""
    print(message, flush=True)
    get_logger = getattr(node, "get_logger", None)
    if get_logger is None:
        return
    try:
        logger = get_logger()
    except Exception:
        return
    if logger is None:
        return
    log_method = getattr(logger, "warning" if is_warning else "info", None)
    if log_method is None:
        return
    kwargs = {"color": LogColor.YELLOW if is_warning else color}
    try:
        log_method(message, **kwargs)
    except TypeError:
        log_method(message)


def _add_snapshot_recorder(
    args: Any,
    node: Any,
    strategy: Any,
    fetch_full_tick: Any,
    fetch_positions: Any | None = None,
    fetch_orders: Any | None = None,
    fetch_trades: Any | None = None,
    fetch_open_prices: Any | None = None,
    event_reporter: FixedTimeEventReporter | None = None,
) -> None:
    """Build the MySQL-backed daily-snapshot recorder actor and add it to the node."""
    from backtests.result_writers.live_writer import LiveSnapshotWriter
    from backtests.result_writers.live_records import BEFORE_TRADING
    from lives.snapshot_recorder import SnapshotRecorder
    from lives.snapshot_recorder import SnapshotRecorderConfig

    try:
        writer = LiveSnapshotWriter.from_pymysql_kwargs(
            logger=node.get_logger(),
            host=args.mysql_host,
            port=int(args.mysql_port),
            user=args.mysql_user,
            password=args.mysql_password,
            database=args.mysql_database,
            charset="utf8mb4",
            autocommit=False,
        )
    except Exception as exc:
        _emit_snapshot_status(
            node,
            f"[snapshot] MySQL writer init failed: {exc}",
            is_warning=True,
        )
        raise

    def _load_live_target_portfolio(trading_date: Any, signal_date: Any) -> list[dict[str, Any]]:
        return writer.load_target_portfolios(
            str(args.account_id),
            str(args.trader_id),
            trading_date,
            signal_date,
            preferred_snapshot_type=BEFORE_TRADING,
            fallback_to_continuous=True,
        )

    def _load_recent_target_dates(
        trading_date: Any,
        cutoff_trade_date: Any,
        stock_codes: list[str],
    ) -> dict[str, Any]:
        return writer.load_recent_target_dates(
            str(args.account_id),
            str(args.trader_id),
            trading_date,
            cutoff_trade_date,
            stock_codes,
        )

    def _load_position_span_start_dates(
        trading_date: Any,
        cutoff_trade_date: Any,
        stock_codes: list[str],
    ) -> dict[str, Any]:
        return writer.load_position_span_start_dates(
            str(args.account_id),
            str(args.trader_id),
            trading_date,
            cutoff_trade_date,
            stock_codes,
        )

    strategy.configure_live_target_portfolio_loader(_load_live_target_portfolio)
    strategy.configure_recent_target_loader(_load_recent_target_dates)
    strategy.configure_position_span_start_loader(_load_position_span_start_dates)
    recorder = SnapshotRecorder(
        config=SnapshotRecorderConfig(
            account_id=str(args.account_id),
            trader_id=str(args.trader_id),
            broker_name=args.broker_name,
            timezone_name=args.exchange_timezone,
            before_time=args.snapshot_before_time,
            after_time=args.snapshot_after_time,
            trading_windows=strategy.config.trading_windows,
        ),
        writer=writer,
        strategy_ref=strategy,
        fetch_full_tick=fetch_full_tick,
        fetch_positions=fetch_positions,
        fetch_orders=fetch_orders,
        fetch_trades=fetch_trades,
        fetch_open_prices=fetch_open_prices,
        event_reporter=event_reporter.report if event_reporter is not None else None,
    )
    strategy.configure_live_target_plan_persister(recorder.persist_strategy_target_plan)
    node.trader.add_actor(recorder)
    _emit_snapshot_status(
        node,
        f"[snapshot] recorder enabled: account={args.account_id} "
        f"before={args.snapshot_before_time} after={args.snapshot_after_time}",
    )


def build_node(args: Any, loader: legacy.LivePredictionDataLoader):
    from nautilus_trader.adapters.qmt import QMTDataClientConfig
    from nautilus_trader.adapters.qmt import QMTExecClientConfig
    from nautilus_trader.adapters.qmt import QMTInstrumentProviderConfig
    from nautilus_trader.adapters.qmt import QMTLiveDataClientFactory
    from nautilus_trader.adapters.qmt import QMTLiveExecClientFactory

    def _make_venue_clients(args: Any, context: Any) -> VenueClients:
        if args.load_all_instruments:
            instrument_provider = QMTInstrumentProviderConfig(
                load_all=True,
                complete_details=args.complete_instrument_details,
            )
        else:
            instrument_provider = QMTInstrumentProviderConfig(
                load_ids=frozenset(context.instrument_ids),
                complete_details=args.complete_instrument_details,
            )
        if args.restrict_reconciliation and not args.load_all_instruments:
            reconciliation_ids = context.instrument_ids
        else:
            reconciliation_ids = None
        return VenueClients(
            client_id=QMT_CLIENT,
            data_client_config=QMTDataClientConfig(
                base_url_http=args.base_url_http,
                base_url_ws=args.base_url_ws,
                api_key=args.api_key,
                instrument_provider=instrument_provider,
                adjust_type=args.adjust_type,
            ),
            exec_client_config=QMTExecClientConfig(
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
            data_factory=QMTLiveDataClientFactory,
            exec_factory=QMTLiveExecClientFactory,
            reconciliation_instrument_ids=reconciliation_ids,
        )

    return build_target_model_node(args, loader, _make_venue_clients, _add_snapshot_recorder)


def main() -> None:
    args = parse_args()
    connection = legacy.build_connection(args)
    loader = legacy.LivePredictionDataLoader(args, connection)
    node, status_server = build_node(args, loader)
    if args.build_only:
        if status_server is not None:
            status_server.stop()
        node.dispose()
        return
    try:
        node.run()
    finally:
        if status_server is not None:
            status_server.stop()
        node.dispose()


if __name__ == "__main__":
    main()
