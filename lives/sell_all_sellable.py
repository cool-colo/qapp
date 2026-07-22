#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import re
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from urllib.parse import urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NAUTILUS_TRADER_PATH = Path(
    os.environ.get("NAUTILUS_TRADER_PATH", "/data/flc/code/quant/nautilus_trader"),
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if NAUTILUS_TRADER_PATH.exists() and str(NAUTILUS_TRADER_PATH) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_TRADER_PATH))

if TYPE_CHECKING:
    from nautilus_trader.live.node import TradingNode


QMT_CLIENT = "QMT"
QMT_DEFAULT_HTTP_URL = "https://2395ebf9eb74494ba7c720002d305ccb.hn.takin.cc/"
QMT_PRICE_TYPE_LATEST_PRICE = 5
SELL_ALL_TAG = "SELL_ALL_POSITIONS"
QMT_ORDER_ID_TIMESTAMP_RE = re.compile(r"^O-(?P<date>\d{8})-(?P<time>\d{6})-")
QMT_OPEN_ORDER_STATUSES = {"", "UNSPECIFIED", "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED"}
QMT_SELL_ORDER_VALUES = {24, "24", "SELL", "sell"}


@dataclass(frozen=True)
class PositionPlanItem:
    stock_code: str
    volume: int
    sellable_volume: int
    avg_price: float | None


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def derive_ws_url(http_url: str) -> str:
    parsed = urlsplit(http_url)
    if parsed.scheme == "https":
        scheme = "wss"
    elif parsed.scheme == "http":
        scheme = "ws"
    else:
        scheme = parsed.scheme
    return urlunsplit((scheme, parsed.netloc, parsed.path, "", ""))


def build_cache_config(args: argparse.Namespace):
    """Return a Redis-backed CacheConfig when --use-redis is set, otherwise None.

    None keeps the default in-memory cache (no persistence across restarts).
    """
    if not args.use_redis:
        return None

    from urllib.parse import quote

    from nautilus_trader.config import CacheConfig
    from nautilus_trader.config import DatabaseConfig

    # Nautilus interpolates username/password directly into a redis:// URL, so
    # any URL-reserved characters (@ : / # ? % ...) must be percent-encoded here
    # or the Rust client rejects the URL with "InvalidClientConfig".
    username = quote(args.redis_username, safe="") if args.redis_username else None
    password = quote(args.redis_password, safe="") if args.redis_password else None

    return CacheConfig(
        database=DatabaseConfig(
            type="redis",
            host=args.redis_host,
            port=args.redis_port,
            username=username,
            password=password,
            ssl=args.redis_ssl,
        ),
        flush_on_start=args.redis_flush_on_start,
    )



def parse_symbols(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip().upper() for item in value.split(",") if item.strip()}


def safe_int(value: object, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return default


def safe_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def position_volume(raw_position: dict[str, object]) -> int:
    for key in ("volume", "current_amount", "total_volume"):
        if key in raw_position:
            return safe_int(raw_position.get(key))
    return 0


def position_sellable_volume(raw_position: dict[str, object]) -> int:
    for key in ("can_use_volume", "available_volume"):
        if key in raw_position:
            return safe_int(raw_position.get(key))
    return position_volume(raw_position)


def build_position_plan(
    positions: list[dict[str, object]],
    open_orders: list[dict[str, object]],
    only_symbols: set[str],
    excluded_symbols: set[str],
    min_volume: int,
) -> list[PositionPlanItem]:
    by_symbol: dict[str, PositionPlanItem] = {}
    for raw_position in positions:
        stock_code = str(raw_position.get("stock_code") or "").strip().upper()
        if not stock_code:
            continue
        if only_symbols and stock_code not in only_symbols:
            continue
        if stock_code in excluded_symbols:
            continue

        volume = position_volume(raw_position)
        sellable_volume = position_sellable_volume(raw_position)
        if sellable_volume < min_volume:
            continue

        existing = by_symbol.get(stock_code)
        avg_price = safe_float(raw_position.get("avg_price"))
        if existing is None:
            by_symbol[stock_code] = PositionPlanItem(
                stock_code=stock_code,
                volume=volume,
                sellable_volume=sellable_volume,
                avg_price=avg_price,
            )
        else:
            by_symbol[stock_code] = PositionPlanItem(
                stock_code=stock_code,
                volume=existing.volume + volume,
                sellable_volume=existing.sellable_volume + sellable_volume,
                avg_price=existing.avg_price if existing.avg_price is not None else avg_price,
            )

    for raw_order in open_orders:
        stock_code = str(raw_order.get("stock_code") or "").strip().upper()
        if not stock_code:
            continue
        if only_symbols and stock_code not in only_symbols:
            continue
        if stock_code in excluded_symbols:
            continue
        if not is_open_sell_order(raw_order):
            continue
        if stock_code in by_symbol:
            continue
        by_symbol[stock_code] = PositionPlanItem(
            stock_code=stock_code,
            volume=0,
            sellable_volume=0,
            avg_price=None,
        )
    return sorted(by_symbol.values(), key=lambda item: item.stock_code)


def is_open_sell_order(raw_order: dict[str, object]) -> bool:
    if raw_order.get("order_type") not in QMT_SELL_ORDER_VALUES:
        return False
    lifecycle_status = str(raw_order.get("lifecycle_status") or "").upper()
    return lifecycle_status in QMT_OPEN_ORDER_STATUSES


def print_plan(plan: list[PositionPlanItem], dry_run: bool) -> None:
    mode = "DRY RUN" if dry_run else "LIVE"
    total_volume = sum(item.sellable_volume for item in plan)
    positions = sum(1 for item in plan if item.sellable_volume > 0)
    cancel_only = len(plan) - positions
    print(
        f"[{mode}] positions={positions} cancel_only_targets={cancel_only} "
        f"total_sell_volume={total_volume}",
        flush=True,
    )
    if not plan:
        return
    print("stock_code      position_volume  sellable_volume    avg_price", flush=True)
    for item in plan:
        avg_price = "" if item.avg_price is None else f"{item.avg_price:.4f}"
        print(
            f"{item.stock_code:<11} {item.volume:>15} {item.sellable_volume:>15} {avg_price:>12}",
            flush=True,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Submit QMT SELL market orders for the current sellable position volume "
            "through Nautilus live execution. Dry-run is the default; pass --yes "
            "to submit live orders. In live mode the script keeps running until "
            "the selected sellable positions are sold out."
        ),
    )
    parser.add_argument("--account-id", default=env("QMT_ACCOUNT_ID"), help="MiniQMT account ID.")
    parser.add_argument("--account-type", default=env("QMT_ACCOUNT_TYPE", "STOCK"), help="MiniQMT account type.")
    parser.add_argument(
        "--base-url-http",
        default=env("QMT_BASE_URL_HTTP", QMT_DEFAULT_HTTP_URL),
        help="quant-qmt-proxy HTTP base URL used by the QMT adapter.",
    )
    parser.add_argument(
        "--base-url-ws",
        default=env("QMT_BASE_URL_WS"),
        help="quant-qmt-proxy WebSocket base URL used by the QMT adapter.",
    )
    parser.add_argument("--api-key", default=env("QMT_API_KEY"), help="Bearer token for quant-qmt-proxy.")
    parser.add_argument(
        "--strategy-name",
        default=env("QMT_SELL_ALL_STRATEGY_NAME", "sell_all_positions"),
        help="Strategy name sent to the QMT execution adapter.",
    )
    parser.add_argument(
        "--price-type",
        type=int,
        default=int(env("QMT_SELL_ALL_PRICE_TYPE", str(QMT_PRICE_TYPE_LATEST_PRICE))),
        help="QMT price type for Nautilus market orders. Defaults to latest price.",
    )
    parser.add_argument(
        "--symbols",
        default=env("QMT_SELL_ALL_SYMBOLS", ""),
        help="Optional comma-separated allowlist, for example 600000.SH,000001.SZ.",
    )
    parser.add_argument(
        "--exclude-symbols",
        default=env("QMT_SELL_ALL_EXCLUDE_SYMBOLS", ""),
        help="Optional comma-separated blocklist.",
    )
    parser.add_argument(
        "--min-volume",
        type=int,
        default=int(env("QMT_SELL_ALL_MIN_VOLUME", "1")),
        help="Minimum full position quantity required before submitting an order.",
    )
    parser.add_argument(
        "--request-timeout-secs",
        type=float,
        default=float(env("QMT_REQUEST_TIMEOUT_SECS", "10")),
        help="HTTP request timeout.",
    )
    parser.add_argument("--trader-id", default=env("QMT_TRADER_ID", "QMT-001"))
    parser.add_argument("--order-id-tag", default=env("QMT_ORDER_ID_TAG", "001"))
    parser.add_argument(
        "--position-ready-timeout-secs",
        type=float,
        default=float(env("QMT_POSITION_READY_TIMEOUT_SECS", "15")),
        help=(
            "Max seconds to wait for the freshly opened QMT session to load account "
            "positions before building the sell plan. A cold session can return an "
            "empty position list until MiniQMT finishes loading the account."
        ),
    )
    parser.add_argument(
        "--poll-interval-secs",
        type=float,
        default=float(env("QMT_POLL_INTERVAL_SECS", "1.0")),
        help="QMT execution polling interval and sell-all monitor interval.",
    )
    parser.add_argument("--log-level", default=env("QMT_LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--log-directory",
        default=env("QMT_LOG_DIRECTORY"),
        help="Directory for Nautilus log files. Defaults to the working directory.",
    )
    parser.add_argument(
        "--log-file-name",
        default=env("QMT_LOG_FILE_NAME"),
        help="Base log file name (without extension). Defaults to an auto-generated trader-id/timestamp name.",
    )
    parser.add_argument(
        "--use-redis",
        action="store_true",
        default=env_bool("QMT_USE_REDIS", False),
        help="Back the Nautilus cache with Redis instead of the default in-memory cache (persists state across restarts).",
    )
    parser.add_argument(
        "--redis-host",
        default=env("QMT_REDIS_HOST", "127.0.0.1"),
        help="Redis host (used only with --use-redis).",
    )
    parser.add_argument(
        "--redis-port",
        type=int,
        default=int(env("QMT_REDIS_PORT", "6379")),
        help="Redis port (used only with --use-redis).",
    )
    parser.add_argument(
        "--redis-username",
        default=env("QMT_REDIS_USERNAME"),
        help="Redis username (used only with --use-redis).",
    )
    parser.add_argument(
        "--redis-password",
        default=env("QMT_REDIS_PASSWORD"),
        help="Redis password (used only with --use-redis).",
    )
    parser.add_argument(
        "--redis-ssl",
        action="store_true",
        default=env_bool("QMT_REDIS_SSL", False),
        help="Use an SSL/TLS connection to Redis (used only with --use-redis).",
    )
    parser.add_argument(
        "--redis-flush-on-start",
        action="store_true",
        default=env_bool("QMT_REDIS_FLUSH_ON_START", False),
        help="Flush the Redis database on start instead of reusing persisted state.",
    )
    parser.add_argument(
        "--adjust-type",
        default=env("QMT_ADJUST_TYPE", "none"),
        help="QMT dividend adjustment mode.",
    )
    parser.add_argument(
        "--complete-instrument-details",
        action="store_true",
        default=env_bool("QMT_COMPLETE_INSTRUMENT_DETAILS", False),
    )
    parser.add_argument(
        "--order-timeout-secs",
        type=float,
        default=float(env("QMT_SELL_ALL_ORDER_TIMEOUT_SECS", "120.0")),
        help="Cancel and retry this script's active SELL order if it is not fully filled within this many seconds.",
    )
    parser.add_argument(
        "--cancel-wait-secs",
        type=float,
        default=float(env("QMT_SELL_ALL_CANCEL_WAIT_SECS", "3.0")),
        help=(
            "Seconds to wait after reject/deny/expire before resubmitting remaining quantity. "
            "Successful cancels resubmit immediately."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Submit live SELL orders. Without this flag the script only prints the sell plan.",
    )
    args = parser.parse_args()
    if not args.base_url_ws:
        args.base_url_ws = derive_ws_url(args.base_url_http)
    if not args.account_id:
        parser.error("--account-id is required, or set QMT_ACCOUNT_ID")
    if args.min_volume <= 0:
        parser.error("--min-volume must be positive")
    if args.poll_interval_secs <= 0:
        parser.error("--poll-interval-secs must be positive")
    if args.order_timeout_secs <= 0:
        parser.error("--order-timeout-secs must be positive")
    if args.cancel_wait_secs < 0:
        parser.error("--cancel-wait-secs must be non-negative")
    if args.position_ready_timeout_secs < 0:
        parser.error("--position-ready-timeout-secs must be non-negative")
    return args


async def load_position_plan(args: argparse.Namespace) -> list[PositionPlanItem]:
    from nautilus_trader.adapters.qmt.http import QMTHttpClient

    client = QMTHttpClient(
        base_url=args.base_url_http,
        api_key=args.api_key,
        timeout_secs=args.request_timeout_secs,
    )
    session_id: str | None = None
    try:
        await client.connect()
        session = await client.open_session(args.account_id, args.account_type)
        session_id = str(session["session_id"])
        positions = await wait_for_positions(client, session_id, args.position_ready_timeout_secs)
        open_orders = await client.get_orders(session_id, cancelable_only=False)
        return build_position_plan(
            positions=positions,
            open_orders=open_orders,
            only_symbols=parse_symbols(args.symbols),
            excluded_symbols=parse_symbols(args.exclude_symbols),
            min_volume=args.min_volume,
        )
    finally:
        if session_id is not None:
            await client.close_session(session_id)
        await client.close()


def asset_is_loaded(asset: dict[str, object]) -> bool:
    # A freshly opened session returns empty/zero asset fields until MiniQMT
    # finishes loading the account. Any populated cash/asset figure means the
    # account snapshot has arrived, so an empty position list is now trustworthy.
    for key in ("cash", "frozen_cash", "total_asset", "market_value"):
        if safe_float(asset.get(key)):
            return True
    return False


async def wait_for_positions(
    client: object,
    session_id: str,
    timeout_secs: float,
) -> list[dict[str, object]]:
    # A cold QMT session can return an empty position list before MiniQMT has
    # loaded the account, which made the first run report "no sellable stock"
    # while a restart (warm proxy) showed the real positions. Poll until
    # positions appear, or until the account snapshot confirms there genuinely
    # are none, bounded by timeout_secs.
    deadline = monotonic() + max(0.0, timeout_secs)
    attempt = 0
    last_positions: list[dict[str, object]] = []
    while True:
        attempt += 1
        positions = await client.get_positions(session_id)
        last_positions = positions
        if positions:
            if attempt > 1:
                print(
                    f"[INFO] account positions loaded after {attempt} attempt(s)",
                    flush=True,
                )
            return positions

        # No positions yet. If the account snapshot is loaded, trust the empty
        # result; otherwise the session is still warming up.
        try:
            asset = await client.get_asset(session_id)
        except Exception:  # noqa: BLE001 - asset probe is best-effort
            asset = {}
        if asset_is_loaded(asset):
            return positions

        if monotonic() >= deadline:
            print(
                "[WARN] timed out waiting for QMT session to load positions; "
                "proceeding with the latest (possibly empty) snapshot",
                flush=True,
            )
            return last_positions

        await asyncio.sleep(0.5)


def quantity_to_int(value: object) -> int:
    return safe_int(str(value))


def qmt_client_order_id_age_secs(client_order_id: object) -> float | None:
    match = QMT_ORDER_ID_TIMESTAMP_RE.match(str(client_order_id))
    if match is None:
        return None

    try:
        # Nautilus encodes the client order id timestamp from its engine clock,
        # which is UTC in live trading. Comparing against a naive datetime.now()
        # (local time) skewed the computed age by the machine's UTC offset.
        submitted_at = datetime.strptime(
            match.group("date") + match.group("time"),
            "%Y%m%d%H%M%S",
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None

    age_secs = (datetime.now(timezone.utc) - submitted_at).total_seconds()
    return max(0.0, age_secs)


def build_node(
    args: argparse.Namespace,
    plan: list[PositionPlanItem],
) -> tuple["TradingNode", object]:
    from nautilus_trader.adapters.qmt import QMTDataClientConfig
    from nautilus_trader.adapters.qmt import QMTExecClientConfig
    from nautilus_trader.adapters.qmt import QMTInstrumentProviderConfig
    from nautilus_trader.adapters.qmt import QMTLiveDataClientFactory
    from nautilus_trader.adapters.qmt import QMTLiveExecClientFactory
    from nautilus_trader.adapters.qmt.common import qmt_symbol_to_instrument_id
    from nautilus_trader.config import LiveExecEngineConfig
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.config import StrategyConfig
    from nautilus_trader.config import TradingNodeConfig
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import TraderId
    from nautilus_trader.trading.strategy import Strategy

    instrument_ids = [qmt_symbol_to_instrument_id(item.stock_code) for item in plan]
    quantities = {
        str(instrument_id): item.sellable_volume
        for instrument_id, item in zip(instrument_ids, plan, strict=True)
    }

    class SellAllPositionsConfig(StrategyConfig, frozen=True):
        instrument_ids: list[object]
        quantities: dict[str, int]
        order_timeout_secs: float
        retry_interval_secs: float
        cancel_wait_secs: float

    class SellAllPositionsStrategy(Strategy):
        def __init__(self, config: SellAllPositionsConfig) -> None:
            super().__init__(config)
            self.done = False
            self.submitted_count = 0
            self.skipped_count = 0
            self.canceled_count = 0
            self.remaining_by_instrument = {
                str(instrument_id): int(config.quantities.get(str(instrument_id), 0))
                for instrument_id in config.instrument_ids
            }
            self.active_order_by_instrument: dict[str, Any] = {}
            self.instrument_by_order_id: dict[str, str] = {}
            self.submitted_at_by_order_id: dict[str, float] = {}
            self.accounted_filled_by_order_id: dict[str, int] = {}
            self.accounted_trade_ids: set[str] = set()
            self.retry_after_by_instrument: dict[str, float] = {}
            self.cancel_requested_at_by_order_id: dict[str, float] = {}
            self.terminal_order_ids: set[str] = set()
            self.waiting_order_snapshot_by_instrument: dict[str, str] = {}
            self.retry_wait_logged_instruments: set[str] = set()

        def on_start(self) -> None:
            self.clock.set_timer(
                name="SELL-ALL-POSITIONS-MONITOR",
                interval=timedelta(seconds=self.config.retry_interval_secs),
                callback=self._on_monitor_timer,
                fire_immediately=False,
            )
            planned = ", ".join(
                f"{instrument_id}={self.remaining_by_instrument.get(str(instrument_id), 0)}"
                for instrument_id in self.config.instrument_ids
            )
            self.log.info(f"Sell-all strategy started with sellable plan: {planned}")
            self._sync_open_sell_orders()
            self._on_monitor_timer(None)

        def on_order_filled(self, event: Any) -> None:
            if self._event_order_side(event) != "SELL":
                return

            order_id = str(event.client_order_id)
            instrument_key = self.instrument_by_order_id.get(order_id)
            if instrument_key is None:
                return

            trade_id = str(getattr(event, "trade_id", ""))
            if trade_id and trade_id in self.accounted_trade_ids:
                self.log.debug(f"Skipping already-accounted SELL fill: order={order_id} trade={trade_id}")
                return
            if trade_id:
                self.accounted_trade_ids.add(trade_id)

            filled_qty = quantity_to_int(event.last_qty)
            self.accounted_filled_by_order_id[order_id] = (
                self.accounted_filled_by_order_id.get(order_id, 0) + filled_qty
            )
            self.remaining_by_instrument[instrument_key] = max(
                0,
                self.remaining_by_instrument.get(instrument_key, 0) - filled_qty,
            )
            self.log.info(
                f"SELL fill {instrument_key}: filled={filled_qty} "
                f"remaining={self.remaining_by_instrument[instrument_key]}",
            )
            self._clear_active_if_closed(event.client_order_id, instrument_key)
            self._submit_missing_orders()
            self._mark_done_if_finished()

        def on_order_accepted(self, event: Any) -> None:
            order_id = str(event.client_order_id)
            if order_id not in self.cancel_requested_at_by_order_id:
                return

            order = self.cache.order(event.client_order_id)
            if order is not None and self._order_side(order) != "SELL":
                return

            instrument_key = self.instrument_by_order_id.get(order_id)
            if instrument_key is None and order is not None:
                instrument_key = str(order.instrument_id)
                self.instrument_by_order_id[order_id] = instrument_key
            if instrument_key is None:
                instrument_key = "UNKNOWN"

            self.log.warning(
                f"SELL cancel did not reach terminal status for {instrument_key}: "
                f"order={order_id} status=ACCEPTED; will stop blocking replacement "
                f"after {self.config.cancel_wait_secs:g}s",
            )

        def on_order_canceled(self, event: Any) -> None:
            self._handle_order_terminal(event.client_order_id, "canceled")

        def on_order_denied(self, event: Any) -> None:
            self._handle_order_terminal(event.client_order_id, "denied")

        def on_order_rejected(self, event: Any) -> None:
            self._handle_order_terminal(event.client_order_id, "rejected")

        def on_order_expired(self, event: Any) -> None:
            self._handle_order_terminal(event.client_order_id, "expired")

        def on_order_cancel_rejected(self, event: Any) -> None:
            order_id = str(event.client_order_id)
            instrument_key = self.instrument_by_order_id.get(order_id, "UNKNOWN")
            self.cancel_requested_at_by_order_id.pop(order_id, None)
            self.log.warning(f"SELL cancel rejected {instrument_key} order={order_id}")

        def _on_monitor_timer(self, _event: Any) -> None:
            now = monotonic()
            for instrument_id in self.config.instrument_ids:
                instrument_key = str(instrument_id)
                active_orders = self._active_unfilled_sell_orders(instrument_id)
                if not active_orders:
                    self.active_order_by_instrument.pop(instrument_key, None)
                    self.waiting_order_snapshot_by_instrument.pop(instrument_key, None)
                    continue

                for order in active_orders:
                    self._track_open_order(instrument_key, order, now)
                    self._account_order_filled_qty(order, instrument_key)

                # Orders that have outlived order_timeout_secs and have no
                # cancel in flight yet.
                stale_orders = [
                    order
                    for order in active_orders
                    if str(order.client_order_id) not in self.cancel_requested_at_by_order_id
                    and not self._is_pending_cancel(order)
                    and self._order_age_secs(order, now) >= self.config.order_timeout_secs
                ]
                # Orders we already asked to cancel, but the venue neither
                # canceled nor acknowledged within cancel_wait_secs. The cancel
                # was likely lost; re-issue it instead of leaving the order live
                # forever (which previously let us resubmit alongside it).
                lost_cancel_orders = [
                    order
                    for order in active_orders
                    if self._cancel_wait_elapsed(str(order.client_order_id), now)
                    and not self._is_pending_cancel(order)
                ]

                to_cancel = stale_orders + lost_cancel_orders
                if not to_cancel:
                    continue

                self.log.warning(
                    f"Canceling {len(stale_orders)} stale and re-canceling "
                    f"{len(lost_cancel_orders)} unacknowledged SELL order(s) for "
                    f"{instrument_key} (timeout {self.config.order_timeout_secs:g}s)",
                )
                canceled_any = False
                seen_order_ids: set[str] = set()
                for order in to_cancel:
                    order_id = str(order.client_order_id)
                    if order_id in seen_order_ids:
                        continue
                    seen_order_ids.add(order_id)
                    if order.is_closed or self._is_pending_cancel(order):
                        continue
                    self.log.info(
                        f"Canceling SELL {instrument_key} order={order_id} "
                        f"status={self._order_status(order)} leaves_qty={order.leaves_qty}",
                    )
                    self.cancel_order(order)
                    self.cancel_requested_at_by_order_id[order_id] = now
                    self.canceled_count += 1
                    canceled_any = True
                if canceled_any:
                    self.retry_after_by_instrument[instrument_key] = now

            self._submit_missing_orders()
            self._mark_done_if_finished()

        def _handle_order_terminal(self, client_order_id: object, status: str) -> None:
            order_id = str(client_order_id)
            instrument_key = self.instrument_by_order_id.get(order_id)
            order = self.cache.order(client_order_id)
            if instrument_key is None and order is not None:
                instrument_key = str(order.instrument_id)
                self.instrument_by_order_id[order_id] = instrument_key
            if instrument_key is None:
                self.terminal_order_ids.add(order_id)
                self.cancel_requested_at_by_order_id.pop(order_id, None)
                self.log.warning(f"SELL order {status} with unknown instrument: order={order_id}")
                return

            self.terminal_order_ids.add(order_id)
            if order is not None:
                self._account_order_filled_qty(order, instrument_key)
            self.active_order_by_instrument.pop(instrument_key, None)
            self.submitted_at_by_order_id.pop(order_id, None)
            retry_after = monotonic()
            if status != "canceled":
                retry_after += self.config.cancel_wait_secs
            self.retry_after_by_instrument[instrument_key] = retry_after
            self.cancel_requested_at_by_order_id.pop(order_id, None)
            self.retry_wait_logged_instruments.discard(instrument_key)
            self.log.info(
                f"SELL order {status} {instrument_key}: order={order_id} "
                f"remaining={self.remaining_by_instrument.get(instrument_key, 0)}",
            )
            self._submit_missing_orders()
            self._mark_done_if_finished()

        def _clear_active_if_closed(self, client_order_id: object, instrument_key: str) -> None:
            order_id = str(client_order_id)
            order = self.cache.order(client_order_id)
            if order is not None and order.is_closed:
                self.terminal_order_ids.add(order_id)
                self._account_order_filled_qty(order, instrument_key)
                self.active_order_by_instrument.pop(instrument_key, None)
                self.submitted_at_by_order_id.pop(order_id, None)
                self.cancel_requested_at_by_order_id.pop(order_id, None)
                self.retry_wait_logged_instruments.discard(instrument_key)
                if self.remaining_by_instrument.get(instrument_key, 0) > 0:
                    retry_after = monotonic()
                    if self._order_status(order) != "CANCELED":
                        retry_after += self.config.cancel_wait_secs
                    self.retry_after_by_instrument[instrument_key] = retry_after

        def _sync_open_sell_orders(self) -> None:
            now = monotonic()
            for instrument_id in self.config.instrument_ids:
                instrument_key = str(instrument_id)
                for order in self._active_unfilled_sell_orders(instrument_id):
                    self._track_open_order(instrument_key, order, now)

        def _active_unfilled_sell_orders(self, instrument_id: object) -> list[Any]:
            orders_by_id: dict[str, Any] = {}
            for order in (
                self.cache.orders_open(None, instrument_id, self.id, OrderSide.SELL)
                + self.cache.orders_inflight(None, instrument_id, self.id, OrderSide.SELL)
                + self.cache.orders_emulated(None, instrument_id, self.id, OrderSide.SELL)
            ):
                order_id = str(order.client_order_id)
                order_status = self._order_status(order)
                if self._is_terminal_status(order_status):
                    self._handle_terminal_order_from_cache(order, str(instrument_id), order_status)
                    continue
                if order_id in self.terminal_order_ids:
                    continue
                if quantity_to_int(order.leaves_qty) <= 0:
                    continue
                orders_by_id[order_id] = order
            return list(orders_by_id.values())

        def _handle_terminal_order_from_cache(
            self,
            order: Any,
            instrument_key: str,
            order_status: str,
        ) -> None:
            order_id = str(order.client_order_id)
            is_new_terminal = order_id not in self.terminal_order_ids
            self.terminal_order_ids.add(order_id)
            self.instrument_by_order_id[order_id] = instrument_key
            self.cancel_requested_at_by_order_id.pop(order_id, None)
            self.retry_wait_logged_instruments.discard(instrument_key)
            self.submitted_at_by_order_id.pop(order_id, None)
            active_order = self.active_order_by_instrument.get(instrument_key)
            if active_order is not None and str(active_order.client_order_id) == order_id:
                self.active_order_by_instrument.pop(instrument_key, None)

            self._account_order_filled_qty(order, instrument_key)
            remaining = self.remaining_by_instrument.get(instrument_key, 0)
            if remaining > 0 and order_status != "FILLED":
                retry_after = monotonic()
                if order_status != "CANCELED":
                    retry_after += self.config.cancel_wait_secs
                    self.retry_after_by_instrument.setdefault(instrument_key, retry_after)
                else:
                    self.retry_after_by_instrument[instrument_key] = retry_after

            if is_new_terminal:
                self.log.info(
                    f"SELL order terminal from cache {instrument_key}: order={order_id} "
                    f"status={order_status} remaining={remaining}",
                )

        def _track_open_order(self, instrument_key: str, order: Any, now: float) -> None:
            order_id = str(order.client_order_id)
            self.terminal_order_ids.discard(order_id)
            self.instrument_by_order_id[order_id] = instrument_key
            self.active_order_by_instrument.setdefault(instrument_key, order)
            self.submitted_at_by_order_id.setdefault(
                order_id,
                self._submitted_at_from_order(order, now),
            )

        def _account_order_filled_qty(self, order: Any, instrument_key: str) -> None:
            if self._order_side(order) != "SELL":
                return

            order_id = str(order.client_order_id)
            cumulative_filled = quantity_to_int(getattr(order, "filled_qty", 0))
            accounted_filled = self.accounted_filled_by_order_id.get(order_id, 0)
            missing_filled = cumulative_filled - accounted_filled
            if missing_filled <= 0:
                # Do NOT mark this order's trade ids accounted here. The cache may
                # expose a trade_id before its quantity is reflected in
                # filled_qty; marking it now would let the matching on_order_filled
                # event be skipped and the fill silently dropped from remaining.
                return

            # The cumulative reconciliation below covers every fill on this order,
            # so the corresponding fill events are now redundant and safe to skip.
            self.accounted_trade_ids.update(str(trade_id) for trade_id in getattr(order, "trade_ids", []))
            self.accounted_filled_by_order_id[order_id] = cumulative_filled
            self.remaining_by_instrument[instrument_key] = max(
                0,
                self.remaining_by_instrument.get(instrument_key, 0) - missing_filled,
            )
            self.log.info(
                f"SELL fill sync {instrument_key}: order={order_id} "
                f"filled_delta={missing_filled} remaining={self.remaining_by_instrument[instrument_key]}",
            )

        def _order_age_secs(self, order: Any, now: float) -> float:
            order_id = str(order.client_order_id)
            submitted_at = self.submitted_at_by_order_id.get(order_id)
            if submitted_at is not None:
                return max(0.0, now - submitted_at)

            ts_init = int(getattr(order, "ts_init", 0) or 0)
            if ts_init > 0:
                return max(0.0, (self.clock.timestamp_ns() - ts_init) / 1_000_000_000)

            return self.config.order_timeout_secs

        def _submitted_at_from_order(self, order: Any, now: float) -> float:
            order_id_age_secs = qmt_client_order_id_age_secs(order.client_order_id)
            if order_id_age_secs is not None:
                return now - order_id_age_secs

            ts_init = int(getattr(order, "ts_init", 0) or 0)
            if ts_init <= 0:
                return now
            age_secs = max(0.0, (self.clock.timestamp_ns() - ts_init) / 1_000_000_000)
            return now - age_secs

        def _is_pending_cancel(self, order: Any) -> bool:
            return self._order_status(order) == "PENDING_CANCEL"

        def _is_terminal_status(self, status: str) -> bool:
            return status in {"DENIED", "REJECTED", "CANCELED", "EXPIRED", "FILLED"}

        def _order_status(self, order: Any) -> str:
            try:
                return str(order.status_string())
            except (AttributeError, TypeError):
                status = getattr(order, "status", "UNKNOWN")
                return self._enum_name(status)

        def _order_side(self, order: Any) -> str:
            try:
                return str(order.side_string())
            except (AttributeError, TypeError):
                side = getattr(order, "side", "UNKNOWN")
                return self._enum_name(side)

        def _event_order_side(self, event: Any) -> str:
            side = getattr(event, "order_side", "UNKNOWN")
            return self._enum_name(side)

        def _enum_name(self, value: Any) -> str:
            text = str(getattr(value, "name", value))
            return text.rsplit(".", 1)[-1]

        def _describe_orders(self, orders: list[Any]) -> str:
            return ", ".join(
                f"{order.client_order_id}:{self._order_status(order)}:leaves={order.leaves_qty}"
                for order in orders
            )

        def _submit_missing_orders(self) -> None:
            if self.done:
                return

            now = monotonic()
            for instrument_id in self.config.instrument_ids:
                instrument_key = str(instrument_id)
                remaining = int(self.remaining_by_instrument.get(instrument_key, 0))
                if remaining <= 0:
                    continue
                # Any live SELL order on the venue (leaves_qty > 0, not terminal)
                # blocks resubmission. Submitting a fresh full-size order while
                # the old one is still open risks selling twice the intended
                # quantity if both fill. Stale/lost orders are re-canceled by the
                # monitor instead.
                blocking_orders = self._active_unfilled_sell_orders(instrument_id)
                if blocking_orders:
                    for order in blocking_orders:
                        self._track_open_order(instrument_key, order, now)
                        self._account_order_filled_qty(order, instrument_key)
                    self.log.debug(
                        f"Waiting for active SELL {instrument_key}: "
                        f"remaining={remaining} orders=[{self._describe_orders(blocking_orders)}]",
                    )
                    self._log_waiting_for_active_orders(instrument_key, remaining, blocking_orders)
                    continue
                retry_after = self.retry_after_by_instrument.get(instrument_key, 0.0)
                if now < retry_after:
                    # Log once per retry window instead of every monitor tick.
                    if instrument_key not in self.retry_wait_logged_instruments:
                        self.retry_wait_logged_instruments.add(instrument_key)
                        self.log.info(
                            f"Waiting to resubmit SELL {instrument_key}: "
                            f"remaining={remaining} retry_in_secs={retry_after - now:.1f}",
                        )
                    continue
                self.retry_wait_logged_instruments.discard(instrument_key)

                instrument = self.cache.instrument(instrument_id)
                if instrument is None:
                    self.skipped_count += 1
                    self.log.error(f"Cannot submit SELL: missing instrument {instrument_id}")
                    continue

                order = self.order_factory.market(
                    instrument_id=instrument_id,
                    order_side=OrderSide.SELL,
                    quantity=instrument.make_qty(remaining),
                    reduce_only=True,
                    tags=[SELL_ALL_TAG],
                )
                self.submit_order(order)
                order_id = str(order.client_order_id)
                self.active_order_by_instrument[instrument_key] = order
                self.instrument_by_order_id[order_id] = instrument_key
                self.submitted_at_by_order_id[order_id] = monotonic()
                self.retry_after_by_instrument.pop(instrument_key, None)
                self.submitted_count += 1
                self.log.info(f"Submitted SELL {instrument_id} qty={remaining} order={order_id}")

        def _cancel_wait_elapsed(self, order_id: str, now: float) -> bool:
            requested_at = self.cancel_requested_at_by_order_id.get(order_id)
            if requested_at is None:
                return False
            return now - requested_at >= self.config.cancel_wait_secs

        def _log_waiting_for_active_orders(
            self,
            instrument_key: str,
            remaining: int,
            active_orders: list[Any],
        ) -> None:
            snapshot = self._describe_orders(active_orders)
            if self.waiting_order_snapshot_by_instrument.get(instrument_key) == snapshot:
                return

            self.waiting_order_snapshot_by_instrument[instrument_key] = snapshot
            self.log.info(
                f"Waiting for active SELL {instrument_key}: "
                f"remaining={remaining} orders=[{snapshot}]",
            )

        def _mark_done_if_finished(self) -> None:
            if self.done:
                return
            remaining = sum(max(0, int(value)) for value in self.remaining_by_instrument.values())
            open_order_count = sum(
                len(self._active_unfilled_sell_orders(instrument_id))
                for instrument_id in self.config.instrument_ids
            )
            if remaining > 0 or open_order_count > 0:
                return

            self.done = True
            self.log.info(
                f"Sell-all complete: submitted={self.submitted_count} "
                f"canceled={self.canceled_count} skipped={self.skipped_count}",
            )

    instrument_provider = QMTInstrumentProviderConfig(
        load_ids=frozenset(instrument_ids),
        complete_details=args.complete_instrument_details,
    )
    config_node = TradingNodeConfig(
        trader_id=TraderId(args.trader_id),
        cache=build_cache_config(args),
        logging=LoggingConfig(
            log_level=args.log_level,
            log_level_file=args.log_level,
            log_directory=args.log_directory,
            log_file_name=args.log_file_name,
        ),
        exec_engine=LiveExecEngineConfig(
            reconciliation=True,
            reconciliation_lookback_mins=1440,
            reconciliation_instrument_ids=instrument_ids,
            filter_unclaimed_external_orders=True,
        ),
        data_clients={
            QMT_CLIENT: QMTDataClientConfig(
                base_url_http=args.base_url_http,
                base_url_ws=args.base_url_ws,
                api_key=args.api_key,
                instrument_provider=instrument_provider,
                request_timeout_secs=args.request_timeout_secs,
                adjust_type=args.adjust_type,
            ),
        },
        exec_clients={
            QMT_CLIENT: QMTExecClientConfig(
                account_id=args.account_id,
                account_type=args.account_type,
                base_url_http=args.base_url_http,
                base_url_ws=args.base_url_ws,
                api_key=args.api_key,
                instrument_provider=instrument_provider,
                request_timeout_secs=args.request_timeout_secs,
                poll_interval_secs=args.poll_interval_secs,
                default_market_price_type=args.price_type,
                strategy_name=args.strategy_name,
                enforce_sellable_position=True,
            ),
        },
        timeout_connection=30.0,
        timeout_reconciliation=10.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=5.0,
    )

    node = TradingNode(config=config_node)
    strategy = SellAllPositionsStrategy(
        config=SellAllPositionsConfig(
            instrument_ids=instrument_ids,
            quantities=quantities,
            order_timeout_secs=args.order_timeout_secs,
            retry_interval_secs=args.poll_interval_secs,
            cancel_wait_secs=args.cancel_wait_secs,
            external_order_claims=instrument_ids,
            order_id_tag=args.order_id_tag,
        ),
    )
    node.trader.add_strategy(strategy)
    node.add_data_client_factory(QMT_CLIENT, QMTLiveDataClientFactory)
    node.add_exec_client_factory(QMT_CLIENT, QMTLiveExecClientFactory)
    node.build()
    return node, strategy


async def run_node_until_sold_out(node: "TradingNode", strategy: object, args: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    stop_requested = asyncio.Event()
    installed_signals: list[signal.Signals] = []

    def request_stop(signum: signal.Signals) -> None:
        print(f"[LIVE] received {signum.name}; stopping Nautilus node...", flush=True)
        stop_requested.set()

    await node.kernel.start_async()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, request_stop, signum)
        except (NotImplementedError, RuntimeError):
            continue
        installed_signals.append(signum)

    try:
        while not getattr(strategy, "done", False) and not stop_requested.is_set():
            try:
                await asyncio.wait_for(
                    stop_requested.wait(),
                    timeout=min(args.poll_interval_secs, 1.0),
                )
            except TimeoutError:
                pass
    finally:
        for signum in installed_signals:
            loop.remove_signal_handler(signum)
        await node.stop_async()


def main() -> None:
    args = parse_args()
    try:
        plan = asyncio.run(load_position_plan(args))
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Failed to import Nautilus/QMT dependencies "
            f"({exc.name!r}). Activate the Nautilus environment or install the "
            f"dependencies for {NAUTILUS_TRADER_PATH}.",
        ) from None

    print_plan(plan, dry_run=not args.yes)
    if not args.yes or not plan:
        return

    try:
        node, strategy = build_node(args, plan)
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Failed to import Nautilus/QMT dependencies "
            f"({exc.name!r}). Activate the Nautilus environment or install the "
            f"dependencies for {NAUTILUS_TRADER_PATH}.",
        ) from None

    loop = node.get_event_loop()
    if loop is None:
        raise SystemExit("TradingNode did not provide an event loop")
    try:
        loop.run_until_complete(run_node_until_sold_out(node, strategy, args))
    except KeyboardInterrupt:
        print("[LIVE] keyboard interrupt received; stopping Nautilus node...", flush=True)
        loop.run_until_complete(node.stop_async())
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
