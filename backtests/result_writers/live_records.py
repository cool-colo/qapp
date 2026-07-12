from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import datetime
from decimal import Decimal
from typing import Any
from typing import Mapping


JsonMapping = Mapping[str, Any]

# Snapshot type values shared across the live_* tables.
BEFORE_TRADING = "before_trading"
CONTINUOUS_TRADING = "continuous_trading"
AFTER_TRADING = "after_trading"

# `source` marks whether a snapshot was captured at its natural time (``live``) or
# reconstructed from intraday state because the process started after the window
# (``fallback``).
SOURCE_LIVE = "live"
SOURCE_FALLBACK = "fallback"


@dataclass(frozen=True)
class LiveAssetSnapshotRecord:
    trade_date: date
    write_time: datetime
    snapshot_type: str
    account_id: str
    trader_id: str
    status: str = "ok"
    source: str = SOURCE_LIVE
    # QMT authoritative fields (no prefix; preferred when consuming).
    total_asset: Decimal | None = None
    market_value: Decimal | None = None
    cash: Decimal | None = None
    available_cash: Decimal | None = None
    frozen_cash: Decimal | None = None
    # Nautilus comparison fields (nt_ prefix; comparison only).
    nt_equity: Decimal | None = None
    nt_market_value: Decimal | None = None
    nt_balance_total: Decimal | None = None
    nt_balance_free: Decimal | None = None
    nt_balance_locked: Decimal | None = None
    nt_unrealized_pnl: Decimal | None = None
    nt_realized_pnl: Decimal | None = None
    qmt_raw: JsonMapping | None = None
    nt_raw: JsonMapping | None = None
    created_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class LivePositionSnapshotRecord:
    trade_date: date
    write_time: datetime
    snapshot_type: str
    account_id: str
    trader_id: str
    instrument_id: str
    stock_code: str
    status: str = "ok"
    source: str = SOURCE_LIVE
    # QMT authoritative fields.
    volume: int | None = None
    can_use_volume: int | None = None
    avg_price: Decimal | None = None
    open_price: Decimal | None = None
    close_price: Decimal | None = None
    market_value: Decimal | None = None
    # Nautilus comparison fields.
    nt_net_qty: int | None = None
    nt_avg_px_open: Decimal | None = None
    nt_market_value: Decimal | None = None
    nt_last_price: Decimal | None = None
    nt_unrealized_pnl: Decimal | None = None
    qmt_raw: JsonMapping | None = None
    nt_raw: JsonMapping | None = None
    created_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class LiveTargetRecord:
    trade_date: date
    write_time: datetime
    snapshot_type: str
    account_id: str
    trader_id: str
    instrument_id: str
    stock_code: str
    signal_date: date | None = None
    asset_snapshot_id: int | None = None
    position_snapshot_id: int | None = None
    total_asset: Decimal | None = None
    investable_asset: Decimal | None = None
    request_id: str | None = None
    target_version: str | None = None
    status: str = "ok"
    target_weight: Decimal | None = None
    open_price: Decimal | None = None
    price_source: str | None = None
    target_qty: int | None = None
    score: Decimal | None = None
    reason: str | None = None
    extra: JsonMapping | None = None
    created_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class LiveOrderRecord:
    trade_date: date
    write_time: datetime
    account_id: str
    trader_id: str
    client_order_id: str
    instrument_id: str
    status: str
    venue_order_id: str | None = None
    stock_code: str | None = None
    side: str | None = None
    order_type: str | None = None
    limit_price: Decimal | None = None
    quantity: int | None = None
    filled_qty: int = 0
    avg_fill_price: Decimal | None = None
    target_weight: Decimal | None = None
    target_version: str | None = None
    open_price: Decimal | None = None
    book_snapshot: JsonMapping | None = None
    reason: str | None = None
    qmt_raw: JsonMapping | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class LiveTradeRecord:
    trade_date: date
    write_time: datetime
    account_id: str
    trader_id: str
    trade_id: str
    client_order_id: str
    instrument_id: str
    venue_order_id: str | None = None
    stock_code: str | None = None
    side: str | None = None
    price: Decimal | None = None
    quantity: int | None = None
    amount: Decimal | None = None
    commission: Decimal | None = None
    trade_time: datetime | None = None
    qmt_raw: JsonMapping | None = None
    created_at: datetime | None = None
    schema_version: int = 1
