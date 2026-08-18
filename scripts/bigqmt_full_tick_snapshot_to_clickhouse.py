#!/usr/bin/env python3
"""
Whole-market Big QMT full-tick snapshot -> ClickHouse ODS ingestion.

This is the Big QMT counterpart of ``full_tick_snapshot_to_clickhouse.py``.
It uses the ``bigqmt_signal_trader`` Redis RPC bridge instead of
``quant-qmt-proxy`` while preserving the same ClickHouse ODS/DWD schema.

Shanghai and Shenzhen A-shares are enumerated from Big QMT's ``沪深A股``
sector. Beijing A-shares are enumerated from the documented ``BJ`` whole-market
snapshot and are included by default. The final full-tick fetch is chunked by
symbol and preserves all returned bid/ask depth levels.

Run from the repository root::

    python -m scripts.bigqmt_full_tick_snapshot_to_clickhouse
    python -m scripts.bigqmt_full_tick_snapshot_to_clickhouse --dry-run --max-symbols 5
    python -m scripts.bigqmt_full_tick_snapshot_to_clickhouse --emit-dwd-sql
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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

from lives.live_common import env  # noqa: E402
from lives.live_common import env_bool  # noqa: E402
from scripts._full_tick_snapshot_common import SHANGHAI_TZ  # noqa: E402
from scripts._full_tick_snapshot_common import add_clickhouse_args  # noqa: E402
from scripts._full_tick_snapshot_common import build_rows  # noqa: E402
from scripts._full_tick_snapshot_common import chunks  # noqa: E402
from scripts._full_tick_snapshot_common import clickhouse_execute  # noqa: E402
from scripts._full_tick_snapshot_common import create_ods_table_sql  # noqa: E402
from scripts._full_tick_snapshot_common import dwd_sync_sql  # noqa: E402
from scripts._full_tick_snapshot_common import insert_rows  # noqa: E402


_LOGGER = logging.getLogger("bigqmt_full_tick_snapshot")

DEFAULT_A_SHARE_SECTOR = "沪深A股"


@dataclass(frozen=True)
class LoadedUniverse:
    symbols: list[str]
    beijing_ticks: dict[str, Any]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", default=env("BIGQMT_ACCOUNT_ID"))
    parser.add_argument(
        "--bigqmt-redis-host",
        default=env("BIGQMT_REDIS_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--bigqmt-redis-port",
        type=int,
        default=int(env("BIGQMT_REDIS_PORT", "6379")),
    )
    parser.add_argument(
        "--bigqmt-redis-db",
        type=int,
        default=int(env("BIGQMT_REDIS_DB", "5")),
    )
    parser.add_argument("--bigqmt-redis-username", default=env("BIGQMT_REDIS_USERNAME"))
    parser.add_argument("--bigqmt-redis-password", default=env("BIGQMT_REDIS_PASSWORD"))
    parser.add_argument(
        "--transport",
        default=env("BIGQMT_TRANSPORT", env("BIGQMT_RPC_TRANSPORT", "redis")),
        help="Big QMT RPC transport (normally redis).",
    )
    parser.add_argument(
        "--rpc-timeout-secs",
        type=float,
        default=float(env("BIGQMT_RPC_TIMEOUT_SECONDS", "30")),
    )
    parser.add_argument(
        "--sector",
        default=env("BIGQMT_FULL_TICK_SECTOR", DEFAULT_A_SHARE_SECTOR),
        help="Big QMT sector used to enumerate Shanghai and Shenzhen A-shares.",
    )
    parser.add_argument(
        "--include-beijing",
        action=argparse.BooleanOptionalAction,
        default=env_bool("BIGQMT_FULL_TICK_INCLUDE_BEIJING", True),
        help="Include Beijing A-shares by enumerating the BJ market snapshot.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=int(env("BIGQMT_FULL_TICK_CHUNK_SIZE", "500")),
        help="Symbols per Big QMT get_full_tick request.",
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=int(env("BIGQMT_FULL_TICK_MAX_SYMBOLS", "0")),
        help="Cap the universe for testing (0 = whole market).",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=int(env("BIGQMT_FULL_TICK_MAX_ATTEMPTS", "5")),
        help="Maximum attempts for each Big QMT RPC call.",
    )
    add_clickhouse_args(parser)
    parser.add_argument("--log-level", default=env("BIGQMT_LOG_LEVEL", "INFO"))
    return parser


def build_bigqmt_xtdata(args: argparse.Namespace) -> Any:
    """Construct the MiniQMT-compatible Big QMT market-data client."""
    if not str(args.account_id or "").strip():
        raise SystemExit("--account-id (BIGQMT_ACCOUNT_ID) is required")

    from bigqmt_signal_trader.xtquant_compat import BigQmtXtData
    from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader

    redis_config = {
        "host": args.bigqmt_redis_host,
        "port": args.bigqmt_redis_port,
        "db": args.bigqmt_redis_db,
        "username": args.bigqmt_redis_username,
        "password": args.bigqmt_redis_password,
        "transport": args.transport,
    }
    trader = BigQmtXtTrader(
        account_id=str(args.account_id),
        redis_config=redis_config,
        timeout_seconds=args.rpc_timeout_secs,
    )
    return BigQmtXtData(trader.client)


def _rpc_call(
    args: argparse.Namespace,
    label: str,
    action: Callable[[], Any],
) -> Any:
    attempts = max(1, args.max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Exception as exc:
            if attempt >= attempts:
                raise RuntimeError(
                    f"Big QMT {label} failed after {attempts} attempts: {exc}",
                ) from exc
            _LOGGER.warning(
                "Big QMT %s failed (attempt %d/%d), retrying in 1s: %s",
                label,
                attempt,
                attempts,
                exc,
            )
            time.sleep(1.0)
    raise RuntimeError("unreachable")


def _is_beijing_a_share(symbol: str) -> bool:
    text = symbol.strip().upper()
    if not text.endswith(".BJ"):
        return False
    code = text[:-3]
    # Existing 4/8-series codes and the 920-series code system are equities.
    return len(code) == 6 and code.isdigit() and code.startswith(("4", "8", "9"))


def load_universe(args: argparse.Namespace, xtdata: Any) -> LoadedUniverse:
    """Enumerate A-shares and retain the BJ market snapshot for ingestion."""
    raw_sector_symbols = _rpc_call(
        args,
        f"get_stock_list_in_sector({args.sector!r})",
        lambda: xtdata.get_stock_list_in_sector(args.sector),
    )
    if not isinstance(raw_sector_symbols, (list, tuple)):
        raise RuntimeError(
            "Big QMT get_stock_list_in_sector returned an unexpected payload: "
            f"{type(raw_sector_symbols).__name__}",
        )

    seen: set[str] = set()
    symbols: list[str] = []
    for raw_symbol in raw_sector_symbols:
        symbol = str(raw_symbol).strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)

    needs_beijing = args.include_beijing and (
        args.max_symbols <= 0 or len(symbols) < args.max_symbols
    )
    beijing_ticks: dict[str, Any] = {}
    if needs_beijing:
        beijing_ticks = _rpc_call(
            args,
            "get_full_tick(['BJ'])",
            lambda: xtdata.get_full_tick(["BJ"]),
        )
        if not isinstance(beijing_ticks, dict):
            raise RuntimeError(
                "Big QMT BJ full-tick enumeration returned an unexpected payload: "
                f"{type(beijing_ticks).__name__}",
            )
        for raw_symbol in beijing_ticks:
            symbol = str(raw_symbol).strip().upper()
            if _is_beijing_a_share(symbol) and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)

    if not symbols:
        raise SystemExit(f"Big QMT returned no A-share symbols for sector {args.sector!r}")
    if args.max_symbols > 0:
        symbols = symbols[: args.max_symbols]
    selected = set(symbols)
    return LoadedUniverse(
        symbols=symbols,
        beijing_ticks={
            str(symbol).strip().upper(): tick
            for symbol, tick in beijing_ticks.items()
            if str(symbol).strip().upper() in selected
            and str(symbol).strip().upper().endswith(".BJ")
        },
    )


def _normalized_value(value: Any) -> Any:
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes, dict)):
        try:
            return value.tolist()
        except Exception:
            pass
    return value


def _to_epoch_ms(value: Any) -> int:
    value = _normalized_value(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI_TZ)
        return int(value.timestamp() * 1000)
    if isinstance(value, (int, float)):
        if value > 1_000_000_000_000:
            return int(value)
        if value > 1_000_000_000:
            return int(float(value) * 1000)
    text = str(value or "")
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=SHANGHAI_TZ)
            return int(parsed.timestamp() * 1000)
        except ValueError:
            continue
    return int(time.time() * 1000)


def _array_value(value: Any) -> list[Any]:
    value = _normalized_value(value)
    return list(value) if isinstance(value, (list, tuple)) else []


def normalize_tick_payload(raw_tick: Any) -> dict[str, Any]:
    """Normalize Big QMT camelCase fields to the shared ODS tick contract."""
    if not isinstance(raw_tick, dict):
        return {}
    return {
        "time_ms": _to_epoch_ms(raw_tick.get("time", raw_tick.get("time_ms"))),
        "last_price": _normalized_value(
            raw_tick.get("lastPrice", raw_tick.get("last_price", 0.0)),
        ),
        "open": _normalized_value(raw_tick.get("open", 0.0)),
        "high": _normalized_value(raw_tick.get("high", 0.0)),
        "low": _normalized_value(raw_tick.get("low", 0.0)),
        "last_close": _normalized_value(
            raw_tick.get("lastClose", raw_tick.get("last_close", 0.0)),
        ),
        "amount": _normalized_value(raw_tick.get("amount", 0.0)),
        "volume": _normalized_value(raw_tick.get("volume", 0)),
        "pvolume": _normalized_value(raw_tick.get("pvolume", 0)),
        "open_int": _normalized_value(
            raw_tick.get("openInt", raw_tick.get("open_int", 0)),
        ),
        "stock_status": _normalized_value(
            raw_tick.get("stockStatus", raw_tick.get("stock_status", 0)),
        ),
        "last_settlement_price": _normalized_value(
            raw_tick.get(
                "lastSettlementPrice",
                raw_tick.get("last_settlement_price", 0.0),
            ),
        ),
        "transaction_num": _normalized_value(
            raw_tick.get("transactionNum", raw_tick.get("transaction_num", 0)),
        ),
        "ask_price": _array_value(raw_tick.get("askPrice", raw_tick.get("ask_price"))),
        "bid_price": _array_value(raw_tick.get("bidPrice", raw_tick.get("bid_price"))),
        "ask_vol": _array_value(raw_tick.get("askVol", raw_tick.get("ask_vol"))),
        "bid_vol": _array_value(raw_tick.get("bidVol", raw_tick.get("bid_vol"))),
    }


def fetch_full_tick(
    args: argparse.Namespace,
    xtdata: Any,
    symbols: list[str],
    beijing_ticks: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Fetch SH/SZ symbols in chunks and ingest BJ from its market snapshot."""
    normalized_symbols = [str(symbol).strip().upper() for symbol in symbols]
    requested_beijing = {symbol for symbol in normalized_symbols if symbol.endswith(".BJ")}
    raw_beijing = beijing_ticks
    if requested_beijing and raw_beijing is None:
        data = _rpc_call(
            args,
            "get_full_tick(['BJ'])",
            lambda: xtdata.get_full_tick(["BJ"]),
        )
        if not isinstance(data, dict):
            raise RuntimeError(
                "Big QMT BJ full-tick request returned an unexpected payload: "
                f"{type(data).__name__}",
            )
        raw_beijing = data

    ticks_by_symbol: dict[str, dict[str, Any]] = {}
    for raw_symbol, raw_tick in (raw_beijing or {}).items():
        symbol = str(raw_symbol).strip().upper()
        if symbol not in requested_beijing:
            continue
        tick = normalize_tick_payload(raw_tick)
        if tick:
            ticks_by_symbol[symbol] = tick
        else:
            _LOGGER.warning("skipping unusable Big QMT full-tick for %s", symbol)

    sh_sz_symbols = [symbol for symbol in normalized_symbols if not symbol.endswith(".BJ")]
    for symbol_chunk in chunks(sh_sz_symbols, max(1, args.chunk_size)):
        requested = set(symbol_chunk)
        data = _rpc_call(
            args,
            f"get_full_tick({len(symbol_chunk)} symbols)",
            lambda symbol_chunk=symbol_chunk: xtdata.get_full_tick(symbol_chunk),
        )
        if not isinstance(data, dict):
            raise RuntimeError(
                "Big QMT get_full_tick returned an unexpected payload: "
                f"{type(data).__name__}",
            )
        returned: set[str] = set()
        for raw_symbol, raw_tick in data.items():
            symbol = str(raw_symbol).strip().upper()
            if symbol not in requested:
                _LOGGER.warning("Big QMT full-tick returned unrequested symbol: %s", symbol)
                continue
            tick = normalize_tick_payload(raw_tick)
            if not tick:
                _LOGGER.warning("skipping unusable Big QMT full-tick for %s", symbol)
                continue
            returned.add(symbol)
            ticks_by_symbol[symbol] = tick
        missing = requested - returned
        if missing:
            sample = ", ".join(sorted(missing)[:10])
            _LOGGER.warning(
                "Big QMT full-tick omitted %d requested symbols (sample: %s)",
                len(missing),
                sample,
            )
    missing_beijing = requested_beijing - ticks_by_symbol.keys()
    if missing_beijing:
        sample = ", ".join(sorted(missing_beijing)[:10])
        _LOGGER.warning(
            "Big QMT BJ market snapshot omitted %d requested symbols (sample: %s)",
            len(missing_beijing),
            sample,
        )
    return [
        {"symbol": symbol, "tick": ticks_by_symbol[symbol]}
        for symbol in normalized_symbols
        if symbol in ticks_by_symbol
    ]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.emit_dwd_sql:
        print(dwd_sync_sql(args.ods_table, args.dwd_table))
        return 0

    xtdata = build_bigqmt_xtdata(args)
    now = datetime.now(SHANGHAI_TZ)

    _LOGGER.info(
        "loading whole-market universe (sector=%s, include_beijing=%s)",
        args.sector,
        args.include_beijing,
    )
    universe = load_universe(args, xtdata)
    symbols = universe.symbols
    _LOGGER.info("universe: %d symbols", len(symbols))

    _LOGGER.info("fetching Big QMT full-tick snapshot in chunks of %d", args.chunk_size)
    items = fetch_full_tick(args, xtdata, symbols, universe.beijing_ticks)
    _LOGGER.info("full-tick returned %d items for %d requested symbols", len(items), len(symbols))

    rows = build_rows(items, now)
    _LOGGER.info("assembled %d ODS rows", len(rows))

    if args.dry_run:
        _LOGGER.info("--dry-run: skipping ClickHouse write")
        if rows:
            _LOGGER.info("sample row: %s", json.dumps(rows[0], ensure_ascii=False))
        return 0

    if not rows:
        _LOGGER.warning("no rows to write; exiting without touching ClickHouse")
        return 0

    if not args.no_create_table:
        clickhouse_execute(args, create_ods_table_sql(args.ods_table))
        _LOGGER.info("ensured ODS table %s exists", args.ods_table)

    written = insert_rows(args, rows)
    _LOGGER.info("wrote %d rows into %s", written, args.ods_table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
