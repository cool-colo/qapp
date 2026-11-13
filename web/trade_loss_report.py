"""Minute execution-loss estimates for the fixed before-trading rebalance book."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from web.db import ClickHouseClient, MySqlSource, quote_literal, ricequant_order_book_id


def _quantity(value: Any, label: str) -> int:
    try:
        quantity = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    if not quantity.is_finite() or quantity < 0 or quantity != quantity.to_integral_value():
        raise ValueError(f"invalid {label}: {value!r}")
    return int(quantity)


def build_preopen_trade_book(
    targets: list[dict[str, Any]],
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Include exits absent from the target portfolio, with target quantity zero."""
    target_qty: dict[str, int] = {}
    position_qty: dict[str, int] = {}
    for rows, field, book in (
        (targets, "target_qty", target_qty),
        (positions, "volume", position_qty),
    ):
        for row in rows:
            code = str(row["stock_code"])
            if code in book:
                raise ValueError(f"duplicate before-trading {field} for {code}")
            book[code] = _quantity(row[field], f"{code} {field}")

    trades = []
    for code in sorted(target_qty.keys() | position_qty.keys()):
        held = position_qty.get(code, 0)
        target = target_qty.get(code, 0)
        delta = target - held
        if delta:
            trades.append({
                "stock_code": code,
                "side": "BUY" if delta > 0 else "SELL",
                "preopen_qty": held,
                "target_qty": target,
                "trade_qty": abs(delta),
            })
    return trades


def minute_loss_query(stock_codes: list[str], trading_date: str) -> str:
    day = date.fromisoformat(trading_date).isoformat()
    symbols = ", ".join(quote_literal(ricequant_order_book_id(code)) for code in stock_codes)
    # Take the entire latest observation, including NULLs, from this append-only
    # stream. Separate argMax(open) / argMax(close) would mix observation versions.
    return f"""
    SELECT order_book_id, toString(datetime) AS ts,
           tupleElement(latest, 1) AS open,
           tupleElement(latest, 2) AS close
    FROM (
        SELECT order_book_id, datetime,
               argMax(tuple(open, close), tuple(received_at, event_id)) AS latest
        FROM ricequant_stock_minute_realtime
        WHERE trade_date = toDate({quote_literal(day)})
          AND order_book_id IN ({symbols})
          AND (
              datetime BETWEEN toDateTime64('{day} 09:30:00', 3, 'Asia/Shanghai')
                           AND toDateTime64('{day} 11:30:00', 3, 'Asia/Shanghai')
              OR datetime BETWEEN toDateTime64('{day} 13:00:00', 3, 'Asia/Shanghai')
                              AND toDateTime64('{day} 15:00:00', 3, 'Asia/Shanghai')
          )
          AND datetime <= now64(3, 'Asia/Shanghai')
          AND received_at <= now64(6, 'Asia/Shanghai')
        GROUP BY order_book_id, datetime
    )
    ORDER BY datetime, order_book_id
    """


def _price(value: Any) -> Decimal | None:
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        return None
    return price if price.is_finite() and price > 0 else None


def build_minute_trade_loss(
    trades: list[dict[str, Any]],
    minutes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Use a constant gross opening notional; incomplete minutes remain gaps."""
    symbols = {ricequant_order_book_id(row["stock_code"]): row for row in trades}
    bars: dict[str, dict[str, dict[str, Any]]] = {symbol: {} for symbol in symbols}
    for row in minutes:
        symbol = str(row["order_book_id"])
        if symbol not in bars:
            raise ValueError(f"unexpected minute symbol: {symbol}")
        ts = datetime.fromisoformat(str(row["ts"])).isoformat(sep=" ", timespec="milliseconds")
        if ts in bars[symbol]:
            raise ValueError(f"duplicate minute for {symbol} at {ts}")
        bars[symbol][ts] = row

    opening_prices: dict[str, Decimal] = {}
    book = []
    for symbol, trade in symbols.items():
        opening = None
        if bars[symbol]:
            first = min(bars[symbol])
            # A later minute's open is not the day's opening price. Accept the
            # opening bar's source label (09:30 or 09:31), without moving it.
            if datetime.fromisoformat(first).time() <= time(9, 31):
                opening = _price(bars[symbol][first]["open"])
        if opening is not None:
            opening_prices[symbol] = opening
        book.append({**trade, "open_price": float(opening) if opening is not None else None})

    missing_open = [row["stock_code"] for row in book if row["open_price"] is None]
    missing_minutes = [trade["stock_code"] for symbol, trade in symbols.items() if not bars[symbol]]
    opening_notional = sum(
        (opening_prices[symbol] * trade["trade_qty"]
         for symbol, trade in symbols.items() if symbol in opening_prices),
        Decimal(0),
    ) if not missing_open else None
    stock_series = {}
    for symbol, trade in symbols.items():
        opening = opening_prices.get(symbol)
        amount = opening * trade["trade_qty"] if opening is not None else None
        weight = amount / opening_notional if amount is not None and opening_notional else None
        stock_series[symbol] = {
            **trade,
            "open_price": float(opening) if opening is not None else None,
            "open_amount": float(amount) if amount is not None else None,
            "weight": float(weight) if weight is not None else None,
            "points": [],
        }
    timestamps = sorted({ts for rows in bars.values() for ts in rows})
    timeline = []
    if timestamps:
        cursor = datetime.fromisoformat(timestamps[0])
        end = datetime.fromisoformat(timestamps[-1])
        while cursor <= end:
            if time(9, 30) <= cursor.time() <= time(11, 30) or time(13) <= cursor.time() <= time(15):
                timeline.append(cursor.isoformat(sep=" ", timespec="milliseconds"))
            cursor += timedelta(minutes=1)
    points = []
    for ts in timeline:
        buy_loss = Decimal(0)
        sell_loss = Decimal(0)
        priced_count = 0
        for symbol, trade in symbols.items():
            bar = bars[symbol].get(ts)
            close = _price(bar["close"]) if bar else None
            opening = opening_prices.get(symbol)
            priced = close is not None and opening is not None
            signed_loss = (close - opening) * trade["trade_qty"] if priced else None
            if signed_loss is not None and trade["side"] == "SELL":
                signed_loss = -signed_loss
            stock_series[symbol]["points"].append({
                "ts": ts,
                "close": float(close) if close is not None else None,
                "price_change_bps": float((close / opening - 1) * 10_000) if priced else None,
                "weighted_loss_bps": (
                    float(signed_loss / opening_notional * 10_000)
                    if signed_loss is not None and opening_notional else None
                ),
                "loss_amount": float(signed_loss) if signed_loss is not None else None,
            })
            if close is None or opening is None:
                continue
            loss = (close - opening) * trade["trade_qty"]
            if trade["side"] == "BUY":
                buy_loss += loss
            else:
                sell_loss -= loss
            priced_count += 1
        complete = priced_count == len(trades) and bool(opening_notional)
        loss = buy_loss + sell_loss
        points.append({
            "ts": ts,
            "priced_count": priced_count,
            "loss_amount": float(loss) if complete else None,
            "buy_loss_amount": float(buy_loss) if complete else None,
            "sell_loss_amount": float(sell_loss) if complete else None,
            "loss_bps": float(loss / opening_notional * 10_000) if complete else None,
        })
    return {
        "trades": book,
        "stock_count": len(book),
        "buy_count": sum(row["side"] == "BUY" for row in book),
        "sell_count": sum(row["side"] == "SELL" for row in book),
        "open_amount": float(opening_notional) if opening_notional is not None else None,
        "missing_open_stock_codes": missing_open,
        "missing_minute_stock_codes": missing_minutes,
        "points": points,
        "stock_series": list(stock_series.values()),
    }


def query_minute_trade_loss(
    mysql: MySqlSource,
    clickhouse: ClickHouseClient,
    *,
    account: str,
    trader: str,
    trading_date: str,
) -> dict[str, Any]:
    day = date.fromisoformat(trading_date).isoformat()
    params = {"account_id": account, "trader_id": trader, "trade_date": day}
    identity = """
        account_id = %(account_id)s AND trader_id = %(trader_id)s
        AND trade_date = %(trade_date)s AND snapshot_type = 'before_trading'
        AND status = 'ok'
    """
    targets = mysql.query(
        f"SELECT stock_code, target_qty, target_version FROM live_target_portfolio WHERE {identity}",
        params,
    )
    result: dict[str, Any] = {
        "trade_date": day,
        "status": "missing_preopen_target",
        "stock_count": 0,
        "trades": [],
        "points": [],
    }
    if not targets:
        return result
    positions = mysql.query(
        f"SELECT stock_code, volume FROM live_position_snapshot WHERE {identity}",
        params,
    )
    if not positions:
        # An empty position table alone cannot distinguish a flat account from a
        # missing snapshot. Confirm the empty portfolio using its preopen asset.
        assets = mysql.query(
            f"SELECT market_value FROM live_asset_snapshot WHERE {identity}", params,
        )
        if not assets or assets[0]["market_value"] is None or Decimal(str(assets[0]["market_value"])) != 0:
            result["status"] = "missing_preopen_positions"
            return result
    trades = build_preopen_trade_book(targets, positions)
    minutes = clickhouse.query(minute_loss_query(
        [row["stock_code"] for row in trades], day,
    )) if trades else []
    result.update(build_minute_trade_loss(trades, minutes))
    result["target_versions"] = sorted({row["target_version"] for row in targets if row["target_version"]})
    result["status"] = "ok" if trades else "no_trades"
    return result
