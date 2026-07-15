"""
Live daily-snapshot recorder for the QMT target-model node.

Runs as a Nautilus :class:`Actor` alongside the strategy (like
``lives/monitoring.py``): it reads the live ``Portfolio`` / ``Cache`` and the
injected QMT full-tick source, then persists daily snapshots, the day's frozen
portfolio target, and order/trade lifecycle events into the ``live_*`` MySQL tables
via :class:`LiveSnapshotWriter`.

Design boundaries:

- The strategy stays DB-free. This actor owns all MySQL access and only reaches the
  strategy through the public ``compute_daily_target_plan`` / ``update_target_quantities``
  methods.
- QMT-reported fields (from ``account.last_event.info`` and position reports) are the
  authoritative values and land in the unprefixed columns; Nautilus-derived values
  land in ``nt_`` columns for comparison. Both raw payloads are kept as JSON.
- Snapshots are idempotent: each phase (before/continuous/after-trading) writes once
  per day, guarded by the table unique keys and pre-checks. If the process misses the
  09:26-09:29 before-trading catch-up window, startup records continuous-trading
  instead; readers that expect before-trading data should fall back to continuous-
  trading when needed.
"""

from __future__ import annotations

import inspect
import asyncio
from datetime import date
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId

from backtests.result_writers.live_records import AFTER_TRADING
from backtests.result_writers.live_records import BEFORE_TRADING
from backtests.result_writers.live_records import CONTINUOUS_TRADING
from backtests.result_writers.live_records import SOURCE_FALLBACK
from backtests.result_writers.live_records import SOURCE_LIVE
from backtests.result_writers.live_records import LiveAssetSnapshotRecord
from backtests.result_writers.live_records import LiveOrderRecord
from backtests.result_writers.live_records import LivePositionSnapshotRecord
from backtests.result_writers.live_records import LiveTargetRecord
from backtests.result_writers.live_records import LiveTradeRecord


class SnapshotRecorderConfig(ActorConfig, frozen=True):
    account_id: str
    trader_id: str
    timezone_name: str = "Asia/Shanghai"
    before_time: str = "09:27"
    after_time: str = "15:40"
    trading_windows: str = "09:30-11:30,13:00-14:55"
    # The before-trading catch-up window: a start within this HH:MM range (inclusive)
    # still runs the before-trading phase even if before_time already passed.
    before_catchup_start: str = "09:26"
    before_catchup_end: str = "09:29"
    # Keep the MySQL connection warm between trading sessions so the server doesn't
    # close the idle socket and make the next order-event burst each hit a
    # connection-lost error. 4 min is well under a typical wait_timeout.
    keepalive_secs: int = 240


class SnapshotRecorder(Actor):
    """Persists daily snapshots, frozen targets, and order/trade events."""

    _BEFORE_ALERT = "SNAPSHOT-BEFORE-TRADING"
    _AFTER_ALERT = "SNAPSHOT-AFTER-TRADING"
    _KEEPALIVE_TIMER = "SNAPSHOT-DB-KEEPALIVE"
    _LIVE_ORDER_REASON_MAX_LEN = 64

    def __init__(
        self,
        config: SnapshotRecorderConfig,
        writer: Any,
        strategy_ref: Any,
        fetch_full_tick: Any | None = None,
        fetch_positions: Any | None = None,
    ) -> None:
        super().__init__(config)
        self._writer = writer
        self._strategy = strategy_ref
        self._fetch_full_tick = fetch_full_tick
        self._fetch_positions = fetch_positions
        self._before_time = self._parse_hh_mm(config.before_time)
        self._after_time = self._parse_hh_mm(config.after_time)
        # Guards so each phase applies the frozen target to the strategy at most once
        # per process-day even if the timer and the on-start catch-up both fire.
        self._applied_target_dates: set[date] = set()

    # ---- lifecycle -----------------------------------------------------------

    def on_start(self) -> None:
        self._subscribe_order_events()
        self._schedule_keepalive()
        if self._before_time is not None:
            self._schedule_daily(self._BEFORE_ALERT, self._before_time, self._on_before_timer)
        if self._after_time is not None:
            self._schedule_daily(self._AFTER_ALERT, self._after_time, self._on_after_timer)
        # Catch up on whatever phase we started inside of, so a mid-day (re)start still
        # records snapshots and loads/generates the day's target.
        self._catch_up_on_start()

    def _catch_up_on_start(self) -> None:
        now = self._now()
        today = now.date()
        current = now.time()
        try:
            after_t = pd.Timestamp(self.config.after_time).time()
        except Exception:
            after_t = None
        in_before = self._in_range(
            current,
            self.config.before_catchup_start,
            self.config.before_catchup_end,
        )
        missed_before = self._past_time(current, self.config.before_catchup_end)
        try:
            self._run_full_tick_fetch()
        except Exception as exc:
            self.log.warning(f"snapshot on-start full-tick fetch failed: {exc}")
        if after_t is not None and current >= after_t:
            # Post-close start: do not backfill before-trading after the window has
            # been missed; keep a continuous snapshot as the fallback baseline, then
            # record the after-trading snapshot.
            self._run_continuous_trading(today, allow_fallback=True)
            self._run_after_trading(today, allow_fallback=True)
            return
        if in_before:
            self._run_before_trading(today, allow_fallback=False)
            return
        if missed_before:
            self._run_continuous_trading(today, allow_fallback=not self._within_trading_window())

    def _on_before_timer(self, _event: Any) -> None:
        self._schedule_daily(self._BEFORE_ALERT, self._before_time, self._on_before_timer)
        today = self._now().date()
        try:
            self._run_full_tick_fetch()
        except Exception as exc:
            self.log.warning(f"snapshot before-trading full-tick fetch failed: {exc}")
        self._run_before_trading(today, allow_fallback=False)

    def _on_after_timer(self, _event: Any) -> None:
        self._schedule_daily(self._AFTER_ALERT, self._after_time, self._on_after_timer)
        self._run_after_trading(self._now().date(), allow_fallback=False)

    def _schedule_keepalive(self) -> None:
        interval = int(self.config.keepalive_secs)
        if interval <= 0:
            return
        self.clock.set_timer(
            name=self._KEEPALIVE_TIMER,
            interval=timedelta(seconds=interval),
            callback=self._on_keepalive,
            fire_immediately=False,
        )
        self.log.info(
            f"snapshot DB keepalive scheduled every {interval}s",
            color=LogColor.BLUE,
        )

    def _on_keepalive(self, _event: Any) -> None:
        try:
            self._writer.ping()
        except Exception as exc:
            self.log.warning(f"snapshot DB keepalive failed: {exc}")

    # ---- phase runners -------------------------------------------------------

    def _run_before_trading(self, trading_date: date, allow_fallback: bool) -> None:
        source = SOURCE_FALLBACK if allow_fallback and not self._within_trading_window() else SOURCE_LIVE
        asset_id = self._record_asset(trading_date, BEFORE_TRADING, source)
        self._record_positions(trading_date, BEFORE_TRADING, source)
        self._ensure_daily_target(trading_date, BEFORE_TRADING, asset_id)

    def _run_continuous_trading(self, trading_date: date, allow_fallback: bool = False) -> None:
        source = SOURCE_FALLBACK if allow_fallback and not self._within_trading_window() else SOURCE_LIVE
        asset_id = self._record_asset(trading_date, CONTINUOUS_TRADING, source)
        self._record_positions(trading_date, CONTINUOUS_TRADING, source)
        # A before-trading target takes precedence; only mint a continuous_trading
        # target if before-trading never ran (e.g. started mid-session same day).
        self._ensure_daily_target(trading_date, CONTINUOUS_TRADING, asset_id)

    def _run_after_trading(self, trading_date: date, allow_fallback: bool) -> None:
        source = SOURCE_FALLBACK if allow_fallback else SOURCE_LIVE
        self._record_asset(trading_date, AFTER_TRADING, source)
        self._record_positions(trading_date, AFTER_TRADING, source)

    # ---- asset snapshot ------------------------------------------------------

    def _record_asset(self, trading_date: date, snapshot_type: str, source: str) -> int | None:
        try:
            if self._writer.has_asset_snapshot(
                self.config.account_id, self.config.trader_id, trading_date, snapshot_type,
            ):
                return self._writer.asset_snapshot_id(
                    self.config.account_id, self.config.trader_id, trading_date, snapshot_type,
                )
        except Exception as exc:
            self.log.warning(f"asset snapshot existence check failed: {exc}")
        account = self._first_account()
        info = self._account_info(account)
        nt_free, nt_total, nt_locked = self._nt_balances(account)
        record = LiveAssetSnapshotRecord(
            trade_date=trading_date,
            write_time=self._now_naive(),
            snapshot_type=snapshot_type,
            account_id=self.config.account_id,
            trader_id=self.config.trader_id,
            source=source,
            total_asset=self._info_decimal(info, "total_asset"),
            market_value=self._info_decimal(info, "market_value"),
            cash=self._info_decimal(info, "cash"),
            available_cash=self._info_decimal(info, "available_cash", "cash"),
            frozen_cash=self._info_decimal(info, "frozen_cash"),
            nt_equity=self._nt_equity(),
            nt_market_value=self._nt_net_exposure(),
            nt_balance_total=nt_total,
            nt_balance_free=nt_free,
            nt_balance_locked=nt_locked,
            nt_unrealized_pnl=self._nt_pnl(realized=False),
            nt_realized_pnl=self._nt_pnl(realized=True),
            qmt_raw=self._jsonable(info),
            nt_raw={
                "balance_free": self._to_str(nt_free),
                "balance_total": self._to_str(nt_total),
                "balance_locked": self._to_str(nt_locked),
                "equity": self._to_str(self._nt_equity()),
            },
            created_at=self._now_naive(),
        )
        try:
            return self._writer.write_asset_snapshot(record)
        except Exception as exc:
            self.log.warning(f"asset snapshot write failed ({snapshot_type}): {exc}")
            return None

    # ---- position snapshot ---------------------------------------------------

    def _record_positions(self, trading_date: date, snapshot_type: str, source: str) -> None:
        broker_positions = self._run_position_fetch(trading_date, snapshot_type, source)
        if broker_positions is None:
            return
        self._record_positions_with_broker(trading_date, snapshot_type, source, broker_positions)

    def _record_positions_with_broker(
        self,
        trading_date: date,
        snapshot_type: str,
        source: str,
        broker_positions: dict[str, dict[str, Any]],
    ) -> None:
        records: list[LivePositionSnapshotRecord] = []
        for position in self._open_positions():
            instrument_id = position.instrument_id
            instrument_id_text = str(instrument_id)
            stock_code = self._stock_code(instrument_id_text)
            broker_position = self._broker_position_for(broker_positions, instrument_id_text, stock_code)
            can_use = self._venue_can_use(instrument_id_text)
            net_qty = self._decimal_or_none(getattr(position, "quantity", None)) or self._nt_net_qty(instrument_id)
            avg_px = self._decimal_or_none(getattr(position, "avg_px_open", None))
            last_price = self._strategy_last_close(instrument_id_text)
            open_price = self._position_open_price(instrument_id_text)
            close_price = self._position_close_price(snapshot_type, last_price)
            nt_market_value = self._market_value(net_qty, last_price)
            broker_volume = self._broker_decimal(broker_position, "volume")
            broker_can_use = self._broker_decimal(broker_position, "can_use_volume")
            broker_avg_price = self._broker_decimal(broker_position, "avg_price")
            broker_last_price = self._broker_decimal(broker_position, "last_price")
            broker_market_value = (
                self._broker_decimal(broker_position, "market_value")
                or self._market_value(broker_volume, broker_last_price)
            )
            records.append(
                LivePositionSnapshotRecord(
                    trade_date=trading_date,
                    write_time=self._now_naive(),
                    snapshot_type=snapshot_type,
                    account_id=self.config.account_id,
                    trader_id=self.config.trader_id,
                    instrument_id=instrument_id_text,
                    stock_code=stock_code,
                    source=source,
                    volume=self._int_or_none(broker_volume if broker_volume is not None else net_qty),
                    can_use_volume=self._int_or_none(
                        broker_can_use if broker_can_use is not None else can_use,
                    ),
                    avg_price=broker_avg_price if broker_avg_price is not None else avg_px,
                    open_price=open_price,
                    close_price=close_price,
                    market_value=broker_market_value if broker_market_value is not None else nt_market_value,
                    nt_net_qty=self._int_or_none(net_qty),
                    nt_avg_px_open=avg_px,
                    nt_market_value=nt_market_value,
                    nt_last_price=self._decimal_or_none(last_price),
                    nt_unrealized_pnl=None,
                    qmt_raw=self._position_qmt_raw(
                        broker_position,
                        fallback_can_use=can_use,
                    ),
                    nt_raw={
                        "net_qty": self._to_str(net_qty),
                        "avg_px_open": self._to_str(avg_px),
                        "last_price": self._to_str(last_price),
                    },
                    created_at=self._now_naive(),
                ),
            )
        if not records:
            return
        try:
            self._writer.write_position_snapshots(records)
        except Exception as exc:
            self.log.warning(f"position snapshot write failed ({snapshot_type}): {exc}")

    # ---- daily target (frozen shares) ---------------------------------------

    def _ensure_daily_target(self, trading_date: date, snapshot_type: str, asset_id: int | None) -> None:
        if trading_date in self._applied_target_dates:
            return
        signal_date = self._resolve_signal_date(trading_date)
        # Reuse a persisted target for the (account, trader, date, signal_date)
        # four-tuple across restarts; a before-trading target also satisfies a later
        # continuous-trading start.
        loaded = self._load_target(trading_date, signal_date)
        if loaded:
            self._apply_loaded_target(trading_date, loaded)
            self._applied_target_dates.add(trading_date)
            return
        self._generate_and_persist_target(trading_date, snapshot_type, signal_date, asset_id)
        self._applied_target_dates.add(trading_date)

    def _generate_and_persist_target(
        self,
        trading_date: date,
        snapshot_type: str,
        signal_date: date | None,
        asset_id: int | None,
    ) -> None:
        try:
            plan = self._strategy.compute_daily_target_plan(trading_date)
        except Exception as exc:
            self.log.warning(f"daily target plan computation failed: {exc}")
            return
        # Raw total asset and the buffer-adjusted investable basis both come from the
        # plan (stamped by the strategy when it built the request) so the persisted
        # figures match exactly what was sent to the risk manager.
        total_asset = self._plan_total_asset(plan)
        investable_asset = self._plan_investable_asset(plan, total_asset)
        open_prices = dict(plan.open_prices) if plan.open_prices else self._current_open_prices()
        price_sources = dict(plan.price_sources)
        # The risk-manager planner commits explicit share counts (固定目标股数, 0 kept).
        # These drive execution; weights are persisted for audit only.
        target_qty = self._plan_target_quantities(plan)
        version = self._strategy.plan_version(plan)
        plan_signal_date = plan.signal_date or signal_date
        position_id = self._position_snapshot_anchor(trading_date)
        # Persist a row per committed target (union of weighted + quantity-bearing
        # instruments) so qty==0 liquidation targets are recorded, not dropped.
        instrument_ids = sorted(set(plan.weights) | set(target_qty))
        records: list[LiveTargetRecord] = []
        for instrument_id_text in instrument_ids:
            weight = plan.weights.get(instrument_id_text)
            qty = target_qty.get(instrument_id_text)
            records.append(
                LiveTargetRecord(
                    trade_date=trading_date,
                    write_time=self._now_naive(),
                    snapshot_type=snapshot_type,
                    account_id=self.config.account_id,
                    trader_id=self.config.trader_id,
                    signal_date=plan_signal_date,
                    asset_snapshot_id=asset_id,
                    position_snapshot_id=position_id,
                    total_asset=self._decimal_or_none(total_asset),
                    investable_asset=self._decimal_or_none(investable_asset),
                    request_id=plan.request_id,
                    target_version=version,
                    instrument_id=instrument_id_text,
                    stock_code=self._stock_code(instrument_id_text),
                    target_weight=None if weight is None else Decimal(str(weight)),
                    open_price=self._decimal_or_none(open_prices.get(instrument_id_text)),
                    price_source=price_sources.get(instrument_id_text),
                    target_qty=self._int_or_none(qty),
                    score=self._instrument_score(instrument_id_text),
                    reason=plan.reason,
                    extra={"target_version": version},
                    created_at=self._now_naive(),
                ),
            )
        if records:
            try:
                self._writer.write_target_portfolios(records)
            except Exception as exc:
                self.log.warning(f"target portfolio write failed: {exc}")
        self._apply_target(trading_date, target_qty, plan.reason, version)
        self.log.info(
            f"generated & persisted daily target: date={trading_date} signal_date={plan_signal_date} "
            f"weights={len(plan.weights)} frozen_qty={len(target_qty)} "
            f"total_asset={total_asset} investable_asset={investable_asset}",
            color=LogColor.GREEN,
        )

    def _plan_target_quantities(self, plan: Any) -> dict[str, Any]:
        plan_qty = getattr(plan, "target_qty", None)
        if isinstance(plan_qty, dict) and plan_qty:
            # Keep every entry, including committed 0 (liquidate) targets.
            return {str(instrument_id): qty for instrument_id, qty in plan_qty.items()}
        return {}

    def _plan_total_asset(self, plan: Any) -> Decimal:
        value = getattr(plan, "total_asset", None)
        coerced = self._decimal_or_none(value)
        if coerced is not None and coerced > 0:
            return coerced
        return self._frozen_total_asset()

    def _plan_investable_asset(self, plan: Any, total_asset: Decimal) -> Decimal:
        value = getattr(plan, "investable_asset", None)
        coerced = self._decimal_or_none(value)
        if coerced is not None and coerced > 0:
            return coerced
        # No stamped basis (equal-weight fallback): the frozen path already leaves room
        # for the buffer, so pin sizing on the raw total.
        return total_asset

    def _position_snapshot_anchor(self, trading_date: date) -> int | None:
        try:
            return self._writer.latest_position_snapshot_id(
                self.config.account_id,
                self.config.trader_id,
                trading_date,
                self._prev_trading_date(trading_date),
            )
        except Exception as exc:
            self.log.warning(f"position snapshot anchor lookup failed: {exc}")
            return None

    def _prev_trading_date(self, trading_date: date) -> date | None:
        dates = getattr(self._strategy, "_trading_dates", None)
        if not isinstance(dates, list):
            return None
        prev = [value for value in dates if value < trading_date]
        return max(prev) if prev else None

    def _apply_loaded_target(self, trading_date: date, rows: list[dict[str, Any]]) -> None:
        target_qty: dict[str, int] = {}
        version: str | None = None
        reason = "loaded_target"
        for row in rows:
            instrument_id_text = str(row.get("instrument_id"))
            qty = row.get("target_qty")
            if qty is not None:
                target_qty[instrument_id_text] = int(qty)
            if version is None and row.get("target_version"):
                version = str(row.get("target_version"))
            if row.get("reason"):
                reason = str(row.get("reason"))
        if not target_qty:
            return
        self._apply_target(trading_date, target_qty, reason, version)
        self.log.info(
            f"loaded persisted daily target: date={trading_date} "
            f"frozen_qty={len(target_qty)} version={version}",
            color=LogColor.GREEN,
        )

    def _apply_target(
        self,
        trading_date: date,
        target_qty: dict[str, Any],
        reason: str,
        version: str | None,
    ) -> None:
        try:
            self._strategy.update_target_quantities(
                quantities=target_qty,
                target_date=trading_date,
                reason=reason,
                version=version,
            )
        except Exception as exc:
            self.log.warning(f"update_target_quantities failed: {exc}")

    def _load_target(self, trading_date: date, signal_date: date | None) -> list[dict[str, Any]]:
        try:
            return self._writer.load_target_portfolios(
                self.config.account_id,
                self.config.trader_id,
                trading_date,
                signal_date,
                preferred_snapshot_type=BEFORE_TRADING,
                fallback_to_continuous=True,
            )
        except Exception as exc:
            self.log.warning(f"target load failed: {exc}")
            return []

    # ---- order / trade events ------------------------------------------------

    def _subscribe_order_events(self) -> None:
        msgbus = getattr(self, "msgbus", None)
        if msgbus is None:
            return
        try:
            msgbus.subscribe(topic="events.order.*", handler=self._on_order_event)
        except Exception as exc:
            self.log.warning(f"could not subscribe to order events: {exc}")

    def _on_order_event(self, event: Any) -> None:
        # Snapshot the current cache view of the order on every event so status
        # transitions (submitted → accepted → filled/canceled/rejected) upsert onto the
        # same row. Fills additionally produce an immutable trade row.
        try:
            self._upsert_order_from_event(event)
        except Exception as exc:
            self.log.warning(f"order event persist failed: {exc}")
        if isinstance(event, OrderFilled):
            try:
                self._write_trade(event)
            except Exception as exc:
                self.log.warning(f"trade persist failed: {exc}")

    def _upsert_order_from_event(self, event: Any) -> None:
        client_order_id = str(getattr(event, "client_order_id", "") or "")
        if not client_order_id:
            return
        order = None
        try:
            order = self.cache.order(getattr(event, "client_order_id"))
        except Exception:
            order = None
        instrument_id = getattr(event, "instrument_id", None)
        instrument_id_text = str(instrument_id or "")
        if order is not None:
            instrument_id = getattr(order, "instrument_id", instrument_id)
            instrument_id_text = str(instrument_id)
        trading_date = self._event_date(event)
        record = LiveOrderRecord(
            trade_date=trading_date,
            write_time=self._now_naive(),
            account_id=self.config.account_id,
            trader_id=self.config.trader_id,
            client_order_id=client_order_id,
            venue_order_id=self._maybe_str(getattr(event, "venue_order_id", None))
            or (self._maybe_str(getattr(order, "venue_order_id", None)) if order else None),
            instrument_id=instrument_id_text,
            stock_code=self._stock_code(instrument_id_text),
            side=self._order_side_text(order, event),
            order_type=self._order_type_text(order),
            limit_price=self._decimal_or_none(getattr(order, "price", None)) if order else None,
            quantity=self._int_or_none(getattr(order, "quantity", None)) if order else None,
            filled_qty=self._int_or_none(getattr(order, "filled_qty", None)) or 0 if order else 0,
            avg_fill_price=self._decimal_or_none(getattr(order, "avg_px", None)) if order else None,
            status=self._order_status_text(order, event),
            target_qty=self._order_target_qty(client_order_id),
            target_version=self._order_target_version(client_order_id),
            open_price=self._order_open_price(instrument_id_text),
            book_snapshot=self._order_book_snapshot(instrument_id, instrument_id_text),
            reason=self._bounded_order_reason(getattr(event, "reason", None)),
            qmt_raw=self._order_event_payload(event),
            created_at=self._now_naive(),
            updated_at=self._now_naive(),
        )
        self._writer.upsert_order(record)

    def _write_trade(self, event: OrderFilled) -> None:
        instrument_id_text = str(event.instrument_id)
        last_qty = self._decimal_or_none(event.last_qty)
        last_px = self._decimal_or_none(event.last_px)
        amount = None
        if last_qty is not None and last_px is not None:
            amount = last_qty * last_px
        record = LiveTradeRecord(
            trade_date=self._event_date(event),
            write_time=self._now_naive(),
            account_id=self.config.account_id,
            trader_id=self.config.trader_id,
            trade_id=str(event.trade_id),
            client_order_id=str(event.client_order_id),
            venue_order_id=self._maybe_str(getattr(event, "venue_order_id", None)),
            instrument_id=instrument_id_text,
            stock_code=self._stock_code(instrument_id_text),
            side="buy" if event.order_side == OrderSide.BUY else "sell",
            price=last_px,
            quantity=self._int_or_none(last_qty),
            amount=amount,
            commission=self._commission(event),
            trade_time=self._ns_to_naive(getattr(event, "ts_event", 0)),
            qmt_raw=self._event_info(event),
            created_at=self._now_naive(),
        )
        self._writer.upsert_trade(record)

    # ---- full tick -----------------------------------------------------------

    def _run_full_tick_fetch(self) -> None:
        if self._fetch_full_tick is None:
            return
        result = self._fetch_full_tick()
        if inspect.isawaitable(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                result = asyncio.run(self._await(result))
            else:
                # Fire-and-forget; the strategy also refreshes opens on its own timer.
                asyncio.ensure_future(self._apply_full_tick_async(result), loop=loop)
                return
        self._apply_full_tick(result)

    async def _await(self, awaitable: Any) -> Any:
        return await awaitable

    async def _apply_full_tick_async(self, awaitable: Any) -> None:
        try:
            snapshot = await awaitable
        except Exception as exc:
            self.log.warning(f"snapshot full-tick await failed: {exc}")
            return
        self._apply_full_tick(snapshot)

    def _run_position_fetch(
        self,
        trading_date: date,
        snapshot_type: str,
        source: str,
    ) -> dict[str, dict[str, Any]] | None:
        if self._fetch_positions is None:
            return {}
        try:
            result = self._fetch_positions()
        except Exception as exc:
            self.log.warning(f"snapshot broker-position fetch failed: {exc}")
            return {}
        if inspect.isawaitable(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                try:
                    result = asyncio.run(self._await(result))
                except Exception as exc:
                    self.log.warning(f"snapshot broker-position fetch failed: {exc}")
                    return {}
            else:
                task = asyncio.ensure_future(result, loop=loop)
                task.add_done_callback(
                    lambda done: self._on_position_fetch_done(
                        done,
                        trading_date,
                        snapshot_type,
                        source,
                    ),
                )
                return None
        return result if isinstance(result, dict) else {}

    def _on_position_fetch_done(
        self,
        task: asyncio.Future[Any],
        trading_date: date,
        snapshot_type: str,
        source: str,
    ) -> None:
        try:
            result = task.result()
        except Exception as exc:
            self.log.warning(f"snapshot broker-position fetch failed: {exc}")
            result = {}
        try:
            self._record_positions_with_broker(
                trading_date,
                snapshot_type,
                source,
                result if isinstance(result, dict) else {},
            )
        except Exception as exc:
            self.log.warning(f"position snapshot async write failed ({snapshot_type}): {exc}")

    def _apply_full_tick(self, snapshot: Any) -> None:
        if not isinstance(snapshot, dict) or not snapshot:
            return
        # Feed authoritative opens into the strategy so both pricing and the frozen
        # target quantities anchor on today's real open.
        setter = getattr(self._strategy, "_set_authoritative_open", None)
        if setter is None:
            return
        trading_date = self._now().date()
        for instrument_id, fields in snapshot.items():
            open_price = fields.get("open") if isinstance(fields, dict) else fields
            try:
                price = float(open_price)
            except (TypeError, ValueError):
                continue
            if price > 0:
                try:
                    setter(str(instrument_id), trading_date, price)
                except Exception:
                    continue

    # ---- strategy / portfolio readers ---------------------------------------

    def _frozen_total_asset(self) -> Decimal:
        getter = getattr(self._strategy, "_portfolio_value", None)
        if getter is not None:
            try:
                value = getter()
                if value and Decimal(str(value)) > 0:
                    return Decimal(str(value))
            except Exception:
                pass
        info = self._account_info(self._first_account())
        total = self._info_decimal(info, "total_asset")
        if total is not None and total > 0:
            return total
        # Restart tail case: neither the strategy nor the broker exposes a live total.
        # Fall back to a persisted snapshot by input priority (today before_trading →
        # today continuous_trading → previous after_trading).
        snapshot_total = self._snapshot_total_asset_fallback()
        if snapshot_total is not None and snapshot_total > 0:
            return snapshot_total
        return Decimal(str(getattr(self._strategy.config, "initial_cash", "1000000")))

    def _snapshot_total_asset_fallback(self) -> Decimal | None:
        try:
            trading_date = self._now().date()
        except Exception:
            return None
        try:
            value = self._writer.latest_asset_snapshot_value(
                self.config.account_id,
                self.config.trader_id,
                trading_date,
                self._prev_trading_date(trading_date),
                column="total_asset",
            )
        except Exception as exc:
            self.log.warning(f"snapshot total-asset fallback lookup failed: {exc}")
            return None
        return self._decimal_or_none(value)

    def _current_open_prices(self) -> dict[str, float]:
        opens = getattr(self._strategy, "_today_open", None)
        if isinstance(opens, dict) and opens:
            return dict(opens)
        # Fall back to last closes so a target can still be sized if opens are absent.
        closes = getattr(self._strategy, "_last_close", None)
        return dict(closes) if isinstance(closes, dict) else {}

    def _resolve_signal_date(self, trading_date: date) -> date | None:
        resolver = getattr(self._strategy, "_resolve_signal_date", None)
        if resolver is None:
            return None
        try:
            return resolver(trading_date)
        except Exception:
            return None

    def _open_positions(self) -> list[Any]:
        try:
            positions = self.cache.positions_open(account_id=self._account_id_obj())
        except Exception:
            try:
                positions = self.cache.positions_open()
            except Exception:
                positions = []
        result = []
        for position in positions:
            try:
                if position.is_long and self._decimal_or_none(position.quantity):
                    result.append(position)
            except Exception:
                continue
        return result

    def _venue_can_use(self, instrument_id_text: str) -> Decimal | None:
        venue_map = getattr(self._strategy, "_venue_sellable", None)
        if isinstance(venue_map, dict):
            return venue_map.get(instrument_id_text)
        return None

    def _stock_code(self, instrument_id_text: str) -> str:
        mapping = getattr(self._strategy, "_stock_by_instrument", {})
        code = mapping.get(instrument_id_text) if isinstance(mapping, dict) else None
        if code:
            return code
        text = instrument_id_text.upper()
        return text[:-4] if text.endswith(".QMT") else text

    def _instrument_score(self, instrument_id_text: str) -> Decimal | None:
        active = getattr(self._strategy, "_active_positions", {})
        if isinstance(active, dict):
            state = active.get(instrument_id_text)
            if isinstance(state, dict) and state.get("score") is not None:
                try:
                    return Decimal(str(state["score"]))
                except Exception:
                    return None
        return None

    def _strategy_last_close(self, instrument_id_text: str) -> float | None:
        closes = getattr(self._strategy, "_last_close", None)
        if isinstance(closes, dict):
            return closes.get(instrument_id_text)
        return None

    def _position_open_price(self, instrument_id_text: str) -> Decimal | None:
        opens = getattr(self._strategy, "_today_open", None)
        if not isinstance(opens, dict):
            return None
        return self._decimal_or_none(opens.get(instrument_id_text))

    def _position_close_price(
        self,
        snapshot_type: str,
        strategy_last_close: Any,
    ) -> Decimal | None:
        if snapshot_type != AFTER_TRADING:
            return None
        return self._decimal_or_none(strategy_last_close)

    @staticmethod
    def _broker_position_for(
        broker_positions: dict[str, dict[str, Any]],
        instrument_id_text: str,
        stock_code: str,
    ) -> dict[str, Any] | None:
        position = broker_positions.get(instrument_id_text)
        if isinstance(position, dict):
            return position
        position = broker_positions.get(stock_code)
        return position if isinstance(position, dict) else None

    @staticmethod
    def _broker_decimal(position: dict[str, Any] | None, key: str) -> Decimal | None:
        if not isinstance(position, dict):
            return None
        return SnapshotRecorder._decimal_or_none(position.get(key))

    @staticmethod
    def _position_qmt_raw(position: dict[str, Any] | None, fallback_can_use: Any) -> dict[str, Any]:
        if isinstance(position, dict):
            raw = position.get("raw")
            if isinstance(raw, dict):
                return SnapshotRecorder._jsonable(raw)
            return SnapshotRecorder._jsonable(position)
        return {"can_use_volume": SnapshotRecorder._to_str(fallback_can_use)}

    def _order_target_qty(self, client_order_id: str) -> int | None:
        quantities = getattr(self._strategy, "_order_target_qty", {})
        if isinstance(quantities, dict) and client_order_id in quantities:
            return self._int_or_none(quantities[client_order_id])
        return None

    def _order_target_version(self, client_order_id: str) -> str | None:
        versions = getattr(self._strategy, "_order_target_versions", {})
        if isinstance(versions, dict):
            return versions.get(client_order_id)
        return None

    def _order_open_price(self, instrument_id_text: str) -> Decimal | None:
        opens = getattr(self._strategy, "_today_open", None)
        if not isinstance(opens, dict):
            return None
        return self._decimal_or_none(opens.get(instrument_id_text))

    def _order_book_snapshot(
        self,
        instrument_id: Any,
        instrument_id_text: str,
    ) -> dict[str, Any] | None:
        resolved_instrument = instrument_id
        if resolved_instrument is None or isinstance(resolved_instrument, str):
            try:
                resolved_instrument = InstrumentId.from_str(instrument_id_text)
            except Exception:
                resolved_instrument = instrument_id

        book_snapshot = getattr(self._strategy, "_book_snapshot", None)
        if callable(book_snapshot):
            try:
                payload = self._book_snapshot_payload(book_snapshot(resolved_instrument))
                if payload is not None:
                    return payload
            except Exception:
                pass

        depth_books = getattr(self._strategy, "_depth_books", None)
        if isinstance(depth_books, dict):
            payload = self._book_snapshot_payload(depth_books.get(instrument_id_text))
            if payload is not None:
                return payload

        quote_snapshot = getattr(self._strategy, "_quote_snapshot", None)
        if callable(quote_snapshot):
            try:
                best_bid, best_ask = quote_snapshot(resolved_instrument)
            except Exception:
                return None
            return self._book_snapshot_payload(
                (
                    best_bid,
                    best_ask,
                    [(best_bid, 0.0)] if best_bid is not None else [],
                    [(best_ask, 0.0)] if best_ask is not None else [],
                ),
            )
        return None

    @classmethod
    def _book_snapshot_payload(cls, snapshot: Any) -> dict[str, Any] | None:
        if snapshot is None:
            return None
        if isinstance(snapshot, tuple) and len(snapshot) == 2:
            bids, asks = snapshot
            best_bid = bids[0][0] if bids else None
            best_ask = asks[0][0] if asks else None
        elif isinstance(snapshot, tuple) and len(snapshot) == 4:
            best_bid, best_ask, bids, asks = snapshot
        else:
            return None
        bid_levels = cls._book_side_payload(bids)
        ask_levels = cls._book_side_payload(asks)
        best_bid_value = cls._float_or_none(best_bid)
        best_ask_value = cls._float_or_none(best_ask)
        if (
            best_bid_value is None
            and best_ask_value is None
            and not bid_levels
            and not ask_levels
        ):
            return None
        return {
            "best_bid": best_bid_value,
            "best_ask": best_ask_value,
            "bids": bid_levels,
            "asks": ask_levels,
        }

    @classmethod
    def _book_side_payload(cls, levels: Any) -> list[dict[str, float]]:
        payload: list[dict[str, float]] = []
        for level in levels or []:
            price = None
            size = None
            if isinstance(level, (tuple, list)) and len(level) >= 2:
                price, size = level[0], level[1]
            else:
                price = getattr(level, "price", None)
                size = getattr(level, "size", None)
            price_value = cls._float_or_none(price)
            size_value = cls._float_or_none(size)
            if price_value is None:
                continue
            payload.append(
                {
                    "price": price_value,
                    "size": 0.0 if size_value is None else size_value,
                },
            )
        return payload

    def _nt_net_qty(self, instrument_id: InstrumentId) -> Decimal | None:
        try:
            qty = self.portfolio.net_position(instrument_id)
        except Exception:
            return None
        return None if qty is None else Decimal(str(qty))

    def _nt_equity(self) -> Decimal | None:
        try:
            equity = self.portfolio.equity(account_id=self._account_id_obj())
        except Exception:
            equity = None
        return self._sum_money_decimal(equity)

    def _nt_net_exposure(self) -> Decimal | None:
        # Portfolio.net_exposures() raises TypeError on any unpriced open position
        # (upstream Cython bug), which is common during the post-subscribe warmup. Sum
        # priced positions ourselves (net_qty × last_close) instead; this is a
        # comparison-only column, with the authoritative value in `market_value`.
        total = Decimal("0")
        found = False
        for position in self._open_positions():
            instrument_id_text = str(position.instrument_id)
            qty = self._decimal_or_none(getattr(position, "quantity", None))
            price = self._decimal_or_none(self._strategy_last_close(instrument_id_text))
            if qty is None or price is None:
                continue
            total += qty * price
            found = True
        return total if found else None

    def _nt_pnl(self, realized: bool) -> Decimal | None:
        try:
            if realized:
                pnls = self.portfolio.realized_pnls(account_id=self._account_id_obj())
            else:
                pnls = self.portfolio.unrealized_pnls(account_id=self._account_id_obj())
        except Exception:
            return None
        return self._sum_money_decimal(pnls)

    def _nt_balances(self, account: Any) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        if account is None:
            return (None, None, None)
        free = self._sum_money_decimal(self._call(account, "balances_free"))
        total = self._sum_money_decimal(self._call(account, "balances_total"))
        locked = self._sum_money_decimal(self._call(account, "balances_locked"))
        return (free, total, locked)

    def _first_account(self) -> Any:
        try:
            accounts = self.cache.accounts()
        except Exception:
            accounts = []
        return accounts[0] if accounts else None

    def _account_id_obj(self) -> Any:
        account = self._first_account()
        return account.id if account is not None else None

    # ---- small utilities -----------------------------------------------------

    @staticmethod
    def _account_info(account: Any) -> dict[str, Any]:
        if account is None:
            return {}
        try:
            event = account.last_event
        except Exception:
            return {}
        info = getattr(event, "info", None) if event is not None else None
        return info if isinstance(info, dict) else {}

    @staticmethod
    def _event_info(event: Any) -> dict[str, Any] | None:
        info = getattr(event, "info", None)
        if isinstance(info, dict) and info:
            return SnapshotRecorder._jsonable(info)
        return None

    @classmethod
    def _bounded_order_reason(cls, value: Any) -> str | None:
        text = cls._maybe_str(value)
        if text is None:
            return None
        max_len = cls._LIVE_ORDER_REASON_MAX_LEN
        if len(text) <= max_len:
            return text
        if max_len <= 3:
            return text[:max_len]
        return text[: max_len - 3] + "..."

    @classmethod
    def _order_event_payload(cls, event: Any) -> dict[str, Any] | None:
        payload = dict(cls._event_info(event) or {})
        reason = cls._maybe_str(getattr(event, "reason", None))
        if reason is not None:
            payload["reason"] = reason
        if not payload:
            return None
        return payload

    @staticmethod
    def _info_decimal(info: dict[str, Any], *keys: str) -> Decimal | None:
        for key in keys:
            value = info.get(key)
            if value is None:
                continue
            try:
                return Decimal(str(value))
            except Exception:
                continue
        return None

    @staticmethod
    def _sum_money_decimal(money_map: Any) -> Decimal | None:
        if not money_map:
            return None
        total = Decimal("0")
        found = False
        for value in money_map.values():
            if value is None:
                continue
            try:
                total += Decimal(str(value.as_decimal()))
                found = True
            except Exception:
                try:
                    total += Decimal(str(float(value)))
                    found = True
                except Exception:
                    continue
        return total if found else None

    @staticmethod
    def _call(obj: Any, method: str) -> Any:
        fn = getattr(obj, method, None)
        if fn is None:
            return None
        try:
            return fn()
        except Exception:
            return None

    @staticmethod
    def _decimal_or_none(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value.as_decimal()))
        except Exception:
            pass
        try:
            return Decimal(str(value))
        except Exception:
            return None

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        dec = SnapshotRecorder._decimal_or_none(value)
        return None if dec is None else int(dec)

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        dec = SnapshotRecorder._decimal_or_none(value)
        return None if dec is None else float(dec)

    @staticmethod
    def _market_value(qty: Any, price: Any) -> Decimal | None:
        qty_dec = SnapshotRecorder._decimal_or_none(qty)
        price_dec = SnapshotRecorder._decimal_or_none(price)
        if qty_dec is None or price_dec is None:
            return None
        return qty_dec * price_dec

    @staticmethod
    def _commission(event: OrderFilled) -> Decimal | None:
        commission = getattr(event, "commission", None)
        if commission is None:
            return None
        return SnapshotRecorder._decimal_or_none(commission)

    @staticmethod
    def _maybe_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value)
        return text or None

    @staticmethod
    def _enum_name_text(value: Any, lowercase: bool = True) -> str | None:
        if value is None:
            return None
        name = getattr(value, "name", None)
        if isinstance(name, str) and name:
            return name.lower() if lowercase else name
        text = str(value)
        if not text:
            return None
        return text.lower() if lowercase else text

    @staticmethod
    def _order_side_text(order: Any, event: Any) -> str | None:
        side = getattr(order, "side", None) if order is not None else None
        if side is None:
            side = getattr(event, "order_side", None)
        if side == OrderSide.BUY:
            return "buy"
        if side == OrderSide.SELL:
            return "sell"
        return None

    @staticmethod
    def _order_type_text(order: Any) -> str | None:
        if order is None:
            return None
        order_type = getattr(order, "order_type", None)
        return None if order_type is None else str(order_type)

    @staticmethod
    def _order_status_text(order: Any, event: Any) -> str:
        status = getattr(order, "status", None) if order is not None else None
        if status is not None:
            return SnapshotRecorder._enum_name_text(status) or "unknown"
        # Fall back to the event class name (e.g. OrderRejected) before the cache order
        # is available.
        return type(event).__name__.replace("Order", "").lower() or "unknown"

    @staticmethod
    def _jsonable(info: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in info.items():
            try:
                if isinstance(value, (str, int, float, bool)) or value is None:
                    result[str(key)] = value
                else:
                    result[str(key)] = str(value)
            except Exception:
                continue
        return result

    @staticmethod
    def _to_str(value: Any) -> str | None:
        return None if value is None else str(value)

    def _event_date(self, event: Any) -> date:
        ts = int(getattr(event, "ts_event", 0) or 0)
        converted = self._ns_to_date(ts)
        return converted or self._now().date()

    def _ns_to_date(self, ts_event: int) -> date | None:
        if ts_event <= 0:
            return None
        try:
            return pd.Timestamp(ts_event, unit="ns", tz="UTC").tz_convert(self.config.timezone_name).date()
        except Exception:
            return None

    def _ns_to_naive(self, ts_event: int) -> datetime | None:
        if not ts_event:
            return None
        try:
            return (
                pd.Timestamp(ts_event, unit="ns", tz="UTC")
                .tz_convert(self.config.timezone_name)
                .tz_localize(None)
                .to_pydatetime()
            )
        except Exception:
            return None

    def _now(self) -> pd.Timestamp:
        try:
            return pd.Timestamp(self.clock.utc_now()).tz_convert(self.config.timezone_name)
        except Exception:
            return pd.Timestamp.utcnow().tz_localize("UTC").tz_convert(self.config.timezone_name)

    def _now_naive(self) -> datetime:
        return self._now().tz_localize(None).to_pydatetime()

    def _within_trading_window(self) -> bool:
        current = self._now().time()
        for session in str(self.config.trading_windows).split(","):
            session = session.strip()
            if not session or "-" not in session:
                continue
            open_str, close_str = session.split("-", 1)
            try:
                open_t = pd.Timestamp(open_str.strip()).time()
                close_t = pd.Timestamp(close_str.strip()).time()
            except Exception:
                continue
            if open_t <= current <= close_t:
                return True
        return False

    def _in_range(self, current: Any, start: str, end: str) -> bool:
        try:
            start_t = pd.Timestamp(start).time()
            end_t = pd.Timestamp(end).time()
        except Exception:
            return False
        return start_t <= current <= end_t

    @staticmethod
    def _past_time(current: Any, boundary: str) -> bool:
        try:
            boundary_t = pd.Timestamp(boundary).time()
        except Exception:
            return False
        return current > boundary_t

    def _schedule_daily(self, name: str, hh_mm: tuple[int, int] | None, callback: Any) -> None:
        if hh_mm is None:
            return
        now = self._now()
        hh, mm = hh_mm
        target = now.normalize() + pd.Timedelta(hours=hh, minutes=mm)
        if target <= now:
            target = target + pd.Timedelta(days=1)
        self.clock.set_time_alert(name=name, alert_time=target, callback=callback, override=True)
        self.log.info(
            f"snapshot {name} scheduled for {target.isoformat()} ({self.config.timezone_name})",
            color=LogColor.BLUE,
        )

    @staticmethod
    def _parse_hh_mm(value: str | None) -> tuple[int, int] | None:
        if not value or not str(value).strip():
            return None
        hh, mm = str(value).strip().split(":")[:2]
        return int(hh), int(mm)
