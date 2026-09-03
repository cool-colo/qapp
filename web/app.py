"""FastAPI backend for the live-trading dashboard.

Read-only JSON API over the live_* MySQL tables and ClickHouse daily bars, plus a
static ECharts frontend mounted at ``/``. Bind to 127.0.0.1 (see web/run.sh).
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from web import compare_presets
from web.config import AppConfig, load_config
from web.db import DataAccess, MySqlSource, jsonable_rows, quote_literal
from web.returns_report import (
    RETURN_COLUMNS,
    derive_instrument_suffix,
    query_returns,
)


class ComparePreset(BaseModel):
    name: str
    series: list[dict[str, Any]]

STATIC_DIR = Path(__file__).resolve().parent / "static"
SNAPSHOT_TYPES = {"before_trading", "continuous_trading", "after_trading"}

# Per-account snapshot tables share these key columns.
_ACCOUNT_KEYS = "account_id = %(account_id)s AND trader_id = %(trader_id)s"


@lru_cache(maxsize=1)
def _config() -> AppConfig:
    return load_config()


@lru_cache(maxsize=1)
def _data() -> DataAccess:
    return DataAccess(_config())


def get_data() -> DataAccess:
    return _data()


app = FastAPI(title="Live Trading Dashboard", docs_url="/api/docs", openapi_url="/api/openapi.json")


def _mysql(data: DataAccess, source: str) -> MySqlSource:
    try:
        return data.mysql(source)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown source: {source!r}") from None


def _check_snapshot_type(snapshot_type: str | None) -> None:
    if snapshot_type is not None and snapshot_type not in SNAPSHOT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"snapshot_type must be one of {sorted(SNAPSHOT_TYPES)}",
        )


# ---- reference / discovery -------------------------------------------------


@app.get("/api/sources")
def list_sources(data: DataAccess = Depends(get_data)) -> dict[str, list[str]]:
    return {"sources": data.source_names}


@app.get("/api/accounts")
def list_accounts(
    source: str,
    data: DataAccess = Depends(get_data),
) -> dict[str, Any]:
    mysql = _mysql(data, source)
    rows = mysql.query(
        """
        SELECT DISTINCT account_id, trader_id
        FROM live_asset_snapshot
        ORDER BY account_id, trader_id
        """,
    )
    cfg = _config()
    today = date.today().isoformat()
    month_ago = (date.today() - timedelta(days=30)).isoformat()
    accounts = []
    for row in rows:
        account_id = str(row["account_id"])
        trader_id = str(row["trader_id"])
        if cfg.is_blocked(account_id, trader_id):
            continue
        report_start = cfg.report_start_for(account_id, trader_id) or month_ago
        accounts.append(
            {
                "account_id": account_id,
                "trader_id": trader_id,
                "instrument_suffix": derive_instrument_suffix(mysql, account_id, trader_id),
                "report_start": report_start,
                "report_end": today,
            },
        )
    return {"accounts": accounts}


@app.get("/api/dates")
def list_dates(
    source: str,
    account: str,
    trader: str,
    table: str = Query("live_asset_snapshot"),
    data: DataAccess = Depends(get_data),
) -> dict[str, list[str]]:
    allowed = {
        "live_asset_snapshot",
        "live_position_snapshot",
        "live_target_portfolio",
        "live_order",
        "live_trade",
    }
    if table not in allowed:
        raise HTTPException(status_code=400, detail=f"table must be one of {sorted(allowed)}")
    mysql = _mysql(data, source)
    rows = mysql.query(
        f"""
        SELECT DISTINCT trade_date
        FROM {table}
        WHERE {_ACCOUNT_KEYS}
        ORDER BY trade_date DESC
        """,
        {"account_id": account, "trader_id": trader},
    )
    return {"dates": [row["trade_date"] for row in jsonable_rows(rows)]}


# ---- per-date snapshot tables ---------------------------------------------


@app.get("/api/positions")
def get_positions(
    source: str,
    account: str,
    trader: str,
    date: str,
    snapshot_type: str | None = Query("after_trading"),
    data: DataAccess = Depends(get_data),
) -> dict[str, Any]:
    _check_snapshot_type(snapshot_type)
    mysql = _mysql(data, source)
    where = [_ACCOUNT_KEYS, "trade_date = %(date)s"]
    params: dict[str, Any] = {"account_id": account, "trader_id": trader, "date": date}
    if snapshot_type:
        where.append("snapshot_type = %(snapshot_type)s")
        params["snapshot_type"] = snapshot_type
    rows = mysql.query(
        f"""
        SELECT trade_date, snapshot_type, stock_code, volume, can_use_volume,
               avg_price, open_price, close_price, market_value,
               nt_net_qty, nt_avg_px_open, nt_market_value, nt_last_price, nt_unrealized_pnl,
               source, write_time
        FROM live_position_snapshot
        WHERE {" AND ".join(where)}
        ORDER BY market_value DESC
        """,
        params,
    )
    return {"rows": data.attach_names(jsonable_rows(rows))}


@app.get("/api/target")
def get_target(
    source: str,
    account: str,
    trader: str,
    date: str,
    snapshot_type: str | None = Query(None),
    data: DataAccess = Depends(get_data),
) -> dict[str, Any]:
    _check_snapshot_type(snapshot_type)
    mysql = _mysql(data, source)
    where = [_ACCOUNT_KEYS, "trade_date = %(date)s"]
    params: dict[str, Any] = {"account_id": account, "trader_id": trader, "date": date}
    if snapshot_type:
        where.append("snapshot_type = %(snapshot_type)s")
        params["snapshot_type"] = snapshot_type
    rows = mysql.query(
        f"""
        SELECT trade_date, snapshot_type, signal_date, stock_code,
               target_weight, target_qty, current_qty, open_price, price_source,
               score, expected_return, is_locked, reason, total_asset, investable_asset,
               target_version, write_time
        FROM live_target_portfolio
        WHERE {" AND ".join(where)}
        ORDER BY target_weight DESC
        """,
        params,
    )
    return {"rows": data.attach_names(jsonable_rows(rows))}


@app.get("/api/orders")
def get_orders(
    source: str,
    account: str,
    trader: str,
    date: str | None = Query(None),
    stock_code: str | None = Query(None),
    data: DataAccess = Depends(get_data),
) -> dict[str, Any]:
    mysql = _mysql(data, source)
    where = [_ACCOUNT_KEYS]
    params: dict[str, Any] = {"account_id": account, "trader_id": trader}
    if date:
        where.append("trade_date = %(date)s")
        params["date"] = date
    if stock_code:
        where.append("stock_code = %(stock_code)s")
        params["stock_code"] = stock_code
    rows = mysql.query(
        f"""
        SELECT trade_date, client_order_id, venue_order_id, stock_code,
               side, source, order_type, limit_price, quantity, filled_qty, avg_fill_price,
               status, target_qty, open_price, reason, order_time, write_time
        FROM live_order
        WHERE {" AND ".join(where)}
        ORDER BY order_time, write_time
        """,
        params,
    )
    return {"rows": data.attach_names(jsonable_rows(rows))}


@app.get("/api/trades")
def get_trades(
    source: str,
    account: str,
    trader: str,
    date: str | None = Query(None),
    stock_code: str | None = Query(None),
    data: DataAccess = Depends(get_data),
) -> dict[str, Any]:
    mysql = _mysql(data, source)
    where = [_ACCOUNT_KEYS]
    params: dict[str, Any] = {"account_id": account, "trader_id": trader}
    if date:
        where.append("trade_date = %(date)s")
        params["date"] = date
    if stock_code:
        where.append("stock_code = %(stock_code)s")
        params["stock_code"] = stock_code
    rows = mysql.query(
        f"""
        SELECT trade_date, trade_id, client_order_id, venue_order_id,
               stock_code, side, source, price, quantity, amount, commission,
               trade_time, write_time
        FROM live_trade
        WHERE {" AND ".join(where)}
        ORDER BY trade_time, write_time
        """,
        params,
    )
    return {"rows": data.attach_names(jsonable_rows(rows))}


# ---- time series -----------------------------------------------------------


@app.get("/api/asset")
def get_asset_series(
    source: str,
    account: str,
    trader: str,
    start: str,
    end: str,
    snapshot_type: str = Query("after_trading"),
    data: DataAccess = Depends(get_data),
) -> dict[str, Any]:
    _check_snapshot_type(snapshot_type)
    mysql = _mysql(data, source)
    rows = mysql.query(
        f"""
        SELECT trade_date, snapshot_type,
               total_asset, market_value, cash, available_cash, frozen_cash,
               nt_equity, nt_market_value, nt_balance_total, nt_balance_free,
               nt_balance_locked, nt_unrealized_pnl, nt_realized_pnl
        FROM live_asset_snapshot
        WHERE {_ACCOUNT_KEYS}
          AND snapshot_type = %(snapshot_type)s
          AND trade_date BETWEEN %(start)s AND %(end)s
        ORDER BY trade_date
        """,
        {
            "account_id": account,
            "trader_id": trader,
            "snapshot_type": snapshot_type,
            "start": start,
            "end": end,
        },
    )
    return {"rows": jsonable_rows(rows)}


@app.get("/api/returns")
def get_returns(
    source: str,
    account: str,
    trader: str,
    start: str,
    end: str,
    instrument_suffix: str | None = Query(None),
    data: DataAccess = Depends(get_data),
) -> dict[str, Any]:
    mysql = _mysql(data, source)
    try:
        rows = query_returns(
            mysql,
            account_id=account,
            trader_id=trader,
            start_date=start,
            end_date=end,
            instrument_suffix=instrument_suffix,
        )
    except Exception as exc:  # noqa: BLE001 — surface DB errors as 500 with a message
        raise HTTPException(status_code=500, detail=f"returns query failed: {exc}") from exc
    return {"columns": RETURN_COLUMNS, "rows": jsonable_rows(rows)}


# ---- ClickHouse k-line -----------------------------------------------------


def _todays_tick_bar(mysql: MySqlSource, stock_code: str, day: str) -> dict[str, Any] | None:
    """Build a daily bar for ``day`` from live_stock_tick_snapshot (MySQL).

    ClickHouse bars lag by a day; when the requested window reaches today and CH
    has no bar yet, the after-trading tick snapshot carries a full OHLC + volume
    for the current session. Returns a bar dict shaped like the CH bar_query rows.
    """
    rows = mysql.query(
        """
        SELECT `open`, high, low, last_price, volume
        FROM live_stock_tick_snapshot
        WHERE stock_code = %(stock_code)s AND trade_date = %(day)s
        ORDER BY FIELD(snapshot_type, 'after_trading', 'continuous_trading', 'before_trading'),
                 write_time DESC
        LIMIT 1
        """,
        {"stock_code": stock_code, "day": day},
    )
    if not rows:
        return None
    r = jsonable_rows(rows)[0]
    if r.get("open") is None or r.get("last_price") is None:
        return None
    return {
        "ts": day,
        "open": r["open"],
        "high": r["high"],
        "low": r["low"],
        "close": r["last_price"],
        "volume": r.get("volume"),
    }


def _supplement_today(
    bars: list[dict[str, Any]], mysql: MySqlSource, stock_code: str, end: str,
) -> list[dict[str, Any]]:
    """Append today's tick-snapshot bar when the CH series stops short of today."""
    today = date.today().isoformat()
    if end < today:
        return bars  # window doesn't reach today; nothing to supplement
    last_ts = str(bars[-1]["ts"])[:10] if bars else ""
    if last_ts >= today:
        return bars  # CH already has today's bar
    extra = _todays_tick_bar(mysql, stock_code, today)
    if extra is not None:
        bars = [*bars, extra]
    return bars


@app.get("/api/kline")
def get_kline(
    stock_code: str,
    start: str,
    end: str,
    source: str | None = Query(None),
    data: DataAccess = Depends(get_data),
) -> dict[str, Any]:
    sql = data.clickhouse.bar_query(stock_code, start, end)
    try:
        rows = data.clickhouse.query(sql)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if source:
        rows = _supplement_today(rows, _mysql(data, source), stock_code, end)
    return {"rows": rows, "stock_name": data.resolve_names([stock_code]).get(stock_code, "")}


@app.get("/api/kline_with_trades")
def get_kline_with_trades(
    source: str,
    account: str,
    trader: str,
    stock_code: str,
    start: str,
    end: str,
    data: DataAccess = Depends(get_data),
) -> dict[str, Any]:
    mysql = _mysql(data, source)
    sql = data.clickhouse.bar_query(stock_code, start, end)
    try:
        bars = data.clickhouse.query(sql)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    bars = _supplement_today(bars, mysql, stock_code, end)

    trades = mysql.query(
        f"""
        SELECT trade_date, side, price, quantity, amount, trade_time
        FROM live_trade
        WHERE {_ACCOUNT_KEYS}
          AND stock_code = %(stock_code)s
          AND trade_date BETWEEN %(start)s AND %(end)s
        ORDER BY trade_time
        """,
        {
            "account_id": account,
            "trader_id": trader,
            "stock_code": stock_code,
            "start": start,
            "end": end,
        },
    )
    return {
        "bars": bars,
        "trades": jsonable_rows(trades),
        "stock_name": data.resolve_names([stock_code]).get(stock_code, ""),
    }


# ---- comparison presets ----------------------------------------------------


@app.get("/api/compare_presets")
def get_compare_presets() -> dict[str, Any]:
    return {"presets": compare_presets.list_presets()}


@app.post("/api/compare_presets")
def save_compare_preset(body: ComparePreset) -> dict[str, Any]:
    try:
        compare_presets.save_preset(body.name, body.series)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.delete("/api/compare_presets")
def delete_compare_preset(name: str) -> dict[str, Any]:
    existed = compare_presets.delete_preset(name)
    if not existed:
        raise HTTPException(status_code=404, detail=f"no such preset: {name!r}")
    return {"ok": True}


# ---- static frontend -------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(STATIC_DIR)), name="static")
