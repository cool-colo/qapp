from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.trading.strategy import Strategy


@dataclass(frozen=True)
class ModelPredictionSignalEvent:
    signal_date: date
    instrument_id: str
    stock_code: str
    signal_name: str
    score: float | None
    rank: int | None
    side: str
    selected: bool
    extra: dict[str, Any]


@dataclass(frozen=True)
class ModelPredictionTargetEvent:
    target_id: str
    target_date: date
    execute_date: date
    instrument_id: str
    target_weight: float
    current_weight: float | None
    delta_weight: float | None
    reason: str
    extra: dict[str, Any]


@dataclass(frozen=True)
class ModelPredictionOrderEvent:
    order_id: str
    trading_date: date
    instrument_id: str
    side: str
    quantity: int
    target_weight: float
    status: str
    reason: str | None
    extra: dict[str, Any]


class ModelPredictionsStrategyConfig(StrategyConfig, frozen=True):
    instrument_ids: list[InstrumentId]
    bar_types: dict[str, BarType]
    instrument_stock_codes: dict[str, str]
    signals_by_date: dict[str, list[dict[str, Any]]]
    trading_dates: list[str]
    listed_dates: dict[str, str]
    st_by_date: dict[str, list[str]]
    suspended_by_date: dict[str, list[str]]
    max_positions: int = 30
    max_position_percent: float = 0.03
    holding_days: int = 10
    stop_loss: float = 0.05
    trailing_take_profit: float = 0.0
    trailing_take_profit_start: float = 0.0
    min_listed_days: int = 120
    initial_cash: Decimal = Decimal("1000000")
    timezone_name: str = "Asia/Shanghai"
    initial_last_closes: dict[str, float] | None = None
    initial_active_positions: dict[str, dict[str, Any]] | None = None


class ModelPredictionsStrategy(Strategy):
    """
    Reusable daily model-prediction strategy.

    Database access, venue symbol conversion, and result persistence are kept
    outside this strategy so the same trading logic can be wired into live
    execution.
    """

    def __init__(self, config: ModelPredictionsStrategyConfig) -> None:
        super().__init__(config)
        self._bar_types = {str(key): value for key, value in config.bar_types.items()}
        self._instrument_ids = list(config.instrument_ids)
        self._stock_by_instrument = {
            str(instrument_id): stock_code
            for instrument_id, stock_code in config.instrument_stock_codes.items()
        }
        self._instrument_by_stock = {
            stock_code: InstrumentId.from_str(instrument_id)
            for instrument_id, stock_code in self._stock_by_instrument.items()
        }
        self._signals_by_date = normalize_signals(config.signals_by_date)
        self._trading_dates = [pd.Timestamp(value).date() for value in config.trading_dates]
        self._listed_dates = {
            stock_code: pd.Timestamp(value).date()
            for stock_code, value in config.listed_dates.items()
            if value
        }
        self._st_by_date = {pd.Timestamp(key).date(): set(values) for key, values in config.st_by_date.items()}
        self._suspended_by_date = {
            pd.Timestamp(key).date(): set(values)
            for key, values in config.suspended_by_date.items()
        }
        self._last_close = normalize_initial_last_closes(config.initial_last_closes)
        self._active_positions = normalize_initial_active_positions(config.initial_active_positions)
        self._pending_targets: dict[str, float] = {}
        self._exit_retry_pool: set[str] = set()
        self._processed_dates: set[date] = set()
        self._rebalance_start_date = first_trading_date_on_or_after(
            self._trading_dates,
            pd.Timestamp(config.trading_dates[0]).date() if config.trading_dates else None,
        )
        self.signal_events: list[ModelPredictionSignalEvent] = []
        self.target_events: list[ModelPredictionTargetEvent] = []
        self.order_events: list[ModelPredictionOrderEvent] = []

    def on_start(self) -> None:
        for bar_type in self._bar_types.values():
            self.subscribe_bars(bar_type)

    def refresh_reference_data(
        self,
        instrument_ids: list[InstrumentId],
        bar_types: dict[str, BarType],
        instrument_stock_codes: dict[str, str],
        signals_by_date: dict[str, list[dict[str, Any]]],
        trading_dates: list[str],
        listed_dates: dict[str, str],
        st_by_date: dict[str, list[str]],
        suspended_by_date: dict[str, list[str]],
        last_closes: dict[str, float] | None = None,
        subscribe_new_bars: bool = True,
        unsubscribe_removed_bars: bool = False,
    ) -> None:
        existing_bar_type_keys = set(self._bar_types)
        refreshed_bar_types = {str(key): value for key, value in bar_types.items()}
        if unsubscribe_removed_bars:
            refreshed_keys = set(refreshed_bar_types)
            removable_keys = existing_bar_type_keys.difference(refreshed_keys).difference(self._active_positions)
            for key in sorted(removable_keys):
                try:
                    self.unsubscribe_bars(self._bar_types[key])
                except Exception as exc:
                    self.log.warning(f"Bar unsubscribe failed for {self._bar_types[key]}: {exc}")
                self._bar_types.pop(key, None)
                self._stock_by_instrument.pop(key, None)

        self._bar_types.update({str(key): value for key, value in bar_types.items()})
        known_ids = {str(instrument_id): instrument_id for instrument_id in self._instrument_ids}
        for instrument_id in instrument_ids:
            known_ids[str(instrument_id)] = instrument_id
        if unsubscribe_removed_bars:
            known_ids = {
                instrument_id: value
                for instrument_id, value in known_ids.items()
                if instrument_id in refreshed_bar_types or instrument_id in self._active_positions
            }
        self._instrument_ids = list(known_ids.values())
        self._stock_by_instrument.update(
            {
                str(instrument_id): stock_code
                for instrument_id, stock_code in instrument_stock_codes.items()
            },
        )
        self._instrument_by_stock = {
            stock_code: InstrumentId.from_str(instrument_id)
            for instrument_id, stock_code in self._stock_by_instrument.items()
        }
        self._signals_by_date = normalize_signals(signals_by_date)
        refreshed_trading_dates = [pd.Timestamp(value).date() for value in trading_dates]
        self._trading_dates = sorted(set(self._trading_dates).union(refreshed_trading_dates))
        self._listed_dates = {
            stock_code: pd.Timestamp(value).date()
            for stock_code, value in listed_dates.items()
            if value
        }
        self._st_by_date = {pd.Timestamp(key).date(): set(values) for key, values in st_by_date.items()}
        self._suspended_by_date = {
            pd.Timestamp(key).date(): set(values)
            for key, values in suspended_by_date.items()
        }
        self._last_close.update(normalize_initial_last_closes(last_closes))
        if subscribe_new_bars:
            for key, bar_type in self._bar_types.items():
                if key in existing_bar_type_keys:
                    continue
                try:
                    self.request_instrument(bar_type.instrument_id)
                except Exception as exc:
                    self.log.warning(f"Instrument request failed for {bar_type.instrument_id}: {exc}")
                self.subscribe_bars(bar_type)

    def on_bar(self, bar: Bar) -> None:
        self.log.info(f"on_bar: {bar.bar_type.instrument_id} ts={bar.ts_event} close={bar.close}")
        trading_date = bar_date(bar, self.config.timezone_name)
        if trading_date not in self._processed_dates:
            self._process_trading_day(trading_date)
            self._processed_dates.add(trading_date)

        instrument_id = str(bar.bar_type.instrument_id)
        self._last_close[instrument_id] = float(bar.close)

    def _process_trading_day(self, trading_date: date) -> None:
        self._pending_targets = {}
        for instrument_id in self._exit_retry_pool:
            self._pending_targets[instrument_id] = 0.0
        self._exit_retry_pool = set()
        self._seed_active_positions_from_portfolio(trading_date)

        signal_date = previous_trading_date(self._trading_dates, trading_date)
        today_signals = self._signals_by_date.get(signal_date, []) if signal_date else []
        target_ids = {
            str(self._instrument_by_stock[signal["stock_code"]])
            for signal in today_signals
            if signal["stock_code"] in self._instrument_by_stock
        }
        rebalance_today = is_rebalance_day(
            trading_dates=self._trading_dates,
            rebalance_start_date=self._rebalance_start_date,
            current_date=trading_date,
            holding_days=self.config.holding_days,
        )
        self._prepare_exits(trading_date, signal_date, target_ids, rebalance_today)
        self._prepare_entries_and_refresh(trading_date, today_signals)
        self._trim_active_positions()
        self._set_equal_weight_targets()
        self._submit_pending_targets(trading_date, signal_date)

    def _seed_active_positions_from_portfolio(self, trading_date: date) -> None:
        for instrument_id in self._instrument_ids:
            instrument_id_text = str(instrument_id)
            if instrument_id_text in self._active_positions:
                continue
            if self._current_quantity(instrument_id) <= 0:
                continue
            close_price = self._last_close.get(instrument_id_text)
            entry_price = self._open_position_entry_price(instrument_id) or close_price
            if entry_price is None or entry_price <= 0:
                continue
            stock_code = self._stock_by_instrument.get(instrument_id_text, "")
            signal_state = self._latest_signal_state(stock_code, trading_date)
            self._active_positions[instrument_id_text] = {
                "entry_date": trading_date,
                "entry_price": entry_price,
                "high_price": max(entry_price, float(close_price or entry_price)),
                "last_signal_date": signal_state.get("last_signal_date", trading_date),
                "score": signal_state.get("score", 0.0),
            }
            self.log.info(
                f"Seeded active model position from portfolio: {instrument_id_text} "
                f"entry_price={entry_price:.4f}",
                color=LogColor.BLUE,
            )

    def _open_position_entry_price(self, instrument_id: InstrumentId) -> float | None:
        try:
            positions = self.cache.positions_open(instrument_id=instrument_id)
        except Exception:
            return None
        prices = []
        for position in positions:
            try:
                if not position.is_long:
                    continue
                price = float(position.avg_px_open)
            except Exception:
                continue
            if price > 0:
                prices.append(price)
        return prices[0] if prices else None

    def _latest_signal_state(self, stock_code: str, trading_date: date) -> dict[str, Any]:
        latest_date = None
        latest_signal = None
        for signal_date, signals in self._signals_by_date.items():
            if signal_date > trading_date:
                continue
            for signal in signals:
                if signal["stock_code"] != stock_code:
                    continue
                if latest_date is None or signal_date > latest_date:
                    latest_date = signal_date
                    latest_signal = signal
        if latest_signal is None:
            return {}
        return {
            "last_signal_date": latest_date,
            "score": float(latest_signal.get("score", 0.0)),
        }

    def _prepare_exits(
        self,
        trading_date: date,
        signal_date: date | None,
        target_ids: set[str],
        is_rebalance: bool,
    ) -> None:
        exit_rank = 0
        for instrument_id, state in list(self._active_positions.items()):
            current_qty = self._current_quantity(InstrumentId.from_str(instrument_id))
            if current_qty <= 0:
                self._active_positions.pop(instrument_id, None)
                continue
            close_price = self._last_close.get(instrument_id)
            cost_price = float(state.get("entry_price") or close_price or 0.0)
            trailing = self._update_trailing_state(state, close_price, cost_price)
            stop_triggered = (
                close_price is not None
                and cost_price > 0
                and close_price <= cost_price * (1.0 - self.config.stop_loss)
            )
            trailing_triggered = bool(trailing["triggered"])
            rebalance_exit = is_rebalance and instrument_id not in target_ids
            if not (stop_triggered or trailing_triggered or rebalance_exit):
                continue
            self._pending_targets[instrument_id] = 0.0
            self._active_positions.pop(instrument_id, None)
            exit_rank += 1
            if stop_triggered:
                signal_name = "stop_triggered"
            elif trailing_triggered:
                signal_name = "trailing_take_profit_triggered"
            else:
                signal_name = "rebalance_exit"
            self._record_signal(
                signal_date=signal_date or trading_date,
                instrument_id=instrument_id,
                stock_code=self._stock_by_instrument.get(instrument_id, ""),
                signal_name=signal_name,
                score=state.get("score"),
                rank=exit_rank,
                side="sell",
                extra={
                    "close_price": close_price,
                    "entry_price": cost_price,
                    "high_price": trailing["high_price"],
                    "trailing_stop_price": trailing["stop_price"],
                },
            )

    def _prepare_entries_and_refresh(self, trading_date: date, signals: list[dict[str, Any]]) -> None:
        if not signals:
            return
        active_ids = set(self._active_positions)
        available_slots = max(0, int(self.config.max_positions) - len(active_ids))
        entry_rank = 0
        for signal in signals:
            stock_code = signal["stock_code"]
            instrument = self._instrument_by_stock.get(stock_code)
            if instrument is None:
                continue
            instrument_id = str(instrument)
            if self._pending_targets.get(instrument_id) == 0.0:
                continue
            skip_reason = self._entry_skip_reason(stock_code, trading_date)
            if skip_reason:
                self._record_signal(
                    signal_date=signal["date"],
                    instrument_id=instrument_id,
                    stock_code=stock_code,
                    signal_name="entry_filtered",
                    score=signal.get("score"),
                    rank=signal.get("rank"),
                    side="buy",
                    selected=False,
                    extra={"reason": skip_reason},
                )
                continue
            close_price = self._last_close.get(instrument_id)
            if close_price is None or close_price <= 0:
                continue
            state = self._active_positions.get(instrument_id)
            if state is None:
                if available_slots <= 0:
                    continue
                self._active_positions[instrument_id] = {
                    "entry_date": trading_date,
                    "entry_price": close_price,
                    "high_price": close_price,
                    "last_signal_date": signal["date"],
                    "score": float(signal["score"]),
                }
                available_slots -= 1
            else:
                state["last_signal_date"] = signal["date"]
                state["score"] = float(signal["score"])
            entry_rank += 1
            self._record_signal(
                signal_date=signal["date"],
                instrument_id=instrument_id,
                stock_code=stock_code,
                signal_name="model_prediction_score",
                score=signal.get("score"),
                rank=entry_rank,
                side="buy",
                selected=True,
                extra={"avg_amount_20": signal.get("avg_amount_20")},
            )

    def _entry_skip_reason(self, stock_code: str, trading_date: date) -> str | None:
        if stock_code in self._suspended_by_date.get(trading_date, set()):
            return "suspended"
        if stock_code in self._st_by_date.get(trading_date, set()):
            return "st"
        if self.config.min_listed_days > 0:
            listed_date = self._listed_dates.get(stock_code)
            if listed_date is not None:
                listed_days = (pd.Timestamp(trading_date) - pd.Timestamp(listed_date)).days
                if listed_days < int(self.config.min_listed_days):
                    return "new_stock"
        return None

    def _trim_active_positions(self) -> None:
        max_positions = int(self.config.max_positions)
        if max_positions <= 0 or len(self._active_positions) <= max_positions:
            return
        rows = []
        for instrument_id, state in self._active_positions.items():
            if self._pending_targets.get(instrument_id) == 0.0:
                continue
            rows.append(
                {
                    "instrument_id": instrument_id,
                    "score": float(state.get("score", 0.0)),
                    "last_signal_date": pd.Timestamp(state.get("last_signal_date", pd.Timestamp.min)),
                },
            )
        ranked = sorted(rows, key=lambda item: (item["score"], item["last_signal_date"]), reverse=True)
        keep_ids = {item["instrument_id"] for item in ranked[:max_positions]}
        for instrument_id in list(self._active_positions):
            if instrument_id in keep_ids or self._pending_targets.get(instrument_id) == 0.0:
                continue
            self._pending_targets[instrument_id] = 0.0
            self._active_positions.pop(instrument_id, None)

    def _set_equal_weight_targets(self) -> None:
        active_ids = sorted(self._active_positions)
        if not active_ids:
            return
        target_percent = min(float(self.config.max_position_percent), 1.0 / len(active_ids))
        for instrument_id in active_ids:
            self._pending_targets.setdefault(instrument_id, target_percent)

    def _submit_pending_targets(self, trading_date: date, signal_date: date | None) -> None:
        exit_targets = {
            instrument_id: weight
            for instrument_id, weight in self._pending_targets.items()
            if weight == 0.0
        }
        non_exit_targets = {
            instrument_id: weight
            for instrument_id, weight in self._pending_targets.items()
            if weight != 0.0
        }
        for instrument_id, target_weight in exit_targets.items():
            self._record_target(trading_date, signal_date, instrument_id, target_weight, "exit")
            submitted = self._submit_target_weight(trading_date, instrument_id, target_weight, "exit")
            if not submitted:
                self._exit_retry_pool.add(instrument_id)
        for instrument_id, target_weight in non_exit_targets.items():
            self._record_target(trading_date, signal_date, instrument_id, target_weight, "entry_or_target")
            if self._current_quantity(InstrumentId.from_str(instrument_id)) > 0:
                continue
            self._submit_target_weight(trading_date, instrument_id, target_weight, "entry_or_target")
        self._pending_targets = {}

    def _submit_target_weight(
        self,
        trading_date: date,
        instrument_id_text: str,
        target_weight: float,
        reason: str,
    ) -> bool:
        instrument_id = InstrumentId.from_str(instrument_id_text)
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self.log.error(f"Cannot submit target: missing instrument {instrument_id}")
            self._record_order(trading_date, instrument_id_text, "buy", 0, target_weight, "rejected", "missing_instrument")
            return False

        close_price = self._last_close.get(instrument_id_text)
        if close_price is None or close_price <= 0:
            self._record_order(trading_date, instrument_id_text, "buy", 0, target_weight, "rejected", "missing_price")
            return False

        current_qty = self._current_quantity(instrument_id)
        target_qty = self._target_quantity(instrument, close_price, target_weight)
        delta_qty = target_qty - current_qty
        if delta_qty == 0:
            self._record_order(trading_date, instrument_id_text, "buy", 0, target_weight, "skipped", "already_target")
            return True

        side = OrderSide.BUY if delta_qty > 0 else OrderSide.SELL
        qty_abs = abs(delta_qty)
        if qty_abs <= 0:
            return True
        order = self.order_factory.market(
            instrument_id=instrument_id,
            order_side=side,
            quantity=instrument.make_qty(qty_abs),
        )
        self.submit_order(order)
        side_text = "buy" if side == OrderSide.BUY else "sell"
        self._record_order(
            trading_date=trading_date,
            instrument_id=instrument_id_text,
            side=side_text,
            quantity=int(qty_abs),
            target_weight=target_weight,
            status="submitted",
            reason=reason,
            order_id=str(order.client_order_id),
        )
        self.log.info(
            f"Submitted {side_text} target {instrument_id_text} qty={qty_abs} target_weight={target_weight:.6f}",
            color=LogColor.BLUE,
        )
        return True

    def _target_quantity(self, instrument: Any, close_price: float, target_weight: float) -> Decimal:
        if target_weight <= 0:
            return Decimal("0")
        portfolio_value = self._portfolio_value()
        raw_qty = Decimal(str(portfolio_value * Decimal(str(target_weight)) / Decimal(str(close_price))))
        lot_size = Decimal(str(instrument.lot_size))
        if lot_size <= 0:
            lot_size = Decimal("1")
        return (raw_qty // lot_size) * lot_size

    def _portfolio_value(self) -> Decimal:
        venue = self._instrument_ids[0].venue if self._instrument_ids else None
        try:
            equity = self.portfolio.equity(venue=Venue(str(venue))) if venue is not None else self.portfolio.equity()
        except Exception:
            equity = {}
        if equity:
            first = next(iter(equity.values()))
            try:
                return Decimal(str(first.as_decimal()))
            except Exception:
                return Decimal(str(float(first)))
        return Decimal(str(self.config.initial_cash))

    def _current_quantity(self, instrument_id: InstrumentId) -> Decimal:
        try:
            qty = self.portfolio.net_position(instrument_id)
        except Exception:
            return Decimal("0")
        if qty is None:
            return Decimal("0")
        return Decimal(str(qty))

    def _current_weight(self, instrument_id_text: str) -> float | None:
        close_price = self._last_close.get(instrument_id_text)
        if close_price is None or close_price <= 0:
            return None
        portfolio_value = self._portfolio_value()
        if portfolio_value <= 0:
            return None
        qty = self._current_quantity(InstrumentId.from_str(instrument_id_text))
        return float(qty * Decimal(str(close_price)) / portfolio_value)

    def _update_trailing_state(
        self,
        state: dict[str, Any],
        close_price: float | None,
        cost_price: float,
    ) -> dict[str, Any]:
        result = {"triggered": False, "high_price": state.get("high_price"), "stop_price": None}
        if close_price is None or close_price <= 0:
            return result
        previous_high = state.get("high_price")
        high_price = max(close_price, float(previous_high or close_price))
        state["high_price"] = high_price
        result["high_price"] = high_price
        trailing_pct = float(self.config.trailing_take_profit)
        if trailing_pct <= 0 or cost_price <= 0:
            state["trailing_stop_price"] = None
            return result
        activation_pct = max(0.0, float(self.config.trailing_take_profit_start))
        if high_price < cost_price * (1.0 + activation_pct):
            state["trailing_stop_price"] = None
            return result
        stop_price = high_price * (1.0 - trailing_pct)
        state["trailing_stop_price"] = stop_price
        result["stop_price"] = stop_price
        result["triggered"] = close_price <= stop_price
        return result

    def _record_signal(
        self,
        signal_date: date,
        instrument_id: str,
        stock_code: str,
        signal_name: str,
        score: Any,
        rank: Any,
        side: str,
        selected: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.signal_events.append(
            ModelPredictionSignalEvent(
                signal_date=pd.Timestamp(signal_date).date(),
                instrument_id=instrument_id,
                stock_code=stock_code,
                signal_name=signal_name,
                score=None if score is None else float(score),
                rank=None if rank is None else int(rank),
                side=side,
                selected=selected,
                extra=extra or {},
            ),
        )

    def _record_target(
        self,
        trading_date: date,
        signal_date: date | None,
        instrument_id: str,
        target_weight: float,
        reason: str,
    ) -> None:
        current_weight = self._current_weight(instrument_id)
        delta_weight = None if current_weight is None else float(target_weight) - current_weight
        self.target_events.append(
            ModelPredictionTargetEvent(
                target_id=f"{trading_date.isoformat()}-{instrument_id}-{len(self.target_events)}",
                target_date=signal_date or trading_date,
                execute_date=trading_date,
                instrument_id=instrument_id,
                target_weight=float(target_weight),
                current_weight=current_weight,
                delta_weight=delta_weight,
                reason=reason,
                extra={"signal_date": None if signal_date is None else signal_date.isoformat()},
            ),
        )

    def _record_order(
        self,
        trading_date: date,
        instrument_id: str,
        side: str,
        quantity: int,
        target_weight: float,
        status: str,
        reason: str | None,
        order_id: str | None = None,
    ) -> None:
        self.order_events.append(
            ModelPredictionOrderEvent(
                order_id=order_id or f"internal-{trading_date.isoformat()}-{instrument_id}-{len(self.order_events)}",
                trading_date=trading_date,
                instrument_id=instrument_id,
                side=side,
                quantity=int(quantity),
                target_weight=float(target_weight),
                status=status,
                reason=reason,
                extra={"source": "strategy_internal"},
            ),
        )


def normalize_signals(raw: dict[str, list[dict[str, Any]]]) -> dict[date, list[dict[str, Any]]]:
    result: dict[date, list[dict[str, Any]]] = {}
    for key, signals in raw.items():
        signal_date = pd.Timestamp(key).date()
        normalized = []
        for signal in signals:
            normalized.append(
                {
                    "date": pd.Timestamp(signal["date"]).date(),
                    "stock_code": str(signal["stock_code"]),
                    "score": float(signal["score"]),
                    "rank": int(signal.get("rank", len(normalized) + 1)),
                    "avg_amount_20": signal.get("avg_amount_20"),
                },
            )
        result[signal_date] = normalized
    return result


def normalize_initial_last_closes(raw: dict[str, float] | None) -> dict[str, float]:
    result: dict[str, float] = {}
    for instrument_id, close in (raw or {}).items():
        try:
            close_value = float(close)
        except (TypeError, ValueError):
            continue
        if close_value > 0:
            result[str(instrument_id)] = close_value
    return result


def normalize_initial_active_positions(
    raw: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for instrument_id, state in (raw or {}).items():
        if not isinstance(state, dict):
            continue
        normalized = dict(state)
        for key in ("entry_price", "high_price", "score"):
            if normalized.get(key) is None:
                continue
            try:
                normalized[key] = float(normalized[key])
            except (TypeError, ValueError):
                normalized.pop(key, None)
        for key in ("entry_date", "last_signal_date"):
            if normalized.get(key) is None:
                continue
            try:
                normalized[key] = pd.Timestamp(normalized[key]).date()
            except (TypeError, ValueError):
                normalized.pop(key, None)
        result[str(instrument_id)] = normalized
    return result


def bar_date(bar: Bar, timezone_name: str) -> date:
    return pd.Timestamp(bar.ts_event, unit="ns", tz="UTC").tz_convert(timezone_name).date()


def previous_trading_date(trading_dates: list[date], current_date: date) -> date | None:
    dates = pd.DatetimeIndex(pd.to_datetime(trading_dates))
    index = int(dates.searchsorted(pd.Timestamp(current_date), side="left")) - 1
    if index < 0:
        return None
    return pd.Timestamp(dates[index]).date()


def first_trading_date_on_or_after(trading_dates: list[date], start_date: date | None) -> date | None:
    if start_date is None:
        return None
    dates = pd.DatetimeIndex(pd.to_datetime(trading_dates))
    index = int(dates.searchsorted(pd.Timestamp(start_date), side="left"))
    if index >= len(dates):
        return None
    return pd.Timestamp(dates[index]).date()


def is_rebalance_day(
    trading_dates: list[date],
    rebalance_start_date: date | None,
    current_date: date,
    holding_days: int,
) -> bool:
    if rebalance_start_date is None:
        return False
    if int(holding_days) <= 1:
        return True
    today = pd.Timestamp(current_date).date()
    start = pd.Timestamp(rebalance_start_date).date()
    if today < start:
        return False
    dates = pd.DatetimeIndex(pd.to_datetime(trading_dates))
    start_index = int(dates.searchsorted(pd.Timestamp(start), side="left"))
    end_index = int(dates.searchsorted(pd.Timestamp(today), side="left"))
    return max(0, end_index - start_index) % int(holding_days) == 0
