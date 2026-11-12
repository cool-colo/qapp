"""Broker/local order execution report with RiceQuant intraday context."""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from web.db import ClickHouseClient
from web.db import MySqlSource
from web.db import jsonable_rows
from web.db import quote_identifier
from web.db import quote_literal


STATUS_TIME_COLUMNS = (
    "initialized_at",
    "denied_at",
    "emulated_at",
    "released_at",
    "submitted_at",
    "accepted_at",
    "rejected_at",
    "canceled_at",
    "expired_at",
    "triggered_at",
    "pending_update_at",
    "pending_cancel_at",
    "partially_filled_at",
    "filled_at",
)
TERMINAL_TIME_COLUMNS = ("rejected_at", "canceled_at", "expired_at", "filled_at", "denied_at")
DEFAULT_TICK_LIMIT = 30_000
MAX_TICK_LIMIT = 100_000
SHANGHAI = ZoneInfo("Asia/Shanghai")


def ricequant_order_book_id(stock_code: str) -> str:
    code = str(stock_code).strip().upper()
    if code.endswith(".SZ"):
        return f"{code[:-3]}.XSHE"
    if code.endswith(".SH"):
        return f"{code[:-3]}.XSHG"
    raise ValueError(f"unsupported RiceQuant stock code: {stock_code!r}")


def parse_report_window(
    trading_date: str,
    start: str | None,
    end: str | None,
) -> tuple[datetime, datetime]:
    try:
        day = date.fromisoformat(trading_date)
    except ValueError as exc:
        raise ValueError("trade_date must use YYYY-MM-DD") from exc

    def parse(value: str | None, default: time) -> datetime:
        if not value:
            return datetime.combine(day, default)
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"invalid ISO timestamp: {value!r}") from exc
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(SHANGHAI).replace(tzinfo=None)
        if parsed.date() != day:
            raise ValueError("start/end must fall on trade_date")
        return parsed

    start_at = parse(start, time(9, 15))
    end_at = parse(end, time(15, 5))
    if end_at <= start_at:
        raise ValueError("end must be later than start")
    return start_at, end_at


def minute_query(order_book_id: str, day: str, start: datetime, end: datetime) -> str:
    return f"""
SELECT
    toString(datetime) AS ts,
    open, high, low, close, volume, total_turnover, num_trades
FROM {quote_identifier("ricequant_stock_minute")}
WHERE trading_date = toDate({quote_literal(day)})
  AND order_book_id = {quote_literal(order_book_id)}
  AND datetime >= toDateTime64({quote_literal(start.isoformat(sep=' '))}, 0, 'Asia/Shanghai')
  AND datetime <= toDateTime64({quote_literal(end.isoformat(sep=' '))}, 0, 'Asia/Shanghai')
ORDER BY datetime
"""


def tick_query(
    order_book_id: str,
    day: str,
    start: datetime,
    end: datetime,
    limit: int,
) -> str:
    return f"""
SELECT
    toString(datetime) AS ts,
    tick_sequence,
    last, volume, total_turnover, num_trades,
    limit_up, limit_down,
    a1, a1_v, a2, a2_v, a3, a3_v, a4, a4_v, a5, a5_v,
    b1, b1_v, b2, b2_v, b3, b3_v, b4, b4_v, b5, b5_v
FROM {quote_identifier("ricequant_stock_tick")}
WHERE trading_date = toDate({quote_literal(day)})
  AND order_book_id = {quote_literal(order_book_id)}
  AND datetime >= toDateTime64({quote_literal(start.isoformat(sep=' '))}, 9, 'Asia/Shanghai')
  AND datetime <= toDateTime64({quote_literal(end.isoformat(sep=' '))}, 9, 'Asia/Shanghai')
ORDER BY datetime, tick_sequence
LIMIT {limit + 1:d}
"""


def query_order_execution(
    mysql: MySqlSource,
    clickhouse: ClickHouseClient,
    *,
    account: str,
    trader: str,
    stock_code: str,
    trading_date: str,
    start: str | None = None,
    end: str | None = None,
    tick_limit: int = DEFAULT_TICK_LIMIT,
) -> dict[str, Any]:
    if tick_limit < 1 or tick_limit > MAX_TICK_LIMIT:
        raise ValueError(f"tick_limit must be between 1 and {MAX_TICK_LIMIT}")
    rq_symbol = ricequant_order_book_id(stock_code)
    params = {
        "account_id": account,
        "trader_id": trader,
        "trade_date": trading_date,
        "stock_code": stock_code,
    }
    identity = (
        "account_id = %(account_id)s AND trader_id = %(trader_id)s "
        "AND trade_date = %(trade_date)s AND stock_code = %(stock_code)s"
    )
    broker_orders = mysql.query(
        f"""
        SELECT trade_date, client_order_id, venue_order_id, stock_code, side, order_type,
               limit_price, quantity, filled_qty, avg_fill_price, status, reason,
               order_time, write_time, updated_at
        FROM live_order WHERE {identity}
        ORDER BY order_time, write_time, client_order_id
        """,
        params,
    )
    broker_trades = mysql.query(
        f"""
        SELECT trade_date, trade_id, client_order_id, venue_order_id, stock_code, side,
               price, quantity, amount, commission, trade_time, write_time
        FROM live_trade WHERE {identity}
        ORDER BY trade_time, write_time, trade_id
        """,
        params,
    )
    status_columns = ", ".join(STATUS_TIME_COLUMNS)
    local_orders = mysql.query(
        f"""
        SELECT trade_date, client_order_id, venue_order_id, stock_code, side, order_type,
               limit_price, quantity, filled_qty, avg_fill_price, status, reason,
               order_time, write_time, updated_at, {status_columns}
        FROM live_order_realtime WHERE {identity}
        ORDER BY COALESCE(submitted_at, initialized_at, write_time), client_order_id
        """,
        params,
    )
    local_trades = mysql.query(
        f"""
        SELECT trade_date, trade_id, client_order_id, venue_order_id, stock_code, side,
               price, quantity, amount, commission, trade_time, write_time
        FROM live_trade_realtime WHERE {identity}
        ORDER BY trade_time, write_time, trade_id
        """,
        params,
    )
    events = mysql.query(
        f"""
        SELECT trade_date, event_id, event_type, client_order_id, venue_order_id,
               stock_code, side, order_type, limit_price, quantity, filled_qty,
               avg_fill_price, status, trade_id, last_fill_price, last_fill_qty,
               broker_event_time, CAST(broker_event_ns AS CHAR) AS broker_event_ns,
               broker_time_kind, local_event_time,
               CAST(local_event_ns AS CHAR) AS local_event_ns,
               local_received_time, persisted_at, reconciliation, signal_date,
               target_qty, target_version, reason, book_snapshot, raw_info
        FROM live_order_event WHERE {identity}
        ORDER BY local_received_time, local_event_ns, event_id
        """,
        params,
    )
    decisions = mysql.query(
        f"""
        SELECT trade_date, decision_id, parent_decision_id, intent_id,
               order_chain_id, client_order_id, trigger_client_order_id,
               decision_type, decision_status, reason_code, instrument_id,
               stock_code, side, signal_date, target_version, target_qty,
               current_qty, delta_qty, order_qty, order_price, policy_name,
               context_version, context, decision_time,
               CAST(decision_ns AS CHAR) AS decision_ns, persisted_at
        FROM live_order_decision WHERE {identity}
        ORDER BY decision_time, decision_ns, decision_id
        """,
        params,
    )

    if start is None and end is None:
        start_at, end_at = _automatic_window(
            trading_date,
            broker_orders,
            broker_trades,
            local_orders,
            events,
            decisions,
        )
    else:
        start_at, end_at = parse_report_window(trading_date, start, end)

    minutes = clickhouse.query(minute_query(rq_symbol, trading_date, start_at, end_at))
    ticks = clickhouse.query(tick_query(rq_symbol, trading_date, start_at, end_at, tick_limit))
    truncated = len(ticks) > tick_limit
    if truncated:
        ticks = ticks[:tick_limit]
    diagnostics = build_diagnostics(
        broker_orders,
        broker_trades,
        local_orders,
        ticks,
        start_at,
        end_at,
        complete_ticks=not truncated,
    )
    return {
        "stock_code": stock_code,
        "ricequant_order_book_id": rq_symbol,
        "trade_date": trading_date,
        "range": {"start": start_at.isoformat(), "end": end_at.isoformat()},
        "broker": {
            "orders": jsonable_rows(broker_orders),
            "trades": jsonable_rows(broker_trades),
        },
        "local": {
            "orders": jsonable_rows(local_orders),
            "trades": jsonable_rows(local_trades),
            "events": jsonable_rows(events),
            "decisions": jsonable_rows(decisions),
        },
        "market": {"minutes": minutes, "ticks": ticks},
        "diagnostics": diagnostics,
        "coverage": {
            "tick_limit": tick_limit,
            "returned_ticks": len(ticks),
            "ticks_truncated": truncated,
            "diagnostics_complete": not truncated,
        },
    }


def _automatic_window(
    trading_date: str,
    broker_orders: list[dict[str, Any]],
    broker_trades: list[dict[str, Any]],
    local_orders: list[dict[str, Any]],
    events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> tuple[datetime, datetime]:
    session_start, session_end = parse_report_window(trading_date, None, None)
    values: list[datetime] = []
    for row in broker_orders:
        value = _as_datetime(row.get("order_time")) or _as_datetime(row.get("write_time"))
        if value is not None:
            values.append(value)
    for row in broker_trades:
        value = _as_datetime(row.get("trade_time")) or _as_datetime(row.get("write_time"))
        if value is not None:
            values.append(value)
    for row in local_orders:
        for column in (*STATUS_TIME_COLUMNS, "order_time", "write_time"):
            value = _as_datetime(row.get(column))
            if value is not None:
                values.append(value)
    for row in events:
        value = _as_datetime(row.get("local_received_time")) or _as_datetime(
            row.get("local_event_time"),
        )
        if value is not None:
            values.append(value)
    for row in decisions:
        value = _as_datetime(row.get("decision_time"))
        if value is not None:
            values.append(value)
    if not values:
        return session_start, session_end
    earliest = max(session_start, min(values) - timedelta(minutes=5))
    latest = min(
        session_end,
        max(max(values) + timedelta(minutes=5), earliest + timedelta(minutes=30)),
    )
    return earliest, latest


def build_diagnostics(
    broker_orders: list[dict[str, Any]],
    broker_trades: list[dict[str, Any]],
    local_orders: list[dict[str, Any]],
    ticks: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
    *,
    complete_ticks: bool,
) -> list[dict[str, Any]]:
    local_by_id = {str(row["client_order_id"]): row for row in local_orders}
    trades_by_order: dict[str, list[dict[str, Any]]] = {}
    for trade in broker_trades:
        trades_by_order.setdefault(str(trade["client_order_id"]), []).append(trade)
    parsed_ticks = [(_as_datetime(row.get("ts")), row) for row in ticks]
    result: list[dict[str, Any]] = []
    for broker in broker_orders:
        client_order_id = str(broker["client_order_id"])
        local = local_by_id.get(client_order_id)
        start_at, interval_source = _active_start(broker, local, window_start)
        end_at = _active_end(local, window_end)
        active = [row for ts, row in parsed_ticks if ts is not None and start_at <= ts <= end_at]
        limit_price = _as_float(broker.get("limit_price"))
        side = str(broker.get("side") or "").lower()
        marketable = []
        crossings = []
        for tick in active:
            last = _as_float(tick.get("last"))
            ask = _as_float(tick.get("a1"))
            bid = _as_float(tick.get("b1"))
            if limit_price is not None and side == "buy":
                marketable.append(ask is not None and ask <= limit_price)
                crossings.append(last is not None and last <= limit_price)
            elif limit_price is not None and side == "sell":
                marketable.append(bid is not None and bid >= limit_price)
                crossings.append(last is not None and last >= limit_price)
        first_marketable = next(
            (
                str(tick.get("ts"))
                for tick, is_marketable in zip(active, marketable, strict=True)
                if is_marketable
            ),
            None,
        )
        first_tick = active[0] if active else {}
        start_ask = _as_float(first_tick.get("a1"))
        start_bid = _as_float(first_tick.get("b1"))
        start_gap = None
        if limit_price is not None and side == "buy" and start_ask is not None:
            start_gap = limit_price - start_ask
        elif limit_price is not None and side == "sell" and start_bid is not None:
            start_gap = start_bid - limit_price
        midpoint = (
            (start_bid + start_ask) / 2
            if start_bid is not None and start_ask is not None
            else None
        )
        acceptance_latency_ms = _acceptance_latency_ms(local)
        last_prices = [value for tick in active if (value := _as_float(tick.get("last"))) is not None]
        quantity = _as_float(broker.get("quantity")) or 0.0
        filled_qty = _as_float(broker.get("filled_qty")) or 0.0
        broker_fills = trades_by_order.get(client_order_id, [])
        broker_trade_qty = sum(_as_float(trade.get("quantity")) or 0.0 for trade in broker_fills)
        discrepancies = _order_discrepancies(broker, local)
        if broker_trade_qty != filled_qty:
            discrepancies.append("broker_trade_qty_vs_order_filled_qty")
        result.append(
            {
                "client_order_id": client_order_id,
                "side": side,
                "status": broker.get("status"),
                "limit_price": limit_price,
                "quantity": quantity,
                "filled_qty": filled_qty,
                "broker_trade_count": len(broker_fills),
                "broker_trade_qty": broker_trade_qty,
                "fill_ratio": (filled_qty / quantity if quantity > 0 else None),
                "active_start": start_at.isoformat(),
                "active_end": end_at.isoformat(),
                "interval_source": interval_source,
                "active_tick_count": len(active),
                "marketable_tick_count": sum(marketable),
                "last_crossing_tick_count": sum(crossings),
                "first_marketable_time": first_marketable,
                "best_bid_at_start": start_bid,
                "best_ask_at_start": start_ask,
                "marketability_gap_at_start": start_gap,
                "marketability_gap_bps_at_start": (
                    start_gap / midpoint * 10_000 if start_gap is not None and midpoint else None
                ),
                "submission_to_acceptance_ms": acceptance_latency_ms,
                "min_last": min(last_prices) if last_prices else None,
                "max_last": max(last_prices) if last_prices else None,
                "tick_coverage_complete": complete_ticks,
                "discrepancies": discrepancies,
                "assessment": _assessment(
                    broker,
                    sum(marketable),
                    filled_qty,
                    complete_ticks,
                ),
            },
        )
    return result


def _active_start(
    broker: dict[str, Any],
    local: dict[str, Any] | None,
    default: datetime,
) -> tuple[datetime, str]:
    if local:
        for column in ("accepted_at", "submitted_at", "initialized_at"):
            value = _as_datetime(local.get(column))
            if value is not None:
                return max(default, value), f"local.{column}"
    broker_time = _as_datetime(broker.get("order_time"))
    if broker_time is not None:
        return max(default, broker_time), "broker.order_time"
    return default, "requested_window"


def _active_end(local: dict[str, Any] | None, default: datetime) -> datetime:
    if local:
        values = [_as_datetime(local.get(column)) for column in TERMINAL_TIME_COLUMNS]
        terminal = [value for value in values if value is not None]
        if terminal:
            return min(default, min(terminal))
    return default


def _order_discrepancies(
    broker: dict[str, Any],
    local: dict[str, Any] | None,
) -> list[str]:
    if local is None:
        return ["missing_local_order"]
    mismatches = []
    for column in ("status", "limit_price", "quantity", "filled_qty", "avg_fill_price"):
        if _comparable(broker.get(column)) != _comparable(local.get(column)):
            mismatches.append(column)
    return mismatches


def _acceptance_latency_ms(local: dict[str, Any] | None) -> float | None:
    if not local:
        return None
    accepted = _as_datetime(local.get("accepted_at"))
    submitted = _as_datetime(local.get("submitted_at")) or _as_datetime(
        local.get("initialized_at"),
    )
    if accepted is None or submitted is None or accepted < submitted:
        return None
    return (accepted - submitted).total_seconds() * 1000


def _assessment(
    broker: dict[str, Any],
    marketable_count: int,
    filled_qty: float,
    complete_ticks: bool,
) -> str:
    status = str(broker.get("status") or "").lower()
    if status in {"rejected", "denied"}:
        return "rejected_before_execution"
    if filled_qty > 0:
        return "partially_or_fully_filled"
    if not complete_ticks:
        return "incomplete_tick_coverage"
    if marketable_count == 0:
        return "price_never_marketable"
    return "marketable_without_fill_review_queue_or_latency"


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(SHANGHAI).replace(tzinfo=None)
    return parsed


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _comparable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, float):
        return f"{value:.8g}"
    return None if value is None else str(value).lower()
