from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.identifiers import InstrumentId
from strategies.order_splitter.notional_order_splitter import LimitOrderQuantityRules
from strategies.model_target_planners.base import ModelTargetPlanningContext, ModelTargetInstrumentContext
from trading_orchestration.adapters.formal_target_adapter import formal_target_from_plan
from trading_orchestration.components.assessors.exposure_assessor import ExposureAssessor
from trading_orchestration.contexts.exposure_context import (
    ExposureContext, ExposurePositionContext, HoldingSnapshot, InstrumentSnapshot, PendingExposureOrder,
)


class ExposureSnapshotProvider:
    """Read phase-specific Nautilus facts on the owner loop without broker API calls."""

    def __init__(self, host, config):
        self.host, self.config = host, config
        self.assessor = ExposureAssessor(config.tolerance)

    def _price(self, iid, now, whole):
        host = self.host
        prices = []
        for value in (host._market_status.get(iid), whole.get(iid)):
            if value is not None and value.last_price is not None:
                stamp = value.ts_event or 0
                if stamp and 0 <= now - stamp <= int(self.config.quote_max_age_secs * 1e9):
                    prices.append((stamp, Decimal(str(value.last_price))))
        book = host._depth_books.get(iid)
        if book is not None and book.best_bid is not None and book.best_ask is not None:
            if 0 <= now - book.ts_event <= int(self.config.quote_max_age_secs * 1e9):
                prices.append((book.ts_event, (Decimal(str(book.best_bid)) +
                                              Decimal(str(book.best_ask))) / 2))
        return max(prices, key=lambda value: value[0])[1] if prices else Decimal(0)

    def capture_positions(self) -> ExposurePositionContext:
        """Value only current holdings; targets and pending buys need no price here."""
        host = self.host
        now, day = int(host.clock.timestamp_ns()), host._clock_date()
        error, formal = "", None
        plan = host.trading_state.formal_model
        if plan is not None:
            try:
                formal = formal_target_from_plan(plan, host._plan_version(plan))
            except ValueError as exc:
                error = str(exc)
        positions = {}
        market_value = Decimal(0)
        whole = host.market_snapshots.latest_snapshot()
        for iid in sorted(host._held_instrument_ids()):
            qty = host._current_quantity(InstrumentId.from_str(iid))
            if qty == 0:
                continue
            price = self._price(iid, now, whole)
            positions[iid] = HoldingSnapshot(qty, price)
            if not price.is_finite() or price <= 0:
                error = error or f"missing_fresh_price:{iid}"
            else:
                market_value += qty * price
        venue = host._instrument_ids[0].venue if host._instrument_ids else None
        account = host.portfolio.account(venue=venue) if venue is not None else None
        balance = account.balance_total() if account is not None else None
        if balance is None:
            error = error or "missing_account_balance"
        assets = market_value + (Decimal(0) if balance is None else balance.as_decimal())
        pending = tuple(PendingExposureOrder(str(order.instrument_id), order.side.name.lower(),
                                             order.leaves_qty.as_decimal())
                        for order in sorted(host.replanning_orders(), key=lambda order: str(order.client_order_id)))
        return ExposurePositionContext(now, day.isoformat(), host._execution_activity, assets,
                                       positions, formal, len(pending), error, pending)

    def capture_planning(self) -> ExposureContext:
        """Revalue the portfolio and capture candidates for its current corrective side."""
        positions = self.capture_positions()
        assessment = self.assessor.assess(positions)
        candidates = set()
        if assessment.status == "correction":
            for iid in set(positions.target.quantities) | set(positions.positions):
                holding = positions.positions.get(iid)
                quantity = Decimal(0) if holding is None else holding.quantity
                target = positions.target.quantities.get(iid, Decimal(0))
                if ((assessment.direction == "buy" and target > quantity)
                        or (assessment.direction == "sell" and quantity > target)):
                    candidates.add(iid)
        return self._capture_execution(positions, candidates)

    def capture_order(self, instrument_id: str) -> ExposureContext:
        """Refresh valuation and only this order's inputs and pending commitments."""
        positions = self.capture_positions()
        candidates = {instrument_id} | {order.instrument_id for order in positions.pending}
        return self._capture_execution(positions, candidates)

    def _capture_execution(self, positions, candidates) -> ExposureContext:
        host = self.host
        instruments = {
            iid: InstrumentSnapshot(holding.quantity, holding.price, Decimal(0), can_buy=False, can_sell=False)
            for iid, holding in positions.positions.items()
        }
        whole = host.market_snapshots.latest_snapshot()
        for iid in sorted(candidates):
            holding = positions.positions.get(iid)
            qty = Decimal(0) if holding is None else holding.quantity
            price = self._price(iid, positions.now_ns, whole) if holding is None else holding.price
            if not price.is_finite() or price <= 0:
                instruments[iid] = InstrumentSnapshot(qty, price, Decimal(0), can_buy=False, can_sell=False,
                                                       quality_error=f"missing_fresh_price:{iid}")
                continue
            instrument = host.cache.instrument(InstrumentId.from_str(iid))
            if instrument is None:
                instruments[iid] = InstrumentSnapshot(qty, price, Decimal(0), can_buy=False, can_sell=False,
                                                       quality_error=f"missing_instrument:{iid}")
                continue
            rules = LimitOrderQuantityRules.from_instrument(instrument)
            tick = host._market_status.get(iid)
            day = host._clock_date()
            planning = ModelTargetPlanningContext(day, "continuous", {
                iid: ModelTargetInstrumentContext(price=float(price),
                    market_status=None if tick is None else tick.market_status)}, ts_init=positions.now_ns)
            can_buy, can_sell = host._planning_permissions(iid, day, planning_context=planning)
            sellable = Decimal(host.planning_sellable_quantity(iid, day))
            instruments[iid] = InstrumentSnapshot(
                qty, price, sellable, rules.quantity_increment, rules.min_quantity, can_buy, can_sell,
                "trading_permissions" if not (can_buy and can_sell) else "",
                Decimal(str(host.config.continuous_buy_min_notional)), allow_odd_lot_sell=rules.allow_odd_lot_sell)
        cash = host._nautilus_free_cash()
        error = positions.quality_error
        if cash is None:
            error = error or "missing_account_balance"
        return ExposureContext(positions.now_ns, positions.trading_date, positions.execution_revision,
                               positions.total_assets, Decimal(0) if cash is None else cash, instruments,
                               positions.target, positions.pending_orders, error, positions.pending)

