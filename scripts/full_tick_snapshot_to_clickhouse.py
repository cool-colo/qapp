#!/usr/bin/env python3
"""
Whole-market full-tick snapshot -> ClickHouse ODS ingestion.

Designed to run standalone under crontab. On each run it:

1. Enumerates the whole A-share universe (Shanghai + Shenzhen + Beijing) from
   the quant-qmt-proxy ``/sectors`` endpoint (the ``沪深京A股`` sector), the same
   source ``QMTInstrumentProvider`` uses for ``load_all``.
2. Fetches a full-tick snapshot for every symbol from the proxy
   ``/api/v1/data/full-tick`` endpoint (chunked), preserving the five-level
   bid/ask depth arrays.
3. Writes each tick as a row into a ClickHouse ODS table, tagged with the
   ingest wall-clock time so multiple intraday runs accumulate append-only.

This is infrastructure plumbing (market-data capture), not strategy logic, so
it talks to the QMT proxy HTTP API directly — the same exception the repo
already makes for ``lives/sell_all_sellable.py``. It does NOT go through
Nautilus, which has no full-tick data type.

Downstream, a scheduled SQL job dedups the ODS rows of a trading day into the
DWD table (see ``--emit-dwd-sql`` / ``scripts/full_tick_ods_to_dwd.sql``).

Run from the repo root so ``lives``/``backtests`` import as top-level packages::

    python -m scripts.full_tick_snapshot_to_clickhouse
    python -m scripts.full_tick_snapshot_to_clickhouse --dry-run --max-symbols 5
    python -m scripts.full_tick_snapshot_to_clickhouse --emit-dwd-sql
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
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

from lives.live_common import QMT_DEFAULT_HTTP_URL  # noqa: E402
from lives.live_common import env  # noqa: E402
from lives.live_common import qmt_symbol  # noqa: E402
from scripts._full_tick_snapshot_common import SHANGHAI_TZ  # noqa: E402
from scripts._full_tick_snapshot_common import add_clickhouse_args  # noqa: E402
from scripts._full_tick_snapshot_common import build_rows  # noqa: E402
from scripts._full_tick_snapshot_common import clickhouse_execute  # noqa: E402
from scripts._full_tick_snapshot_common import chunks  # noqa: E402
from scripts._full_tick_snapshot_common import create_ods_table_sql  # noqa: E402
from scripts._full_tick_snapshot_common import dwd_sync_sql  # noqa: E402
from scripts._full_tick_snapshot_common import insert_rows  # noqa: E402

_LOGGER = logging.getLogger("full_tick_snapshot")

# The QMT whole-market A-share sector: Shanghai + Shenzhen + Beijing. Matches
# QMTInstrumentProvider.DEFAULT_LOAD_ALL_SECTORS.
WHOLE_MARKET_SECTOR = "沪深京A股"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    # QMT proxy
    parser.add_argument("--base-url-http", default=env("QMT_BASE_URL_HTTP", QMT_DEFAULT_HTTP_URL))
    parser.add_argument("--api-key", default=env("QMT_API_KEY"))
    parser.add_argument("--sector", default=env("QMT_FULL_TICK_SECTOR", WHOLE_MARKET_SECTOR))
    parser.add_argument(
        "--http-timeout-secs",
        type=float,
        default=float(env("QMT_HTTP_TIMEOUT_SECS", "30")),
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=int(env("QMT_FULL_TICK_CHUNK_SIZE", "500")),
        help="Symbols per /full-tick request.",
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=int(env("QMT_FULL_TICK_MAX_SYMBOLS", "0")),
        help="Cap the universe for testing (0 = whole market).",
    )
    add_clickhouse_args(parser)
    parser.add_argument("--log-level", default=env("QMT_LOG_LEVEL", "INFO"))
    return parser


# ---------------------------------------------------------------------------
# QMT proxy HTTP
# ---------------------------------------------------------------------------
def _proxy_get(base_url: str, api_key: str | None, path: str, timeout: float) -> Any:
    url = f"{base_url}{path}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    return _read_proxy_json(request, timeout)


def _proxy_post(base_url: str, api_key: str | None, path: str, body: dict[str, Any], timeout: float) -> Any:
    url = f"{base_url}{path}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    return _read_proxy_json(request, timeout)


def _read_proxy_json(request: urllib.request.Request, timeout: float, max_attempts: int = 5) -> Any:
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict) and not payload.get("success", True):
                raise RuntimeError(str(payload.get("message") or payload))
            if isinstance(payload, dict) and "data" in payload:
                return payload["data"]
            return payload
        except (urllib.error.URLError, ValueError, TimeoutError, RuntimeError) as exc:
            if attempt >= max_attempts:
                raise RuntimeError(
                    f"QMT proxy request {request.get_method()} {request.full_url} "
                    f"failed after {max_attempts} attempts: {exc}"
                ) from exc
            _LOGGER.warning(
                "QMT proxy request failed (attempt %d/%d), retrying in 1s: %s",
                attempt,
                max_attempts,
                exc,
            )
            time.sleep(1.0)
    raise RuntimeError("unreachable")


def load_universe(args: argparse.Namespace) -> list[str]:
    """Enumerate whole-market QMT symbols from the proxy /sectors endpoint."""
    base_url = str(args.base_url_http or "").rstrip("/")
    if not base_url:
        raise SystemExit("--base-url-http (QMT_BASE_URL_HTTP) is required")
    data = _proxy_get(base_url, args.api_key, "/api/v1/data/sectors", args.http_timeout_secs)
    sectors = data.get("items", []) if isinstance(data, dict) else (data or [])
    seen: set[str] = set()
    symbols: list[str] = []
    for sector in sectors:
        if str(sector.get("sector_name", "")) != args.sector:
            continue
        for raw in sector.get("symbols", []) or []:
            symbol = qmt_symbol(str(raw))
            if symbol and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    if not symbols:
        raise SystemExit(
            f"proxy /sectors returned no symbols for sector {args.sector!r}; "
            f"available sectors: {[s.get('sector_name') for s in sectors][:20]}"
        )
    if args.max_symbols > 0:
        symbols = symbols[: args.max_symbols]
    return symbols


def fetch_full_tick(args: argparse.Namespace, symbols: list[str]) -> list[dict[str, Any]]:
    """Fetch full-tick snapshots for all symbols, chunked. Returns proxy items."""
    base_url = str(args.base_url_http or "").rstrip("/")
    items: list[dict[str, Any]] = []
    for chunk in chunks(symbols, max(1, args.chunk_size)):
        data = _proxy_post(
            base_url,
            args.api_key,
            "/api/v1/data/full-tick",
            {"symbols": chunk},
            args.http_timeout_secs,
        )
        chunk_items = data.get("items", []) if isinstance(data, dict) else (data or [])
        items.extend(chunk_items)
    return items


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.emit_dwd_sql:
        print(dwd_sync_sql(args.ods_table, args.dwd_table))
        return 0

    now = datetime.now(SHANGHAI_TZ)

    _LOGGER.info("loading whole-market universe (sector=%s)", args.sector)
    symbols = load_universe(args)
    _LOGGER.info("universe: %d symbols", len(symbols))

    _LOGGER.info("fetching full-tick snapshot in chunks of %d", args.chunk_size)
    items = fetch_full_tick(args, symbols)
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
