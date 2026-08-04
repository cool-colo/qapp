#!/usr/bin/env python3
"""
Live target-model-predictions node running against Big QMT (大QMT).

Same trading logic as ``live_qmt_target_model_predictions`` but wired to the
``nautilus_trader.adapters.bigqmt`` adapter (Big QMT Redis RPC bridge) instead of
the QMT ``quant-qmt-proxy`` HTTP/WS adapter. Strategy, ClickHouse data loading,
snapshot recorder, reconciliation and monitoring are shared, unchanged.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NAUTILUS_TRADER_PATH = Path(
    os.environ.get("NAUTILUS_TRADER_PATH", "/data/flc/code/quant/nautilus_trader"),
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if NAUTILUS_TRADER_PATH.exists() and str(NAUTILUS_TRADER_PATH) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_TRADER_PATH))

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


def _apply_bigqmt_args(args: Any) -> None:
    """
    Attach Big QMT RPC-bridge transport settings to args from the environment.

    These are BigQMT-specific and not defined by the shared
    ``live_common.parse_args()`` (which carries QMT ``base_url_*`` / ``api_key``),
    so they are resolved here. The presence of ``bigqmt_redis_host`` also selects
    the BigQMT broker data source in ``build_broker_data_source``.

    Note: ``live_common``'s ``--redis-*`` args configure the Nautilus *cache* Redis;
    the ``BIGQMT_REDIS_*`` settings below are the separate RPC-bridge Redis.
    """
    args.bigqmt_redis_host = _env("BIGQMT_REDIS_HOST", "127.0.0.1")
    args.bigqmt_redis_port = int(_env("BIGQMT_REDIS_PORT", "6379"))
    args.bigqmt_redis_db = int(_env("BIGQMT_REDIS_DB", "5"))
    args.bigqmt_redis_password = _env("BIGQMT_REDIS_PASSWORD")
    args.transport = _env("BIGQMT_TRANSPORT", "redis")
    args.rpc_timeout_secs = float(_env("BIGQMT_RPC_TIMEOUT_SECONDS", "6.0"))
    # account_id falls back to BIGQMT_ACCOUNT_ID when QMT_ACCOUNT_ID is unset.
    if not str(getattr(args, "account_id", "") or "").strip():
        args.account_id = _env("BIGQMT_ACCOUNT_ID", "")


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
        args = legacy.parse_args()
    finally:
        sys.argv = original_argv
    args.log_file_name = _resolve_daily_log_file_name(args.log_file_name, args.exchange_timezone)
    try:
        args.refresh_time = normalize_refresh_time(args.refresh_time)
    except ValueError as exc:
        raise SystemExit(f"invalid configured HH:MM time: {exc}") from exc
    _apply_snapshot_args(args)
    _apply_bigqmt_args(args)
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
                poll_interval_secs=args.poll_interval_secs,
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
                poll_interval_secs=args.poll_interval_secs,
                strategy_name=args.strategy_name,
                enforce_sellable_position=not args.no_sellable_check,
                instrument_provider=instrument_provider,
            ),
            data_factory=BigQMTLiveDataClientFactory,
            exec_factory=BigQMTLiveExecClientFactory,
            reconciliation_instrument_ids=reconciliation_ids,
            timeout_connection_secs=300.0,
        )

    return build_target_model_node(args, loader, _make_venue_clients, _add_snapshot_recorder)


def main() -> None:
    args = parse_args()
    connection = legacy.build_connection(args)
    broker_source = BigQmtBrokerDataSource(args)
    loader = legacy.LivePredictionDataLoader(args, connection, broker_source=broker_source)
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
