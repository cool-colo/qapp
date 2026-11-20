from __future__ import annotations

from decimal import Decimal
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.enums import OrderSide
from strategies.order_splitter.notional_order_splitter import LimitOrderQuantityRules
from trading_orchestration.adapters.formal_target_adapter import formal_target_from_plan
from trading_orchestration.contexts.exposure_context import ExposureContext, InstrumentSnapshot, PendingExposureOrder


class ExposureSnapshotProvider:
    """Read Nautilus facts on the owner loop; no broker-side API calls."""

    def __init__(self, host, config):
        self.host, self.config = host, config

    def capture(self) -> ExposureContext:
        with self.host.orchestration.pricing_windows(self.config.windows):
            return self._capture()

    def _capture(self) -> ExposureContext:
        host = self.host
        now, day = int(host.clock.timestamp_ns()), host._clock_date()
        error, formal = "", None
        plan = host.trading_state.formal_model
        if plan is not None:
            try:
                formal = formal_target_from_plan(plan, host._plan_version(plan))
            except ValueError as exc:
                error = str(exc)
        ids = host._held_instrument_ids() | set(host._target_quantities)
        if formal is not None:
            ids |= set(formal.quantities)
        instruments = {}
        market_value = Decimal(0)
        for iid in sorted(ids):
            instrument_id = InstrumentId.from_str(iid)
            instrument = host.cache.instrument(instrument_id)
            if instrument is None:
                error = f"missing_instrument:{iid}"
                continue
            qty = host._current_quantity(instrument_id)
            tick = host._market_status.get(iid)
            whole = host._whole_market_status.get(iid)
            book = host._depth_books.get(iid)
            prices = []
            for value in (tick, whole):
                if value is not None and value.last_price is not None:
                    stamp = value.ts_event or 0
                    if stamp and 0 <= now - stamp <= int(self.config.quote_max_age_secs * 1e9):
                        prices.append((stamp, Decimal(str(value.last_price))))
            if book is not None and book.best_bid is not None and book.best_ask is not None:
                if 0 <= now - book.ts_event <= int(self.config.quote_max_age_secs * 1e9):
                    prices.append((book.ts_event, (Decimal(str(book.best_bid)) +
                                                  Decimal(str(book.best_ask))) / 2))
            price = max(prices, key=lambda x: x[0])[1] if prices else Decimal(0)
            if price <= 0 or not price.is_finite():
                error = f"missing_fresh_price:{iid}"
            else:
                market_value += qty * price
            rules = LimitOrderQuantityRules.from_instrument(instrument)
            from strategies.model_target_planners.base import ModelTargetPlanningContext, ModelTargetInstrumentContext
            planning = ModelTargetPlanningContext(day, "continuous", {
                iid: ModelTargetInstrumentContext(price=float(price),
                    market_status=None if tick is None else tick.market_status)}, ts_init=now)
            can_buy, can_sell = host._planning_permissions(iid, day, planning_context=planning)
            buy = host._limit_price(instrument, instrument_id, OrderSide.BUY, Decimal(100))
            sell = host._limit_price(instrument, instrument_id, OrderSide.SELL, Decimal(100))
            sellable = Decimal(host.planning_sellable_quantity(iid, day))
            instruments[iid] = InstrumentSnapshot(
                qty, price, sellable, rules.quantity_increment, rules.min_quantity,
                can_buy, can_sell,
                None if buy is None else buy.as_decimal(),
                None if sell is None else sell.as_decimal(),
                "trading_permissions" if not (can_buy and can_sell) else "",
                Decimal(str(host.config.continuous_buy_min_notional)))
        venue = host._instrument_ids[0].venue if host._instrument_ids else None
        account = host.portfolio.account(venue=venue) if venue is not None else None
        balance = account.balance_total() if account is not None else None
        cash = host._nautilus_free_cash()
        if balance is None or cash is None:
            error = "missing_account_balance"
        assets = market_value + (Decimal(0) if balance is None else balance.as_decimal())
        pending = tuple(PendingExposureOrder(str(order.instrument_id), order.side.name.lower(),
                                             order.leaves_qty.as_decimal())
                        for order in sorted(host.replanning_orders(), key=lambda o: str(o.client_order_id)))
        return ExposureContext(now, day.isoformat(), host._execution_activity, assets,
                               cash or Decimal(0), instruments, formal,
                               len(pending), error, pending)

