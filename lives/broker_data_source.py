#!/usr/bin/env python3
"""
Broker data-source abstraction for the live prediction loader.

The live prediction loader reads signals/bars from ClickHouse (venue-agnostic) but
also needs an authoritative full-tick snapshot and broker position/order/trade
snapshots that come straight from the broker gateway. Those calls are the only
venue-specific bit of the loader, so they live behind :class:`BrokerDataSource`:

- :class:`QmtBrokerDataSource` reaches ``quant-qmt-proxy`` over HTTP (the original
  implementation, moved here verbatim).
- :class:`BigQmtBrokerDataSource` reaches the Big QMT RPC bridge via
  ``bigqmt_signal_trader``'s ``BigQmtXtData`` / ``BigQmtXtTrader`` client.

Both return the SAME normalized shapes keyed by Nautilus instrument id, so the
loader and the snapshot recorder stay venue-agnostic.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any
from typing import Protocol

from backtests.data_providers.clickhouse_model_predictions import normalize_stock_code


_LOGGER = logging.getLogger(__name__)


def chunks(items: list[Any], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def build_instrument_id(stock_code: str, venue: str) -> str:
    """
    Build the Nautilus instrument-id string for a stock code under ``venue``.

    Mirrors ``qmt_symbol_to_instrument_id`` / ``bigqmt_symbol_to_instrument_id``
    (``SYMBOL.VENUE``) without importing the adapter for a trivial join.
    """
    return f"{stock_code.strip().upper()}.{venue}"


# --------------------------------------------------------------------------------------
# Shared row-normalization helpers (venue-agnostic, parametrized by venue suffix)
# --------------------------------------------------------------------------------------


def coerce_tick_fields(tick: Any) -> dict[str, float]:
    if not isinstance(tick, dict):
        return {}
    coerced: dict[str, float] = {}
    for key, value in tick.items():
        try:
            coerced[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return coerced


def _first_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _with_inferred_exchange(stock_code: str | None) -> str | None:
    if not stock_code or "." in stock_code:
        return stock_code
    if len(stock_code) != 6 or not stock_code.isdigit():
        return stock_code
    if stock_code.startswith(("6", "9")):
        return f"{stock_code}.SH"
    if stock_code.startswith(("0", "2", "3")):
        return f"{stock_code}.SZ"
    if stock_code.startswith(("4", "8")):
        return f"{stock_code}.BJ"
    return stock_code


def position_stock_code(row: dict[str, Any], venue: str) -> str | None:
    suffix = f".{venue}"
    for key in ("stock_code", "instrument_id", "symbol"):
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip().upper()
        if text.endswith(suffix):
            text = text[: -len(suffix)]
        stock_code = normalize_stock_code(text)
        stock_code = _with_inferred_exchange(stock_code)
        if stock_code:
            return stock_code
    return None


def enrich_broker_rows(rows: list[dict[str, Any]], venue: str) -> list[dict[str, Any]]:
    """
    Attach a normalized ``stock_code`` and derived ``instrument_id`` to each broker
    order/trade dict so the recorder stays venue-symbol-agnostic. Rows without a
    resolvable stock code are passed through unchanged.
    """
    enriched: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stock_code = position_stock_code(row, venue)
        item = dict(row)
        if stock_code:
            item["stock_code"] = stock_code
            item["instrument_id"] = build_instrument_id(stock_code, venue)
        enriched.append(item)
    return enriched


def normalize_bigqmt_orders(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize BigQMT compatibility objects to the recorder order contract."""
    normalized = enrich_broker_rows(rows, "BIGQMT")
    for row in normalized:
        # The Nautilus BigQMT client submits ClientOrderId as order_remark. The
        # MiniQMT compatibility layer preserves it there rather than exposing the
        # recorder's canonical client_order_id field.
        if not row.get("client_order_id"):
            row["client_order_id"] = row.get("order_remark")
    return normalized


def normalize_bigqmt_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize BigQMT compatibility objects to the recorder trade contract."""
    normalized = enrich_broker_rows(rows, "BIGQMT")
    for row in normalized:
        # BigQMT calls the execution identity trade_id; QMT's normalized broker
        # contract (and SnapshotRecorder) calls it traded_id.
        if not row.get("traded_id"):
            row["traded_id"] = row.get("trade_id")
        if not row.get("client_order_id"):
            row["client_order_id"] = row.get("order_remark")
    return normalized


def normalize_broker_positions(
    rows: list[dict[str, Any]],
    venue: str,
) -> dict[str, dict[str, Any]]:
    by_instrument: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        stock_code = position_stock_code(row, venue)
        if not stock_code:
            continue
        instrument_id = build_instrument_id(stock_code, venue)
        by_instrument[instrument_id] = {
            "stock_code": stock_code,
            "volume": _first_value(row, "volume", "current_amount", "total_volume"),
            "can_use_volume": _first_value(row, "can_use_volume", "available_volume", "available_amount"),
            "avg_price": _first_value(row, "avg_price", "open_price", "cost_price"),
            "market_value": _first_value(row, "market_value"),
            "last_price": _first_value(row, "last_price", "price"),
            "raw": row,
        }
    return by_instrument


# --------------------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------------------


class BrokerDataSource(Protocol):
    venue: str

    def full_tick_snapshot(self, stock_codes: list[str]) -> dict[str, dict[str, float]]: ...

    async def broker_position_snapshot(self) -> dict[str, dict[str, Any]]: ...

    async def broker_order_snapshot(self) -> list[dict[str, Any]]: ...

    async def broker_trade_snapshot(self) -> list[dict[str, Any]]: ...


# --------------------------------------------------------------------------------------
# QMT (quant-qmt-proxy over HTTP) — moved verbatim from live_common
# --------------------------------------------------------------------------------------


class QmtBrokerDataSource:
    venue = "QMT"

    def __init__(self, args: Any) -> None:
        self.args = args

    def full_tick_snapshot(self, stock_codes: list[str]) -> dict[str, dict[str, float]]:
        """
        Authoritative full-tick snapshot per instrument id from the QMT proxy
        ``get_full_tick`` endpoint (``POST /api/v1/data/full-tick``).
        """
        if not stock_codes:
            _LOGGER.error("full-tick snapshot requested with empty stock_codes")
            return {}
        base_url = str(getattr(self.args, "base_url_http", "") or "").rstrip("/")
        if not base_url:
            _LOGGER.error("full-tick snapshot requires base_url_http")
            return {}
        api_key = getattr(self.args, "api_key", None)
        symbol_to_stock = {code.strip().upper(): code for code in stock_codes}
        by_stock: dict[str, dict[str, float]] = {}
        for chunk in chunks(sorted(symbol_to_stock), 500):
            payload = self._post_full_tick(base_url, api_key, chunk)
            for item in payload:
                symbol = str(item.get("symbol", "")).strip().upper()
                stock_code = symbol_to_stock.get(symbol)
                if not stock_code:
                    _LOGGER.warning("full-tick snapshot returned unrequested symbol: %s", symbol)
                    continue
                tick = coerce_tick_fields(item.get("tick"))
                if tick:
                    by_stock[stock_code] = tick
                else:
                    _LOGGER.error(
                        "full-tick snapshot returned unusable tick for %s: %s",
                        stock_code,
                        item.get("tick"),
                    )
        return {
            build_instrument_id(stock_code, self.venue): tick
            for stock_code, tick in by_stock.items()
        }

    async def broker_position_snapshot(self) -> dict[str, dict[str, Any]]:
        rows = await self._with_broker_session(
            lambda client, session_id: client.get_positions(session_id),
        )
        return normalize_broker_positions(rows, self.venue)

    async def broker_order_snapshot(self) -> list[dict[str, Any]]:
        rows = await self._with_broker_session(
            lambda client, session_id: client.get_orders(session_id),
        )
        return enrich_broker_rows(rows, self.venue)

    async def broker_trade_snapshot(self) -> list[dict[str, Any]]:
        rows = await self._with_broker_session(
            lambda client, session_id: client.get_trades(session_id),
        )
        return enrich_broker_rows(rows, self.venue)

    async def _with_broker_session(self, action: Any) -> list[dict[str, Any]]:
        base_url = str(getattr(self.args, "base_url_http", "") or "").rstrip("/")
        account_id = str(getattr(self.args, "account_id", "") or "").strip()
        if not base_url or not account_id:
            return []
        from nautilus_trader.adapters.qmt.http import QMTHttpClient

        client = QMTHttpClient(
            base_url=base_url,
            api_key=getattr(self.args, "api_key", None),
            timeout_secs=float(getattr(self.args, "clickhouse_timeout_secs", 10.0) or 10.0),
        )
        session_id: str | None = None
        try:
            await client.connect()
            session = await client.open_session(
                account_id,
                str(getattr(self.args, "account_type", "STOCK") or "STOCK"),
            )
            session_id = str(session["session_id"])
            return list(await action(client, session_id))
        finally:
            if session_id is not None:
                await client.close_session(session_id)
            await client.close()

    def _post_full_tick(
        self,
        base_url: str,
        api_key: str | None,
        symbols: list[str],
    ) -> list[dict[str, Any]]:
        url = f"{base_url}/api/v1/data/full-tick"
        body = json.dumps({"symbols": symbols}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = float(getattr(self.args, "clickhouse_timeout_secs", 10.0) or 10.0)
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, dict) and not payload.get("success", True):
                    raise RuntimeError(str(payload.get("message") or payload))
                data = payload.get("data", payload) if isinstance(payload, dict) else payload
                if isinstance(data, dict):
                    return list(data.get("items", []))
                return list(data or [])
            except (urllib.error.URLError, ValueError, TimeoutError, RuntimeError) as exc:
                if attempt >= max_attempts:
                    raise RuntimeError(
                        f"QMT full-tick request failed after {max_attempts} attempts: {exc}",
                    ) from exc
                _LOGGER.warning(
                    "QMT full-tick request failed (attempt %d/%d), retrying in 1s: %s",
                    attempt,
                    max_attempts,
                    exc,
                )
                time.sleep(1)

        raise RuntimeError(f"QMT full-tick request failed after {max_attempts} attempts")


# --------------------------------------------------------------------------------------
# Big QMT (xtquant_big_convert Redis RPC bridge)
# --------------------------------------------------------------------------------------

# Big QMT get_full_tick returns camelCase fields; normalize to the same snake_case
# tick keys the QMT proxy tick carries (open/last_price/high/low/last_close/...).
_BIG_QMT_TICK_FIELD_MAP = {
    "open": "open",
    "lastPrice": "last_price",
    "high": "high",
    "low": "low",
    "lastClose": "last_close",
    "amount": "amount",
    "volume": "volume",
    "pvolume": "pvolume",
}


class BigQmtBrokerDataSource:
    venue = "BIGQMT"

    def __init__(self, args: Any) -> None:
        self.args = args
        self._trader: Any = None
        self._xtdata: Any = None
        self._account: Any = None

    def _ensure_client(self) -> None:
        if self._trader is not None:
            return
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader
        from bigqmt_signal_trader.xtquant_compat import StockAccount

        account_id = str(getattr(self.args, "account_id", "") or "").strip()
        redis_config = {
            "host": getattr(self.args, "bigqmt_redis_host", "127.0.0.1"),
            "port": int(getattr(self.args, "bigqmt_redis_port", 6379) or 6379),
            "db": int(getattr(self.args, "bigqmt_redis_db", 5) or 5),
            "password": getattr(self.args, "bigqmt_redis_password", None),
            "transport": getattr(self.args, "transport", "redis"),
        }
        timeout = float(getattr(self.args, "rpc_timeout_secs", 6.0) or 6.0)
        self._trader = BigQmtXtTrader(
            account_id=account_id,
            redis_config=redis_config,
            timeout_seconds=timeout,
        )
        self._xtdata = BigQmtXtData(self._trader.client)
        self._account = StockAccount(account_id)

    def full_tick_snapshot(self, stock_codes: list[str]) -> dict[str, dict[str, float]]:
        if not stock_codes:
            _LOGGER.error("full-tick snapshot requested with empty stock_codes")
            return {}
        account_id = str(getattr(self.args, "account_id", "") or "").strip()
        if not account_id:
            _LOGGER.error("full-tick snapshot requires account_id")
            return {}
        self._ensure_client()
        symbol_to_stock = {code.strip().upper(): code for code in stock_codes}
        by_stock: dict[str, dict[str, float]] = {}
        for chunk in chunks(sorted(symbol_to_stock), 500):
            data = self._xtdata.get_full_tick(chunk) or {}
            for symbol, raw_tick in data.items():
                stock_code = symbol_to_stock.get(str(symbol).strip().upper())
                if not stock_code:
                    _LOGGER.warning("full-tick snapshot returned unrequested symbol: %s", symbol)
                    continue
                tick = self._normalize_tick(raw_tick)
                if tick:
                    by_stock[stock_code] = tick
                else:
                    _LOGGER.error(
                        "full-tick snapshot returned unusable tick for %s: %s",
                        stock_code,
                        raw_tick,
                    )
        return {
            build_instrument_id(stock_code, self.venue): tick
            for stock_code, tick in by_stock.items()
        }

    @staticmethod
    def _normalize_tick(raw_tick: Any) -> dict[str, float]:
        if not isinstance(raw_tick, dict):
            return {}
        renamed: dict[str, Any] = {}
        for src, dst in _BIG_QMT_TICK_FIELD_MAP.items():
            if src in raw_tick:
                renamed[dst] = raw_tick[src]
        # Top-of-book bid/ask from the level arrays (QMT proxy tick exposes these too).
        for side, price_key in (("bid", "bidPrice"), ("ask", "askPrice")):
            prices = raw_tick.get(price_key)
            if isinstance(prices, (list, tuple)) and prices:
                renamed[f"{side}_price"] = prices[0]
        return coerce_tick_fields(renamed)

    async def broker_position_snapshot(self) -> dict[str, dict[str, Any]]:
        rows = await self._query("query_stock_positions")
        return normalize_broker_positions(rows, self.venue)

    async def broker_order_snapshot(self) -> list[dict[str, Any]]:
        rows = await self._query("query_stock_orders")
        return normalize_bigqmt_orders(rows)

    async def broker_trade_snapshot(self) -> list[dict[str, Any]]:
        rows = await self._query("query_stock_trades")
        return normalize_bigqmt_trades(rows)

    async def _query(self, method: str) -> list[dict[str, Any]]:
        import asyncio

        account_id = str(getattr(self.args, "account_id", "") or "").strip()
        if not account_id:
            return []
        self._ensure_client()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._query_sync, method)

    def _query_sync(self, method: str) -> list[dict[str, Any]]:
        # strategy_name="" queries all orders/trades for the account (not just one
        # strategy's) — see the strategy-name trap in the Big QMT docs.
        if method == "query_stock_positions":
            items = self._trader.query_stock_positions(self._account)
        elif method == "query_stock_orders":
            items = self._trader.query_stock_orders(self._account, False, "")
        elif method == "query_stock_trades":
            items = self._trader.query_stock_trades(self._account, "")
        else:
            return []
        return [_compat_to_dict(item) for item in items or []]


def _compat_to_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    return dict(getattr(obj, "__dict__", {}))


def build_broker_data_source(args: Any) -> BrokerDataSource:
    """
    Pick the broker data source for the venue implied by ``args``.

    A ``bigqmt_redis_host`` attribute (set by the BigQMT entrypoint's parser) selects
    the Big QMT bridge; otherwise the QMT HTTP proxy source is used (default,
    backward-compatible with the existing QMT entrypoint).
    """
    if getattr(args, "bigqmt_redis_host", None) is not None:
        return BigQmtBrokerDataSource(args)
    return QmtBrokerDataSource(args)
