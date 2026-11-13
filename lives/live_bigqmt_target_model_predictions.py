#!/usr/bin/env python3
"""
Live target-model-predictions node running against Big QMT (大QMT).

Same trading logic as ``live_qmt_target_model_predictions`` but wired to the
``nautilus_trader.adapters.bigqmt`` adapter (Big QMT Redis RPC bridge) instead of
the QMT ``quant-qmt-proxy`` HTTP/WS adapter. Strategy, ClickHouse data loading,
snapshot recorder, reconciliation and monitoring are shared, unchanged.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from pipe_runtime import configure_runtime_paths  # noqa: E402

configure_runtime_paths(PROJECT_ROOT)

from lives import live_common as legacy
from lives._target_model_node import VenueClients
from lives._target_model_node import build_target_model_node
from lives.broker_data_source import BigQmtBrokerDataSource

# Reuse the venue-agnostic entrypoint helpers from the QMT script.
from lives.live_qmt_target_model_predictions import _add_snapshot_recorder
from lives.live_qmt_target_model_predictions import _apply_snapshot_args
from lives.live_qmt_target_model_predictions import _preparse_env_file
from lives.live_qmt_target_model_predictions import _resolve_daily_log_file_name
from lives.live_qmt_target_model_predictions import normalize_refresh_time
from monitoring.dingtalk_alert import load_env


BIGQMT_CLIENT = "BIGQMT"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value, got {value!r}")


def _apply_bigqmt_args(args: Any, *, tick_trace_enabled: bool | None = None) -> None:
    """
    Attach Big QMT RPC-bridge transport settings to args from the environment.

    These are BigQMT-specific and not defined by the shared
    ``live_common.parse_args()`` (which carries QMT ``base_url_*`` / ``api_key``),
    so they are resolved here. The presence of ``bigqmt_redis_host`` also selects
    the BigQMT broker data source in ``build_broker_data_source``.

    Note: ``live_common``'s ``--redis-*`` args configure the Nautilus *cache* Redis;
    the ``BIGQMT_REDIS_*`` settings below are the separate RPC-bridge Redis.
    ``BIGQMT_ACCOUNT_CASH_OFFSET`` excludes a fixed cash amount from the Nautilus
    account view while the adapter retains the untouched broker asset payload.
    """
    args.bigqmt_redis_host = _env("BIGQMT_REDIS_HOST", "127.0.0.1")
    args.bigqmt_redis_port = int(_env("BIGQMT_REDIS_PORT", "6379"))
    args.bigqmt_redis_db = int(_env("BIGQMT_REDIS_DB", "5"))
    args.bigqmt_redis_password = _env("BIGQMT_REDIS_PASSWORD")
    args.transport = _env("BIGQMT_TRANSPORT", "redis")
    args.rpc_timeout_secs = float(_env("BIGQMT_RPC_TIMEOUT_SECONDS", "30.0"))
    args.bigqmt_account_cash_offset = float(_env("BIGQMT_ACCOUNT_CASH_OFFSET", "0"))
    args.bigqmt_use_quote_push = _env_bool("BIGQMT_USE_QUOTE_PUSH", False)
    args.bigqmt_tick_trace_enabled = (
        _env_bool("BIGQMT_TICK_TRACE_ENABLED", False)
        if tick_trace_enabled is None else tick_trace_enabled
    )
    args.bigqmt_market_poll_enabled = _env_bool(
        "BIGQMT_MARKET_POLL_ENABLED",
        not args.bigqmt_use_quote_push,
    )
    args.bigqmt_quote_push_stale_secs = float(
        _env("BIGQMT_QUOTE_PUSH_STALE_SECONDS", "30.0"),
    )
    args.bigqmt_quote_fallback_poll_interval_secs = float(
        _env("BIGQMT_QUOTE_FALLBACK_POLL_INTERVAL_SECONDS", "60.0"),
    )
    if args.bigqmt_account_cash_offset < 0:
        raise ValueError("BIGQMT_ACCOUNT_CASH_OFFSET must be non-negative")
    if args.bigqmt_quote_push_stale_secs <= 0:
        raise ValueError("BIGQMT_QUOTE_PUSH_STALE_SECONDS must be positive")
    if args.bigqmt_quote_fallback_poll_interval_secs < 0:
        raise ValueError("BIGQMT_QUOTE_FALLBACK_POLL_INTERVAL_SECONDS must be non-negative")
    # account_id falls back to BIGQMT_ACCOUNT_ID when QMT_ACCOUNT_ID is unset.
    if not str(getattr(args, "account_id", "") or "").strip():
        args.account_id = _env("BIGQMT_ACCOUNT_ID", "")


def _add_bigqmt_trace_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bigqmt-tick-trace",
        dest="bigqmt_tick_trace_enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Publish and asynchronously log BigQMT tick timestamps. "
            "Env: BIGQMT_TICK_TRACE_ENABLED (default off); CLI overrides env. "
            "Requires the quote push feed."
        ),
    )


def parse_args():
    original_argv = sys.argv[:]
    load_env(
        _preparse_env_file(original_argv[1:]),
        script_dir=Path(__file__).resolve().parent,
    )
    # BigQMT users set BIGQMT_ACCOUNT_ID; the shared parser requires --account-id
    # (else QMT_ACCOUNT_ID). Seed QMT_ACCOUNT_ID from BIGQMT_ACCOUNT_ID before the
    # shared parser runs so its validation passes, unless --account-id is on argv.
    if "--account-id" not in original_argv and not _env("QMT_ACCOUNT_ID"):
        bigqmt_account = _env("BIGQMT_ACCOUNT_ID")
        if bigqmt_account:
            os.environ["QMT_ACCOUNT_ID"] = bigqmt_account
    try:
        sys.argv = original_argv
        args = legacy.parse_args(configure_parser=_add_bigqmt_trace_args)
    finally:
        sys.argv = original_argv
    args.log_file_name = _resolve_daily_log_file_name(args.log_file_name, args.exchange_timezone)
    try:
        args.refresh_time = normalize_refresh_time(args.refresh_time)
    except ValueError as exc:
        raise SystemExit(f"invalid configured HH:MM time: {exc}") from exc
    _apply_snapshot_args(args)
    _apply_bigqmt_args(args, tick_trace_enabled=args.bigqmt_tick_trace_enabled)
    args.broker_name = "BigQMT"
    return args


def build_node(args: Any, loader: legacy.LivePredictionDataLoader):
    from nautilus_trader.adapters.bigqmt import BigQMTDataClientConfig
    from nautilus_trader.adapters.bigqmt import BigQMTExecClientConfig
    from nautilus_trader.adapters.bigqmt import BigQMTInstrumentProviderConfig
    from nautilus_trader.adapters.bigqmt import BigQMTLiveDataClientFactory
    from nautilus_trader.adapters.bigqmt import BigQMTLiveExecClientFactory

    def _make_venue_clients(args: Any, context: Any) -> VenueClients:
        if args.load_all_instruments:
            instrument_provider = BigQMTInstrumentProviderConfig(
                load_all=True,
                complete_details=args.complete_instrument_details,
            )
        else:
            instrument_provider = BigQMTInstrumentProviderConfig(
                load_ids=frozenset(context.instrument_ids),
                complete_details=args.complete_instrument_details,
            )
        if args.restrict_reconciliation and not args.load_all_instruments:
            reconciliation_ids = context.instrument_ids
        else:
            reconciliation_ids = None
        return VenueClients(
            client_id=BIGQMT_CLIENT,
            data_client_config=BigQMTDataClientConfig(
                account_id=args.account_id,
                redis_host=args.bigqmt_redis_host,
                redis_port=args.bigqmt_redis_port,
                redis_db=args.bigqmt_redis_db,
                redis_password=args.bigqmt_redis_password,
                transport=args.transport,
                rpc_timeout_secs=args.rpc_timeout_secs,
                use_quote_push=args.bigqmt_use_quote_push,
                tick_trace_enabled=args.bigqmt_tick_trace_enabled,
                poll_enabled=args.bigqmt_market_poll_enabled,
                poll_interval_secs=args.poll_interval_secs,
                quote_push_stale_after_secs=args.bigqmt_quote_push_stale_secs,
                quote_push_fallback_poll_interval_secs=(
                    args.bigqmt_quote_fallback_poll_interval_secs
                ),
                instrument_provider=instrument_provider,
                adjust_type=args.adjust_type,
            ),
            exec_client_config=BigQMTExecClientConfig(
                account_id=args.account_id,
                account_type=args.account_type,
                redis_host=args.bigqmt_redis_host,
                redis_port=args.bigqmt_redis_port,
                redis_db=args.bigqmt_redis_db,
                redis_password=args.bigqmt_redis_password,
                transport=args.transport,
                rpc_timeout_secs=args.rpc_timeout_secs,
                poll_enabled=True,
                poll_interval_secs=1.0,
                strategy_name=args.strategy_name,
                enforce_sellable_position=not args.no_sellable_check,
                account_cash_offset=args.bigqmt_account_cash_offset,
                instrument_provider=instrument_provider,
            ),
            data_factory=BigQMTLiveDataClientFactory,
            exec_factory=BigQMTLiveExecClientFactory,
            reconciliation_instrument_ids=reconciliation_ids,
            timeout_connection_secs=600.0,
        )

    def _add_recorders(recorder_args: Any, node: Any, strategy: Any, *sources: Any) -> None:
        _add_snapshot_recorder(recorder_args, node, strategy, *sources)
        if recorder_args.bigqmt_tick_trace_enabled:
            from lives.tick_trace_logging import BigQMTTickTraceLogger

            node.trader.add_actor(BigQMTTickTraceLogger())

    return build_target_model_node(args, loader, _make_venue_clients, _add_recorders)


def main() -> None:
    args = parse_args()
    connection = legacy.build_connection(args)
    broker_source = BigQmtBrokerDataSource(args)
    loader = legacy.LivePredictionDataLoader(args, connection, broker_source=broker_source)
    node, status_server, control_server = build_node(args, loader)
    if args.build_only:
        if control_server is not None:
            control_server.stop()
        if status_server is not None:
            status_server.stop()
        node.dispose()
        return
    try:
        node.run()
    finally:
        if control_server is not None:
            control_server.stop()
        if status_server is not None:
            status_server.stop()
        node.dispose()


if __name__ == "__main__":
    main()
