"""
In-process HTTP control server for the live trading node.

Runs a stdlib :mod:`http.server` on its own daemon thread alongside the live
``TradingNode`` (like :mod:`lives.status_server`), holding direct handles to the
node, the running strategy, and the MySQL control writer. It exposes:

- a **real-time positions** view (资金账号/证券/持仓/盈亏 columns) assembled entirely
  from the Nautilus ``Cache``/``Portfolio`` plus the strategy's in-memory price
  state — never a direct QMT proxy poll, honoring the repo convention that
  business reads go through Nautilus first; and
- **trading control** — suspend/resume and manual sell (all-sellable or a single
  stock) — driven through ``strategy.trading_controller``.

Thread model: this server runs entirely on its own daemon thread and calls the
strategy/cache **directly**. It does NOT capture ``node.kernel.exec_engine._loop``
and never marshals onto the Nautilus event loop. Reads are lock-free best-effort
snapshots (GIL-safe per field; a monitor tolerates a momentary cross-field skew).
Mutations go through the ``TradingController``, which serializes against the
trading thread's convergence via the strategy's ``_converge_lock``.

Write endpoints require a shared-token ``X-Control-Token`` header. If no token is
configured, all writes are rejected (fail-safe). Reads are open.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId

from strategies.trading_control import SELL_ALL_REASON
from strategies.trading_control import SELL_REASON


@dataclass
class ControlServerConfig:
    """Config for :class:`LiveControlServer`."""

    port: int = 9300
    addr: str = "0.0.0.0"
    token: str | None = None


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        if value is None:
            return None
        return Decimal(str(value))
    except (TypeError, ValueError):
        return None


def _money_or_none(money: Any) -> float | None:
    """Nautilus ``Money`` (or None) → float via ``as_decimal`` (see ``_sum_money``)."""
    if money is None:
        return None
    try:
        return float(money.as_decimal())
    except (TypeError, ValueError, AttributeError):
        return None


class LiveControlServer:
    """Serves real-time positions + trading control over HTTP on a daemon thread.

    Parameters
    ----------
    node : TradingNode
        The live node; ``node.cache`` / ``node.portfolio`` are read directly.
    strategy_ref : Any
        The running strategy (its ``trading_controller`` and in-memory price dicts).
    control_writer : Any, optional
        ``LiveControlWriter`` for persisting the pause flag + audit log. May be None
        (MySQL unavailable); the server then degrades to in-memory state.
    account_id, trader_id : str
        Identify this account for persistence and the 资金账号 column.
    config : ControlServerConfig, optional
        Bind address / port / write token.
    """

    def __init__(
        self,
        node: Any,
        strategy_ref: Any,
        control_writer: Any = None,
        account_id: str = "",
        trader_id: str = "",
        config: ControlServerConfig | None = None,
    ) -> None:
        self._node = node
        self._strategy = strategy_ref
        self._control_writer = control_writer
        self._account_id = str(account_id)
        self._trader_id = str(trader_id)
        self._config = config or ControlServerConfig()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._httpd is not None:
            return
        handler = self._make_handler()

        class _Server(ThreadingHTTPServer):
            # Reuse a socket left in TIME_WAIT by a just-restarted node so a quick
            # restart does not spuriously fail to bind. (A genuinely occupied port
            # still raises OSError, which the builder treats as best-effort.)
            allow_reuse_address = True
            daemon_threads = True

        self._httpd = _Server((self._config.addr, self._config.port), handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="qapp-live-control-server",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Shut the server down and close the writer. Safe to call more than once."""
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)
        writer, self._control_writer = self._control_writer, None
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass

    @property
    def port(self) -> int:
        return self._config.port

    # ---- Nautilus access -----------------------------------------------------

    @property
    def _cache(self) -> Any:
        return self._node.cache

    def _open_long_positions(self) -> list[Any]:
        cache = self._cache
        try:
            positions = cache.positions_open()
        except Exception:
            positions = []
        result = []
        for position in positions:
            try:
                if position.is_long and Decimal(str(position.quantity)) > 0:
                    result.append(position)
            except Exception:
                continue
        return result

    def _closed_today_long_positions(self, open_iids: set[str]) -> list[Any]:
        # Names sold out today: closed long positions whose ts_closed falls on the
        # strategy's current trading date. Skip any instrument still open (its open
        # row is authoritative). Mirrors the broker screen, which keeps sold-out
        # rows for the session with their realized result.
        cache = self._cache
        try:
            positions = cache.positions_closed()
        except Exception:
            positions = []
        today = self._strategy.control_clock_date()
        result = []
        for position in positions:
            try:
                # A-shares are long-only; `entry` is unreliable here because the
                # QMT adapter reconciles from broker snapshots and often records the
                # sell as the entry order. Use peak_qty > 0 to confirm shares were
                # actually held (a real long that has since been sold out).
                if Decimal(str(position.peak_qty)) <= 0:
                    continue
                if not position.ts_closed:  # zero unless closed
                    continue
                if self._strategy.control_event_date(int(position.ts_closed)) != today:
                    continue
                if str(position.instrument_id) in open_iids:
                    continue
                result.append(position)
            except Exception:
                continue
        return result

    def _stock_code(self, instrument_id_text: str) -> str:
        mapping = getattr(self._strategy, "_stock_by_instrument", {})
        code = mapping.get(instrument_id_text) if isinstance(mapping, dict) else None
        if code:
            return str(code)
        try:
            return InstrumentId.from_str(instrument_id_text).symbol.value.upper()
        except (ValueError, TypeError):
            head, _, _ = instrument_id_text.strip().rpartition(".")
            return (head or instrument_id_text).upper()

    def _instrument_name(self, instrument_id_text: str) -> str | None:
        try:
            instrument = self._cache.instrument(InstrumentId.from_str(instrument_id_text))
        except (ValueError, TypeError):
            instrument = None
        if instrument is not None:
            info = getattr(instrument, "info", None)
            if isinstance(info, dict):
                name = info.get("name")
                if name:
                    return str(name)
        return None

    def _last_price(self, instrument_id_text: str) -> float | None:
        # Prefer the live full-tick last_price (the current traded price). The
        # strategy's `_last_close` dict is a fallback that, for full-tick-priced
        # names, actually holds the *previous* session close (see
        # target_quantities.py:603) — using it as the "latest" price would make
        # 当日涨幅/当日盈亏 collapse to zero (last_price == prev_close).
        snapshot = self._strategy._market_status.get(instrument_id_text)
        if snapshot is not None:
            live = _float_or_none(snapshot.last_price)
            if live is not None and live > 0:
                return live
        return _float_or_none(self._strategy._last_close.get(instrument_id_text))

    def _prev_close(self, instrument_id_text: str) -> float | None:
        snapshot = self._strategy._market_status.get(instrument_id_text)
        if snapshot is not None:
            return _float_or_none(snapshot.last_close)
        return None

    def _sellable(self, instrument_id_text: str) -> Decimal | None:
        try:
            return self._strategy.venue_sellable_quantity(instrument_id_text)
        except Exception:
            return None

    def _frozen_by_pending_sell(self, instrument_id_text: str) -> Decimal:
        """Shares locked in working SELL orders (i.e. currently being sold)."""
        try:
            orders = self._cache.orders_open(
                instrument_id=InstrumentId.from_str(instrument_id_text),
                side=OrderSide.SELL,
            )
        except Exception:
            return Decimal(0)
        total = Decimal(0)
        for order in orders:
            leaves = _decimal_or_none(order.leaves_qty)
            if leaves is not None and leaves > 0:
                total += leaves
        return total

    # ---- payload assembly ----------------------------------------------------

    def _positions_payload(self) -> list[dict]:
        rows: list[dict] = []
        open_iids: set[str] = set()
        for position in self._open_long_positions():
            try:
                iid_text = str(position.instrument_id)
            except Exception:
                continue
            open_iids.add(iid_text)
            rows.append(self._open_row(position, iid_text))
        # Names sold out today (current volume 0) — appended after live holdings.
        for position in self._closed_today_long_positions(open_iids):
            try:
                iid_text = str(position.instrument_id)
            except Exception:
                continue
            rows.append(self._closed_row(position, iid_text))
        return rows

    def _closed_debug_payload(self) -> dict:
        """Diagnostic: what does the cache hold for closed positions today?"""
        cache = self._cache
        try:
            positions = cache.positions_closed()
        except Exception as exc:
            return {"error": repr(exc), "closed_count": 0, "positions": []}
        today = str(self._strategy.control_clock_date())
        out = []
        for position in positions:
            try:
                ts_closed = int(position.ts_closed)
                event_date = self._strategy.control_event_date(ts_closed)
                out.append(
                    {
                        "instrument_id": str(position.instrument_id),
                        "entry": str(position.entry),
                        "ts_closed": ts_closed,
                        "closed_date": None if event_date is None else str(event_date),
                        "peak_qty": str(position.peak_qty),
                        "avg_px_open": _float_or_none(position.avg_px_open),
                        "avg_px_close": _float_or_none(position.avg_px_close),
                        "realized_pnl": _money_or_none(position.realized_pnl),
                    }
                )
            except Exception as exc:
                out.append({"error": repr(exc)})
        return {"today": today, "closed_count": len(positions), "positions": out}

    def _base_row(self, iid_text: str) -> dict:
        return {
            "account": self._account_id,  # 资金账号
            "stock_code": self._stock_code(iid_text),  # 证券代码
            "name": self._instrument_name(iid_text),  # 证券名称
            "instrument_id": iid_text,
        }

    def _open_row(self, position: Any, iid_text: str) -> dict:
        volume = _decimal_or_none(getattr(position, "quantity", None)) or Decimal(0)
        avg_price = _float_or_none(getattr(position, "avg_px_open", None))
        can_use = self._sellable(iid_text)
        # 冻结数量: shares locked in pending SELL orders (currently in selling),
        # not the T+1 unsettled portion. Derive from working sell orders.
        frozen = int(self._frozen_by_pending_sell(iid_text))
        last_price = self._last_price(iid_text)
        prev_close = self._prev_close(iid_text)
        volume_f = float(volume)

        market_value = None if last_price is None else volume_f * last_price
        position_cost = None if avg_price is None else volume_f * avg_price

        unrealized_pnl = None
        pnl_ratio = None
        if last_price is not None and avg_price is not None:
            unrealized_pnl = (last_price - avg_price) * volume_f
            if avg_price > 0:
                pnl_ratio = (last_price - avg_price) / avg_price

        day_change = None
        day_pnl = None
        if last_price is not None and prev_close is not None and prev_close > 0:
            day_change = (last_price - prev_close) / prev_close
            day_pnl = (last_price - prev_close) * volume_f

        row = self._base_row(iid_text)
        row.update(
            {
                "volume": int(volume),  # 当前拥股
                "can_use_volume": None if can_use is None else int(can_use),  # 可用数量
                "frozen": frozen,  # 冻结数量
                "avg_price": avg_price,  # 成本价
                "last_price": last_price,  # 最新价
                "unrealized_pnl": unrealized_pnl,  # 持仓盈亏
                "pnl_ratio": pnl_ratio,  # 盈亏比例
                "day_change": day_change,  # 当日涨幅
                "day_pnl": day_pnl,  # 当日盈亏
                "market_value": market_value,  # 市值
                "position_cost": position_cost,  # 持仓成本
                "closed_today": False,
            }
        )
        return row

    def _closed_row(self, position: Any, iid_text: str) -> dict:
        # Sold out today: nothing held now, so 当前拥股/可用/市值/持仓成本 are 0.
        # 最新价 shows the average close (清仓均价 — what it went out at).
        # NOTE: under QMT snapshot reconciliation avg_px_open == avg_px_close and
        # realized_pnl is basically just fees, so the position's own realized figure
        # is NOT the day's result. We show "-" for 盈亏比例, and compute 当日盈亏 as
        # (清仓均价 − 昨收) × 卖出股数, which is the meaningful day move on the shares.
        avg_price = _float_or_none(position.avg_px_open)  # 成本价
        avg_close = _float_or_none(position.avg_px_close)  # 最新价 (清仓均价)
        prev_close = self._prev_close(iid_text)
        sold_qty = _decimal_or_none(position.peak_qty) or Decimal(0)

        day_change = None
        day_pnl = None
        if avg_close is not None and prev_close is not None and prev_close > 0:
            day_change = (avg_close - prev_close) / prev_close
            day_pnl = (avg_close - prev_close) * float(sold_qty)

        row = self._base_row(iid_text)
        row.update(
            {
                "volume": 0,  # 当前拥股
                "can_use_volume": 0,  # 可用数量
                "frozen": 0,  # 冻结数量
                "avg_price": avg_price,  # 成本价
                "last_price": avg_close,  # 最新价 (清仓均价)
                "unrealized_pnl": None,  # 持仓盈亏 (已清仓 → 留空)
                "pnl_ratio": "-",  # 盈亏比例 (无法从对账数据可靠计算)
                "day_change": day_change,  # 当日涨幅
                "day_pnl": day_pnl,  # 当日盈亏 = (清仓均价 − 昨收) × 卖出股数
                "market_value": 0.0,  # 市值
                "position_cost": 0.0,  # 持仓成本
                "closed_today": True,
            }
        )
        return row

    def _asset_payload(self) -> dict:
        cache = self._cache
        try:
            accounts = cache.accounts()
        except Exception:
            accounts = []
        account = accounts[0] if accounts else None
        payload: dict[str, Any] = {
            "account": self._account_id,
            "nt_balance_free": None,
            "nt_balance_total": None,
            "total_asset": None,
            "market_value": None,
            "cash": None,
            "available_cash": None,
            "frozen_cash": None,
        }
        if account is None:
            return payload
        try:
            payload["nt_balance_free"] = self._sum_money(account.balances_free())
            payload["nt_balance_total"] = self._sum_money(account.balances_total())
        except Exception:
            pass
        info = self._account_info(account)
        for key in ("total_asset", "market_value", "cash", "available_cash", "frozen_cash"):
            payload[key] = _float_or_none(info.get(key))
        return payload

    @staticmethod
    def _account_info(account: Any) -> dict:
        try:
            event = account.last_event
        except Exception:
            return {}
        info = getattr(event, "info", None) if event is not None else None
        return info if isinstance(info, dict) else {}

    @staticmethod
    def _sum_money(money_map: Any) -> float:
        if not money_map:
            return 0.0
        total = 0.0
        for money in money_map.values():
            if money is None:
                continue
            try:
                total += float(money.as_decimal())
            except Exception:
                try:
                    total += float(money)
                except (TypeError, ValueError):
                    continue
        return total

    # ---- control actions -----------------------------------------------------

    def _set_paused(self, paused: bool) -> dict:
        self._strategy.trading_controller.set_paused(paused)
        self._persist_paused(paused)
        self._append_action(
            "suspend" if paused else "resume",
            {"trading_paused": paused},
            "ok",
        )
        return {"ok": True, "trading_paused": paused}

    def _sell(self, instrument_ids: list[str] | None, action: str, reason: str) -> dict:
        result = self._strategy.trading_controller.sell(instrument_ids, reason)
        self._append_action(action, {"instrument_ids": instrument_ids}, json.dumps(result, default=str))
        return {"ok": True, "result": result}

    def _resolve_instrument_id(self, body: dict) -> str | None:
        iid = body.get("instrument_id")
        if iid:
            return str(iid)
        stock_code = body.get("stock_code")
        if not stock_code:
            return None
        target = str(stock_code).upper()
        # Map stock code -> instrument-id text via the strategy's mapping.
        mapping = getattr(self._strategy, "_stock_by_instrument", {})
        if isinstance(mapping, dict):
            for iid_text, code in mapping.items():
                if str(code).upper() == target:
                    return iid_text
        # Fallback: match by the parsed symbol of currently-held positions.
        for position in self._open_long_positions():
            iid_text = str(position.instrument_id)
            if self._stock_code(iid_text).upper() == target:
                return iid_text
        return None

    def _control_state(self) -> dict:
        recent: list[dict] = []
        if self._control_writer is not None:
            try:
                recent = self._control_writer.load_recent_actions(
                    self._account_id, self._trader_id, limit=100
                )
            except Exception:
                recent = []
        return {
            "trading_paused": bool(self._strategy.trading_controller.is_paused()),
            "recent_actions": recent,
        }

    def _persist_paused(self, paused: bool) -> None:
        if self._control_writer is None:
            return
        try:
            self._control_writer.set_trading_paused(self._account_id, self._trader_id, paused)
        except Exception:
            pass

    def _append_action(self, action: str, detail: dict, result: str) -> None:
        if self._control_writer is None:
            return
        try:
            self._control_writer.append_action(
                self._account_id, self._trader_id, action, detail, result
            )
        except Exception:
            pass

    # ---- request handling ----------------------------------------------------

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:  # noqa: N802
                pass

            def _send(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, default=str).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return {}
                return parsed if isinstance(parsed, dict) else {}

            def _require_token(self) -> bool:
                # Fail-safe: no configured token means all writes are rejected.
                if not server._config.token:
                    self._send(401, {"error": "control token not configured"})
                    return False
                provided = self.headers.get("X-Control-Token")
                if provided != server._config.token:
                    self._send(401, {"error": "invalid or missing control token"})
                    return False
                return True

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0].rstrip("/") or "/"
                try:
                    if path == "/health/live":
                        self._send(200, {"status": "alive"})
                    elif path == "/realtime/positions":
                        self._send(
                            200,
                            {
                                "positions": server._positions_payload(),
                                "asset": server._asset_payload(),
                                "server_time": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                    elif path == "/control/state":
                        self._send(200, server._control_state())
                    elif path == "/realtime/closed_debug":
                        self._send(200, server._closed_debug_payload())
                    else:
                        self._send(404, {"error": "not found", "path": self.path})
                except Exception as exc:  # never let a handler crash the thread
                    self._send(500, {"error": repr(exc)})

            def do_POST(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0].rstrip("/") or "/"
                try:
                    if path not in (
                        "/control/suspend",
                        "/control/resume",
                        "/control/sell",
                        "/control/sell_all",
                    ):
                        self._send(404, {"error": "not found", "path": self.path})
                        return
                    if not self._require_token():
                        return
                    body = self._read_body()
                    if path == "/control/suspend":
                        self._send(200, server._set_paused(True))
                    elif path == "/control/resume":
                        self._send(200, server._set_paused(False))
                    elif path == "/control/sell_all":
                        self._send(200, server._sell(None, "sell_all", SELL_ALL_REASON))
                    elif path == "/control/sell":
                        iid = server._resolve_instrument_id(body)
                        if iid is None:
                            self._send(400, {"error": "unknown stock_code / instrument_id"})
                            return
                        self._send(200, server._sell([iid], "sell", SELL_REASON))
                except TimeoutError as exc:
                    self._send(503, {"error": repr(exc)})
                except Exception as exc:
                    self._send(500, {"error": repr(exc)})

        return _Handler
