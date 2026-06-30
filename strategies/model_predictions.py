from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import timedelta
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
    excluded_name_prefixes: tuple[str, ...] = ("*ST", "ST", "退市")
    unfilled_timeout_secs: float = 30.0
    resubmit_check_interval_secs: float = 10.0
    # Fraction of free cash held back when sizing/gating buys, to absorb commission
    # and market-buy slippage above last close so a buy that "just fits" by close
    # price does not get rejected as 废单 (可用资金不足). 0.0 disables the buffer.
    cash_buffer_percent: float = 0.01


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
        self._order_submit_ts: dict[str, int] = {}
        # Buys that could not be funded from currently-free cash. They wait here
        # until a sell frees cash; the resubmit timer drains them as cash arrives.
        self._deferred_buys: dict[str, float] = {}
        # Client order ids the venue terminally rejected (e.g. QMT 废单 — insufficient
        # funds, price/volume validation). A rejected order is gone from the venue and
        # is NOT cancellable; track them so we never try to cancel or resubmit them.
        self._rejected_order_ids: set[str] = set()
        # Instruments whose last buy was rejected for insufficient funds — do not keep
        # resubmitting a buy that will only be rejected again until cash is available.
        self._insufficient_funds: set[str] = set()
        self._processed_dates: set[date] = set()
        self._rebalance_start_date = first_trading_date_on_or_after(
            self._trading_dates,
            pd.Timestamp(config.trading_dates[0]).date() if config.trading_dates else None,
        )
        self.signal_events: list[ModelPredictionSignalEvent] = []
        self.target_events: list[ModelPredictionTargetEvent] = []
        self.order_events: list[ModelPredictionOrderEvent] = []

    def on_start(self) -> None:
        self.log.info(
            f"on_start: subscribing bars instruments={len(self._instrument_ids)} "
            f"bar_types={len(self._bar_types)} signal_dates={len(self._signals_by_date)} "
            f"trading_dates={len(self._trading_dates)} last_closes={len(self._last_close)} "
            f"rebalance_start_date={self._rebalance_start_date}",
            color=LogColor.BLUE,
        )
        if not self._bar_types:
            self.log.warning("on_start: no bar_types configured — no bars will arrive, no orders will be made")
        for bar_type in self._bar_types.values():
            self.subscribe_bars(bar_type)
        interval_secs = float(self.config.resubmit_check_interval_secs)
        if float(self.config.unfilled_timeout_secs) > 0 and interval_secs > 0:
            self.clock.set_timer(
                name="MODEL-PREDICTION-RESUBMIT",
                interval=timedelta(seconds=interval_secs),
                callback=self._on_resubmit_timer,
                fire_immediately=False,
            )

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

        signal_date = self._resolve_signal_date(trading_date)
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
        self.log.info(
            f"_process_trading_day {trading_date}: signal_date={signal_date} "
            f"signals_for_signal_date={len(today_signals)} "
            f"mapped_targets={len(target_ids)} rebalance_today={rebalance_today} "
            f"active_positions={len(self._active_positions)}",
            color=LogColor.BLUE,
        )
        if signal_date is None:
            self.log.warning(
                f"_process_trading_day {trading_date}: no previous trading date found "
                f"(trading_dates range may not cover {trading_date}) — no entry signals",
            )
        elif not today_signals:
            self.log.warning(
                f"_process_trading_day {trading_date}: zero signals for signal_date={signal_date} "
                f"— available signal_dates={sorted(self._signals_by_date)[-5:]}",
            )
        self._prepare_exits(trading_date, signal_date, target_ids, rebalance_today)
        self._prepare_entries_and_refresh(trading_date, today_signals)
        self._trim_active_positions()
        self._set_equal_weight_targets()
        self.log.info(
            f"_process_trading_day {trading_date}: pending_targets={len(self._pending_targets)} "
            f"(exits={sum(1 for w in self._pending_targets.values() if w == 0.0)} "
            f"entries={sum(1 for w in self._pending_targets.values() if w != 0.0)})",
            color=LogColor.BLUE,
        )
        self._submit_pending_targets(trading_date, signal_date)

    def _resolve_signal_date(self, trading_date: date) -> date | None:
        """
        Choose which signal date drives entries for ``trading_date``.

        The strategy trades on the previous trading day's predictions, so the
        primary choice is ``previous_trading_date``. That exact lookup is what we
        want in a backtest, where every replayed day has its own prior-day
        signals available.

        In live trading the most recent prediction may lag the real previous
        trading day (e.g. the predictions table has not been refreshed yet), so a
        strict prior-day lookup yields zero entries and the strategy never trades.
        Mirror the fallback in ``live_qmt_model_predictions.subscription_signal_date``:
        when no signals exist for the prior day, use the most recent signal date
        on or before it. This only ever looks backwards, so it stays correct for
        backtests — it can never pull a future signal, and when the exact
        prior-day signals exist they are still preferred.
        """
        prev_date = previous_trading_date(self._trading_dates, trading_date)
        if prev_date is not None and prev_date in self._signals_by_date:
            return prev_date
        # Fall back to the latest available signal date <= the prior trading day
        # (or <= trading_date itself if the prior day couldn't be resolved).
        cutoff = prev_date or trading_date
        candidates = [value for value in self._signals_by_date if value <= cutoff]
        if candidates:
            fallback = max(candidates)
            self.log.info(
                f"_resolve_signal_date {trading_date}: no signals for prior trading "
                f"day {prev_date}, falling back to latest available {fallback}",
            )
            return fallback
        return prev_date

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
        self.log.info(
            f"_prepare_entries {trading_date}: candidates={len(signals)} "
            f"active={len(active_ids)} available_slots={available_slots} "
            f"max_positions={self.config.max_positions}",
        )
        drop_no_instrument = 0
        drop_pending_exit = 0
        drop_no_price = 0
        drop_no_slots = 0
        entry_rank = 0
        for signal in signals:
            stock_code = signal["stock_code"]
            instrument = self._instrument_by_stock.get(stock_code)
            if instrument is None:
                drop_no_instrument += 1
                continue
            instrument_id = str(instrument)
            if self._pending_targets.get(instrument_id) == 0.0:
                drop_pending_exit += 1
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
                drop_no_price += 1
                continue
            state = self._active_positions.get(instrument_id)
            if state is None:
                if available_slots <= 0:
                    drop_no_slots += 1
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
        if drop_no_instrument or drop_pending_exit or drop_no_price or drop_no_slots:
            self.log.info(
                f"_prepare_entries {trading_date}: selected={entry_rank} dropped "
                f"no_instrument={drop_no_instrument} pending_exit={drop_pending_exit} "
                f"no_price={drop_no_price} no_slots={drop_no_slots}",
            )

    def _entry_skip_reason(self, stock_code: str, trading_date: date) -> str | None:
        name_reason = self._name_skip_reason(stock_code)
        if name_reason:
            return name_reason
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

    def _name_skip_reason(self, stock_code: str) -> str | None:
        instrument_id = self._instrument_by_stock.get(stock_code)
        if instrument_id is None:
            return None
        name = self._instrument_name(str(instrument_id)).strip()
        if not name:
            return None
        # Check the more specific prefix first so "*ST" is not reported as "st".
        for prefix in sorted(self.config.excluded_name_prefixes, key=len, reverse=True):
            if prefix and name.startswith(prefix):
                if prefix.endswith("ST"):
                    return "st_name"
                return "delisting"
        return None

    def _instrument_name(self, instrument_id_text: str) -> str:
        try:
            instrument = self.cache.instrument(InstrumentId.from_str(instrument_id_text))
        except Exception:
            return ""
        if instrument is None:
            return ""
        info = getattr(instrument, "info", None)
        if not isinstance(info, dict):
            return ""
        return str(info.get("name", "") or "")

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

    def _equal_weight_percent(self) -> float:
        active_count = len(self._active_positions)
        if active_count <= 0:
            return 0.0
        return min(float(self.config.max_position_percent), 1.0 / active_count)

    def _set_equal_weight_targets(self) -> None:
        active_ids = sorted(self._active_positions)
        if not active_ids:
            return
        target_percent = self._equal_weight_percent()
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
        # Sells first: they release the cash the buys below need. The buys are then
        # gated on free cash and any that don't fit are deferred until a sell fills
        # (drained by the resubmit timer), so we never fire a buy the account can't
        # fund and trigger a 废单 (可用资金不足).
        for instrument_id, target_weight in exit_targets.items():
            self._record_target(trading_date, signal_date, instrument_id, target_weight, "exit")
            submitted = self._submit_target_weight(trading_date, instrument_id, target_weight, "exit")
            if not submitted:
                self._exit_retry_pool.add(instrument_id)

        # A fresh rebalance supersedes any buys left deferred from a previous one.
        self._deferred_buys = {}
        buy_candidates: dict[str, float] = {}
        for instrument_id, target_weight in non_exit_targets.items():
            self._record_target(trading_date, signal_date, instrument_id, target_weight, "entry_or_target")
            if self._current_quantity(InstrumentId.from_str(instrument_id)) > 0:
                continue
            buy_candidates[instrument_id] = target_weight
        self._submit_buys_within_cash(trading_date, buy_candidates, "entry_or_target")
        self._pending_targets = {}

    def _submit_buys_within_cash(
        self,
        trading_date: date,
        buy_candidates: dict[str, float],
        reason: str,
    ) -> None:
        """
        Submit buys in target-weight order while free cash covers each one; defer
        the rest into ``_deferred_buys`` so the resubmit timer can drain them once a
        sell frees cash. This is the single funnel for new buys — same-tick rebalance
        buys and resubmitted deferred buys both pass through here, so the free-cash
        check is always enforced.
        """
        if not buy_candidates:
            return
        free_cash = self._free_cash()
        buffer_pct = max(0.0, float(self.config.cash_buffer_percent))
        if buffer_pct > 0:
            free_cash = free_cash * Decimal(str(1.0 - min(buffer_pct, 1.0)))
        # Highest target weight first so the most-wanted names get funded earliest.
        for instrument_id in sorted(buy_candidates, key=lambda i: buy_candidates[i], reverse=True):
            target_weight = buy_candidates[instrument_id]
            est_cost = self._estimated_buy_cost(instrument_id, target_weight)
            if est_cost is None:
                # No price yet — let _submit_target_weight log/record the skip.
                self._submit_target_weight(trading_date, instrument_id, target_weight, reason)
                continue
            if est_cost > free_cash:
                self._deferred_buys[instrument_id] = target_weight
                self.log.info(
                    f"Deferred buy {instrument_id} weight={target_weight:.6f} "
                    f"est_cost={est_cost} > free_cash={free_cash} — waiting for cash",
                    color=LogColor.BLUE,
                )
                self._record_order(
                    trading_date, instrument_id, "buy", 0, target_weight, "deferred", "insufficient_cash",
                )
                continue
            submitted = self._submit_target_weight(trading_date, instrument_id, target_weight, reason)
            if submitted and est_cost > 0:
                free_cash -= est_cost

    def _estimated_buy_cost(self, instrument_id_text: str, target_weight: float) -> Decimal | None:
        instrument_id = InstrumentId.from_str(instrument_id_text)
        instrument = self.cache.instrument(instrument_id)
        close_price = self._last_close.get(instrument_id_text)
        if instrument is None or close_price is None or close_price <= 0:
            return None
        current_qty = self._current_quantity(instrument_id)
        target_qty = self._target_quantity(instrument, close_price, target_weight)
        delta_qty = target_qty - current_qty
        if delta_qty <= 0:
            return Decimal("0")
        return Decimal(str(delta_qty)) * Decimal(str(close_price))

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
            self.log.warning(
                f"_submit_target {instrument_id_text}: missing/invalid price "
                f"close={close_price} weight={target_weight:.6f} reason={reason} — order skipped",
            )
            self._record_order(trading_date, instrument_id_text, "buy", 0, target_weight, "rejected", "missing_price")
            return False

        current_qty = self._current_quantity(instrument_id)
        target_qty = self._target_quantity(instrument, close_price, target_weight)
        delta_qty = target_qty - current_qty
        if delta_qty == 0:
            self.log.info(
                f"_submit_target {instrument_id_text}: no-op delta_qty=0 "
                f"current={current_qty} target={target_qty} weight={target_weight:.6f} "
                f"close={close_price} reason={reason}",
            )
            self._record_order(trading_date, instrument_id_text, "buy", 0, target_weight, "skipped", "already_target")
            return True

        side = OrderSide.BUY if delta_qty > 0 else OrderSide.SELL
        qty_abs = abs(delta_qty)
        if qty_abs <= 0:
            self.log.warning(
                f"_submit_target {instrument_id_text}: qty_abs<=0 "
                f"current={current_qty} target={target_qty} weight={target_weight:.6f} "
                f"close={close_price} portfolio_value={self._portfolio_value()} reason={reason}",
            )
            return True
        order = self.order_factory.market(
            instrument_id=instrument_id,
            order_side=side,
            quantity=instrument.make_qty(qty_abs),
        )
        self.submit_order(order)
        self._order_submit_ts[str(order.client_order_id)] = self.clock.timestamp_ns()
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

    def on_order_filled(self, event: Any) -> None:
        client_order_id = str(event.client_order_id)
        self._order_submit_ts.pop(client_order_id, None)
        self._deferred_buys.pop(str(getattr(event, "instrument_id", "")), None)
        # A fill (typically a sell) frees cash — clear the insufficient-funds flag so
        # deferred/blocked buys can be retried on the next resubmit tick.
        self._insufficient_funds.discard(str(getattr(event, "instrument_id", "")))

    def on_order_canceled(self, event: Any) -> None:
        self._order_submit_ts.pop(str(event.client_order_id), None)

    def on_order_rejected(self, event: Any) -> None:
        client_order_id = str(event.client_order_id)
        self._order_submit_ts.pop(client_order_id, None)
        # A rejected order (QMT 废单) is terminal and NOT cancellable. Remember it so
        # the reconcile loop never tries to cancel or resubmit it.
        self._rejected_order_ids.add(client_order_id)
        instrument_id_text = str(getattr(event, "instrument_id", ""))
        reason = str(getattr(event, "reason", "") or "")
        if _is_insufficient_funds(reason):
            # Don't keep resubmitting a buy that will only be rejected again; wait for
            # a sell to free cash (on_order_filled clears this). Keep the intent in
            # _deferred_buys so the next funded resubmit tick can pick it back up.
            if instrument_id_text:
                self._insufficient_funds.add(instrument_id_text)
            self.log.warning(
                f"Order {client_order_id} {instrument_id_text} rejected for insufficient "
                f"funds — will retry after cash frees up. reason={reason}",
            )

    def _on_resubmit_timer(self, _event: Any) -> None:
        try:
            self._reconcile_unfilled_orders()
        except Exception as exc:
            self.log.warning(f"Unfilled-order reconcile failed: {exc}")

    def _reconcile_unfilled_orders(self) -> None:
        """
        Cancel orders that stay unfilled past the timeout, then resubmit toward the
        desired target for the instruments whose order was just canceled.

        The desired weight is derived from ``_active_positions`` (equal-weight if
        still active, otherwise ``0`` for an in-progress exit) — the same source the
        trading-day logic uses. Crucially this method does NOT re-seed
        ``_active_positions`` from the portfolio: within a session ``_active_positions``
        already reflects exits, and after a process restart it is empty here, so the
        reconciler simply cancels stale orders and lets the next bar's
        ``_process_trading_day`` rebuild state and recompute targets. That makes a
        restart inert — it can never introduce an unexpected position change.
        """
        timeout_secs = float(self.config.unfilled_timeout_secs)
        if timeout_secs <= 0:
            return
        now = self.clock.timestamp_ns()
        timeout_ns = int(timeout_secs * 1_000_000_000)
        trading_date = self.clock.utc_now().date()
        try:
            open_orders = self.cache.orders_open(strategy_id=self.id)
        except Exception:
            open_orders = []

        canceled_instruments: set[str] = set()
        instruments_with_open_order: set[str] = set()
        for order in open_orders:
            instrument_id_text = str(order.instrument_id)
            client_order_id = str(order.client_order_id)
            # A venue-rejected order (QMT 废单) is terminal and cannot be canceled.
            # It should already be gone from orders_open(), but guard defensively so
            # we never fire a doomed cancel against it.
            if client_order_id in self._rejected_order_ids:
                continue
            try:
                if order.is_pending_cancel:
                    instruments_with_open_order.add(instrument_id_text)
                    continue
            except Exception:
                pass
            submit_ts = self._order_submit_ts.get(client_order_id)
            if submit_ts is None:
                submit_ts = int(getattr(order, "ts_last", now) or now)
            if now - submit_ts < timeout_ns:
                instruments_with_open_order.add(instrument_id_text)
                continue
            try:
                self.cancel_order(order)
            except Exception as exc:
                self.log.warning(f"cancel_order failed for {client_order_id}: {exc}")
                instruments_with_open_order.add(instrument_id_text)
                continue
            self._order_submit_ts.pop(client_order_id, None)
            instruments_with_open_order.add(instrument_id_text)
            canceled_instruments.add(instrument_id_text)
            self.log.info(
                f"Canceled unfilled order {client_order_id} {instrument_id_text} "
                f"after {timeout_secs:.0f}s — will resubmit toward target",
                color=LogColor.BLUE,
            )
            self._record_order(
                trading_date=trading_date,
                instrument_id=instrument_id_text,
                side="buy" if order.side == OrderSide.BUY else "sell",
                quantity=0,
                target_weight=0.0,
                status="canceled",
                reason="unfilled_timeout",
                order_id=client_order_id,
            )

        # Resubmit only for instruments whose stale order we just canceled and that have
        # no other open order in flight. Targets come from current _active_positions, so
        # exits resubmit as sells and entries as buys — and the delta-to-target math is a
        # no-op once the position already matches, making this safe to repeat.
        for instrument_id_text in sorted(canceled_instruments):
            if instrument_id_text in instruments_with_open_order.difference(canceled_instruments):
                continue
            self._resubmit_toward_target(trading_date, instrument_id_text)

        # Drain buys that were deferred for lack of cash. Sells that have since filled
        # freed cash, so retry them through the same free-cash funnel — any that still
        # don't fit stay deferred for the next tick. Skip instruments with an order in
        # flight or still flagged insufficient-funds (no fill has freed cash yet).
        if self._deferred_buys:
            retryable = {
                instrument_id: weight
                for instrument_id, weight in self._deferred_buys.items()
                if instrument_id not in instruments_with_open_order
                and instrument_id not in self._insufficient_funds
                and self._current_quantity(InstrumentId.from_str(instrument_id)) <= 0
            }
            self._deferred_buys = {
                instrument_id: weight
                for instrument_id, weight in self._deferred_buys.items()
                if instrument_id not in retryable
            }
            self._submit_buys_within_cash(trading_date, retryable, "resubmit_deferred")

    def _resubmit_toward_target(self, trading_date: date, instrument_id_text: str) -> None:
        instrument_id = InstrumentId.from_str(instrument_id_text)
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            return
        # Desired weight: equal-weight if active, otherwise 0 (exit).
        if instrument_id_text in self._active_positions:
            target_weight = self._equal_weight_percent()
        else:
            target_weight = 0.0
        close_price = self._last_close.get(instrument_id_text)
        if close_price is None or close_price <= 0:
            return
        current_qty = self._current_quantity(instrument_id)
        target_qty = self._target_quantity(instrument, close_price, target_weight)
        delta_qty = target_qty - current_qty
        if delta_qty == 0:
            return
        side = OrderSide.BUY if delta_qty > 0 else OrderSide.SELL
        qty_abs = abs(delta_qty)
        if qty_abs <= 0:
            return
        # Route resubmitted buys through the free-cash gate so the timer can never
        # fire an unfundable buy and trigger a 废单. Sells (exits) go straight through —
        # they free cash and were canceled precisely to be retried.
        if side == OrderSide.BUY:
            self._submit_buys_within_cash(
                trading_date,
                {instrument_id_text: float(target_weight)},
                "resubmit_unfilled",
            )
            return
        order = self.order_factory.market(
            instrument_id=instrument_id,
            order_side=side,
            quantity=instrument.make_qty(qty_abs),
        )
        self.submit_order(order)
        self._order_submit_ts[str(order.client_order_id)] = self.clock.timestamp_ns()
        side_text = "buy" if side == OrderSide.BUY else "sell"
        self._record_order(
            trading_date=trading_date,
            instrument_id=instrument_id_text,
            side=side_text,
            quantity=int(qty_abs),
            target_weight=float(target_weight),
            status="submitted",
            reason="resubmit_unfilled",
            order_id=str(order.client_order_id),
        )
        self.log.info(
            f"Resubmitted {side_text} {instrument_id_text} qty={qty_abs} toward "
            f"target_qty={target_qty} (current={current_qty})",
            color=LogColor.BLUE,
        )

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

    def _free_cash(self) -> Decimal:
        """
        Return the cash available to fund new buys.

        Sells release cash only once they fill (and, for the QMT proxy, once that
        fill is polled back into the account state), so buys must be sized against
        this free balance rather than total equity — otherwise several buys each
        sized against full equity overrun the cash and the venue rejects them as
        废单 (可用资金不足). Falls back to total equity when no account/balance is
        available (e.g. before the first account-state update).
        """
        venue = self._instrument_ids[0].venue if self._instrument_ids else None
        try:
            account = (
                self.portfolio.account(venue=Venue(str(venue)))
                if venue is not None
                else self.portfolio.account()
            )
        except Exception:
            account = None
        if account is not None:
            try:
                free = account.balance_free()
            except Exception:
                free = None
            if free is not None:
                try:
                    return Decimal(str(free.as_decimal()))
                except Exception:
                    return Decimal(str(float(free)))
        return self._portfolio_value()

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


# QMT counter error 260200 is "可用资金不足" (insufficient available funds). Match the
# code and the Chinese phrase so a wording change on either side still triggers the
# cash-aware backoff.
_INSUFFICIENT_FUNDS_MARKERS = ("260200", "可用资金不足", "资金不足", "insufficient")


def _is_insufficient_funds(reason: str) -> bool:
    text = (reason or "").lower()
    return any(marker.lower() in text for marker in _INSUFFICIENT_FUNDS_MARKERS)


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
