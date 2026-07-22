from __future__ import annotations

import dataclasses
from datetime import date
from datetime import timedelta
from typing import Any
from typing import Callable

import pandas as pd

from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from strategies.model_common import ModelPredictionSignalEvent
from strategies.model_common import normalize_initial_active_positions
from strategies.model_common import normalize_signals
from strategies.model_common import previous_trading_date
from strategies.model_target_planners import CurrentHolding
from strategies.model_target_planners import ModelTargetCandidate
from strategies.model_target_planners import ModelTargetPlan
from strategies.model_target_planners import ModelTargetPlanningRequest
from strategies.model_target_planners import build_model_target_planner
from strategies.model_target_planners import normalize_stock_code
from strategies.target_quantities import TargetQuantityStrategy
from strategies.target_quantities import TargetQuantityStrategyConfig
from strategies.target_quantities import bar_date


class TargetModelPredictionsStrategyConfig(TargetQuantityStrategyConfig, kw_only=True, frozen=True):
    instrument_stock_codes: dict[str, str]
    signals_by_date: dict[str, list[dict[str, Any]]]
    trading_dates: list[str]
    listed_dates: dict[str, str]
    st_by_date: dict[str, list[str]]
    suspended_by_date: dict[str, list[str]]
    max_positions: int = 50
    max_position_percent: float = 0.03
    holding_days: int = 10
    stop_loss: float = 0.05
    trailing_take_profit: float = 0.0
    trailing_take_profit_start: float = 0.0
    min_listed_days: int = 120
    initial_active_positions: dict[str, dict[str, Any]] | None = None
    excluded_name_prefixes: tuple[str, ...] = ("*ST", "ST", "\u9000\u5e02")
    target_weight_planner: str = "equal_weight"
    target_weight_planner_error_policy: str = "raise"
    risk_manager_base_url: str = ""
    risk_manager_risk_model_id: str = ""
    risk_manager_mode: str = "simulation"
    risk_manager_timeout_secs: float = 10.0
    process_targets_on_timer: bool = False
    process_targets_interval_secs: float = 10.0


class TargetModelPredictionsStrategy(TargetQuantityStrategy):
    """
    Model-prediction target provider using the reusable target-quantity executor.

    This class decides the target share counts (via the risk-manager planner). The
    inherited executor decides how to reach them through Nautilus account, cache, and
    order APIs.
    """

    _PROCESS_TARGETS_TIMER = "TARGET-MODEL-PROCESS-TARGETS"

    def __init__(self, config: TargetModelPredictionsStrategyConfig) -> None:
        super().__init__(config)
        self._stock_by_instrument = {
            str(instrument_id): stock_code
            for instrument_id, stock_code in config.instrument_stock_codes.items()
        }
        self._instrument_by_stock = {
            stock_code: InstrumentId.from_str(instrument_id)
            for instrument_id, stock_code in self._stock_by_instrument.items()
        }
        self._signals_by_date = normalize_signals(config.signals_by_date)
        self._latest_signal_by_stock_date = latest_signal_index(self._signals_by_date)
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
        self._active_positions = normalize_initial_active_positions(config.initial_active_positions)
        self._processed_dates: set[date] = set()
        self._target_planner = build_model_target_planner(config)
        self._live_target_portfolio_loader: Callable[[date, date | None], list[dict[str, Any]]] | None = None
        self._recent_target_loader: Callable[[date, date, list[str]], dict[str, date]] | None = None
        self.signal_events: list[ModelPredictionSignalEvent] = []

    def configure_live_target_portfolio_loader(
        self,
        loader: Callable[[date, date | None], list[dict[str, Any]]] | None,
    ) -> None:
        """
        Inject a loader for persisted daily live targets.

        The strategy remains storage-agnostic: live wiring owns MySQL access and
        provides rows from ``live_target_portfolio``. Backtests leave this unset and
        use the computed plan path.
        """
        self._live_target_portfolio_loader = loader

    def configure_recent_target_loader(
        self,
        loader: Callable[[date, date, list[str]], dict[str, date]] | None,
    ) -> None:
        """
        Inject a loader for the most-recent positive-target date per held stock.

        Signature: ``loader(trading_date, cutoff_trade_date, stock_codes) ->
        {stock_code: recent_target_date}``. Live wiring queries ``live_target_portfolio``
        for the last ``target_qty > 0`` date (before_trading preferred) within the
        window ``[cutoff_trade_date, trading_date)``. Backtests leave this unset and
        fall back to each position's entry date.
        """
        self._recent_target_loader = loader

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
        self.refresh_target_instruments(
            instrument_ids=instrument_ids,
            bar_types=bar_types,
            last_closes=last_closes,
            subscribe_new_bars=subscribe_new_bars,
            unsubscribe_removed_bars=unsubscribe_removed_bars,
        )
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
        self._latest_signal_by_stock_date = latest_signal_index(self._signals_by_date)
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
        try:
            today = pd.Timestamp(self.clock.utc_now()).tz_convert(self.config.timezone_name).date()
        except Exception:
            return
        self._processed_dates.discard(today)
        if self._within_trading_window():
            self._process_trading_day_once(today, "refresh")

    def on_target_bar(self, bar: Bar) -> None:
        trading_date = bar_date(bar, self.config.timezone_name)
        self._process_trading_day_once(trading_date, "bar")

    def on_start(self) -> None:
        super().on_start()
        self._start_process_targets_timer()

    def _start_process_targets_timer(self) -> None:
        interval_secs = float(self.config.process_targets_interval_secs)
        if bool(self.config.process_targets_on_timer) and interval_secs > 0:
            self.clock.set_timer(
                name=self._PROCESS_TARGETS_TIMER,
                interval=timedelta(seconds=interval_secs),
                callback=self._on_process_targets_timer,
                fire_immediately=False,
            )

    def _on_process_targets_timer(self, _event: Any) -> None:
        if self._within_trading_window():
            trading_date = self._clock_date()
            self._process_trading_day_once(trading_date, "timer")

    def _process_trading_day_once(self, trading_date: date, trigger: str) -> bool:
        if trading_date in self._processed_dates:
            return False
        self._process_trading_day(trading_date)
        self._processed_dates.add(trading_date)
        self.log.info(
            f"processed model target day from {trigger}: date={trading_date}",
            color=LogColor.BLUE,
        )
        return True

    def _process_trading_day(self, trading_date: date) -> None:
        loaded_target = self._live_target_portfolio_target(trading_date)
        if loaded_target is not None:
            quantities, reason, version = loaded_target
            self.update_target_quantities(
                quantities=quantities,
                target_date=trading_date,
                reason=reason,
                version=version,
            )
            return
        plan = self.compute_daily_target_plan(trading_date)
        # The risk-manager planner commits explicit share counts (固定目标股数); the
        # executor trades toward those quantities. Weights on the plan are audit-only
        # and are not consulted for execution.
        self.update_target_quantities(
            quantities=plan.target_qty,
            target_date=trading_date,
            reason=plan.reason,
            version=self._plan_version(plan),
        )

    def _live_target_portfolio_target(
        self,
        trading_date: date,
    ) -> tuple[dict[str, int], str, str | None] | None:
        loader = self._live_target_portfolio_loader
        if loader is None:
            self._log_live_target_portfolio_info(
                f"live target portfolio loader is not configured: date={trading_date}",
            )
            return None
        signal_date = self._resolve_signal_date(trading_date)
        rows = loader(trading_date, signal_date)
        if not rows:
            self._log_live_target_portfolio_info(
                f"live target portfolio not found: date={trading_date} signal_date={signal_date}",
            )
            return None
        quantities, reason, version = self._target_quantities_from_live_target_rows(rows)
        self._log_live_target_portfolio_info(
            f"loaded live target portfolio: date={trading_date} signal_date={signal_date} "
            f"frozen_qty={len(quantities)} version={version}",
            color=LogColor.GREEN,
        )
        return quantities, reason, version

    def _log_live_target_portfolio_info(
        self,
        message: str,
        color: LogColor = LogColor.BLUE,
    ) -> None:
        if self.log is None:
            return
        self.log.info(message, color=color)

    @staticmethod
    def _target_quantities_from_live_target_rows(
        rows: list[dict[str, Any]],
    ) -> tuple[dict[str, int], str, str | None]:
        quantities: dict[str, int] = {}
        reason = "loaded_target"
        version: str | None = None
        for row in rows:
            instrument_id = str(row["instrument_id"] or "").strip()
            if not instrument_id:
                raise RuntimeError("live_target_portfolio row has empty instrument_id")
            qty = row.get("target_qty")
            if qty is None:
                continue
            quantity = int(qty)
            if quantity < 0:
                raise RuntimeError(
                    f"live_target_portfolio row has negative target_qty: instrument_id={instrument_id}",
                )
            quantities[instrument_id] = quantity
            if version is None and row.get("target_version"):
                version = str(row["target_version"])
            if row.get("reason"):
                reason = str(row["reason"])
        if not quantities:
            raise RuntimeError("live_target_portfolio rows contain no target_qty values")
        return quantities, reason, version

    def compute_daily_target_plan(self, trading_date: date) -> ModelTargetPlan:
        """
        Build the risk-manager request for the day and return the resulting plan
        **without submitting orders or accepting the target**.

        Two independent inputs are built and filtered separately (no merge):

        * **candidates** — the signal stocks, minus entry-ineligible ones (name
          prefixes, ST, suspension, minimum listed days, missing sizing price).
        * **current_holdings** — the held positions, minus the untradable ones and the
          ones a local hard-exit rule (stop-loss / trailing take-profit) fires on, so
          the optimizer no longer sees them as current state and unwinds them.

        The bar/timer path (_process_trading_day) uses this and then applies the plan
        via update_target_quantities. The snapshot recorder uses it before-trading to
        derive the day's frozen share counts. Both paths therefore run the same logic.
        """
        self._seed_active_positions_from_portfolio(trading_date)
        signal_date = self._resolve_signal_date(trading_date)
        today_signals = self._signals_by_date.get(signal_date, []) if signal_date else []
        self.log.info(
            f"model target day {trading_date}: signal_date={signal_date} "
            f"signals={len(today_signals)} active={len(self._active_positions)}",
            color=LogColor.BLUE,
        )
        return self._target_plan(trading_date, signal_date)

    def plan_version(self, plan: ModelTargetPlan) -> str:
        """Public alias of the version string used by update_target_quantities."""
        return self._plan_version(plan)

    def _resolve_signal_date(self, trading_date: date) -> date | None:
        prev_date = previous_trading_date(self._trading_dates, trading_date)
        if prev_date is not None and prev_date in self._signals_by_date:
            return prev_date
        cutoff = prev_date or trading_date
        candidates = [value for value in self._signals_by_date if value <= cutoff]
        if candidates:
            return max(candidates)
        return prev_date

    def _seed_active_positions_from_portfolio(self, trading_date: date) -> None:
        try:
            open_positions = self.cache.positions_open()
        except Exception:
            open_positions = []
        for position in open_positions:
            try:
                if not position.is_long:
                    continue
                instrument_id = position.instrument_id
            except Exception:
                continue
            instrument_id_text = str(instrument_id)
            if instrument_id_text in self._active_positions:
                continue
            if self._current_quantity(instrument_id) <= 0:
                continue
            close_price = self._last_close.get(instrument_id_text)
            try:
                avg_px_open = float(position.avg_px_open)
            except Exception:
                avg_px_open = 0.0
            entry_price = avg_px_open if avg_px_open > 0 else close_price
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

    def _latest_signal_state(self, stock_code: str, trading_date: date) -> dict[str, Any]:
        latest_date = None
        latest_signal = None
        for signal_date, signal in self._latest_signal_by_stock_date.get(stock_code, []):
            if signal_date > trading_date:
                break
            latest_date = signal_date
            latest_signal = signal
        if latest_signal is None:
            return {}
        return {
            "last_signal_date": latest_date,
            "score": float(latest_signal.get("score", 0.0)),
        }

    def _holding_exclusion(
        self,
        trading_date: date,
        signal_date: date | None,
        instrument_id: str,
        exit_rank: int,
    ) -> str | None:
        """
        Decide whether a currently-held position must be **excluded** from the
        risk-manager ``current_holdings`` so the optimizer unwinds it.

        Two exclusion families, both unchanged in intent from the old exit logic:

        * **untradable** — the stock is suspended or ST today (can't be traded, so it
          is not offered as current state).
        * **local hard-exit** — stop-loss or trailing-take-profit fires against the
          entry/high-water state tracked in ``_active_positions``.

        Updates the position's trailing state as a side effect (the daily high-water
        mark must advance even on days the position is kept) and records a sell
        ``signal_event`` when it excludes. Returns the exclusion reason, or ``None`` to
        keep the holding.
        """
        state = self._active_positions.get(instrument_id, {})
        exit_price, price_source = self._exit_price_with_source(instrument_id)
        if price_source == "prev_close":
            self._log_missing_exit_open_price(
                trading_date=trading_date,
                instrument_id=instrument_id,
                stock_code=self._stock_by_instrument.get(instrument_id, ""),
                fallback_price=exit_price,
            )
        cost_price = float(state.get("entry_price") or exit_price or 0.0)
        trailing = self._update_trailing_state(state, exit_price, cost_price)
        stock_code = normalize_stock_code(self._stock_by_instrument.get(instrument_id))
        untradable = self._untradable_reason(stock_code, trading_date)
        stop_triggered = (
            exit_price is not None
            and cost_price > 0
            and exit_price <= cost_price * (1.0 - self.config.stop_loss)
        )
        trailing_triggered = bool(trailing["triggered"])
        if untradable:
            signal_name = untradable
        elif stop_triggered:
            signal_name = "stop_triggered"
        elif trailing_triggered:
            signal_name = "trailing_take_profit_triggered"
        else:
            return None
        self._record_signal(
            signal_date=signal_date or trading_date,
            instrument_id=instrument_id,
            stock_code=self._stock_by_instrument.get(instrument_id, ""),
            signal_name=signal_name,
            score=state.get("score"),
            rank=exit_rank,
            side="sell",
            extra={
                "open_price": exit_price,
                "price_source": price_source,
                "entry_price": cost_price,
                "high_price": trailing["high_price"],
                "trailing_stop_price": trailing["stop_price"],
            },
        )
        return signal_name

    def _untradable_reason(self, stock_code: str, trading_date: date) -> str | None:
        if stock_code in self._suspended_by_date.get(trading_date, set()):
            return "suspended"
        if stock_code in self._st_by_date.get(trading_date, set()):
            return "st"
        return None

    def _exit_price_with_source(self, instrument_id_text: str) -> tuple[float | None, str | None]:
        open_price = self._today_open_price(instrument_id_text)
        if open_price is not None:
            return open_price, "open"
        close_price = self._last_close.get(instrument_id_text)
        if close_price is not None and close_price > 0:
            return float(close_price), "prev_close"
        return None, None

    def _log_missing_exit_open_price(
        self,
        trading_date: date,
        instrument_id: str,
        stock_code: str,
        fallback_price: float | None,
    ) -> None:
        if self.log is None:
            return
        self.log.warning(
            f"model target exit using previous close: missing open price "
            f"date={trading_date} instrument_id={instrument_id} "
            f"stock_code={stock_code} fallback_price={fallback_price}",
            color=LogColor.YELLOW,
        )

    def _today_open_price(self, instrument_id_text: str) -> float | None:
        open_price = self._today_open.get(instrument_id_text)
        if open_price is None or open_price <= 0:
            return None
        return float(open_price)

    def _log_missing_new_entry_open_price(
        self,
        trading_date: date,
        signal_date: date,
        instrument_id: str,
        stock_code: str,
    ) -> None:
        if self.log is None:
            return
        self.log.warning(
            f"skipping model prediction candidate: missing open price "
            f"date={trading_date} signal_date={signal_date} "
            f"instrument_id={instrument_id} stock_code={stock_code}",
            color=LogColor.YELLOW,
        )

    def _target_plan(self, trading_date: date, signal_date: date | None) -> ModelTargetPlan:
        request = self._target_planning_request(trading_date, signal_date)
        plan = self._target_planner.plan(request)
        return self._annotate_plan(plan, request)

    def _annotate_plan(
        self,
        plan: ModelTargetPlan,
        request: ModelTargetPlanningRequest,
    ) -> ModelTargetPlan:
        """
        Stamp the sizing-input audit fields onto the plan so the bar path and the
        snapshot recorder persist consistent open prices, price sources, and asset
        figures without recomputing them. Planners stay unaware of price provenance.
        """
        price_sources = {
            instrument_id: source
            for instrument_id in request.open_prices
            for _price, source in (self._open_price_with_source(instrument_id),)
            if source is not None
        }
        expected_returns = {
            candidate.instrument_id: float(candidate.expected_return)
            for candidate in request.candidates
            if candidate.expected_return is not None
        }
        holding_meta = {
            holding.instrument_id: {
                "recent_buy_date": (
                    None
                    if holding.recent_target_date is None
                    else holding.recent_target_date.isoformat()
                ),
                "recent_holding_days": int(holding.recent_holding_days),
            }
            for holding in request.current_holdings
        }
        return dataclasses.replace(
            plan,
            open_prices=dict(request.open_prices),
            price_sources=price_sources,
            total_asset=request.total_asset,
            investable_asset=request.investable_asset,
            expected_returns=expected_returns,
            holding_meta=holding_meta,
        )

    def _target_planning_request(
        self,
        trading_date: date,
        signal_date: date | None,
    ) -> ModelTargetPlanningRequest:
        open_prices: dict[str, float] = {}
        candidates = self._build_candidates(trading_date, signal_date, open_prices)
        current_holdings = self._build_current_holdings(trading_date, signal_date, open_prices)
        active_ids = sorted(
            {candidate.instrument_id for candidate in candidates}
            | {holding.instrument_id for holding in current_holdings},
        )
        total_asset = float(self._portfolio_value())
        investable_asset = float(self.investable_total_asset())
        return ModelTargetPlanningRequest(
            trading_date=trading_date,
            signal_date=signal_date,
            active_instrument_ids=active_ids,
            candidates=candidates,
            current_holdings=current_holdings,
            target_cash_buffer_percent=float(self.config.target_cash_buffer_percent),
            max_position_percent=float(self.config.max_position_percent),
            total_asset=total_asset,
            investable_asset=investable_asset,
            open_prices=open_prices,
        )

    def _build_candidates(
        self,
        trading_date: date,
        signal_date: date | None,
        open_prices: dict[str, float],
    ) -> list[ModelTargetCandidate]:
        """
        Build the candidate list from the resolved signal date's signals only (not
        current holdings). Entry-ineligible signals (name prefixes, ST, suspension,
        minimum listed days, missing sizing price) are filtered out and recorded as
        ``entry_filtered`` signal events; the survivors carry their score, sizing open
        price, and ``expected_return`` (the model's ``pred_return_live``).
        """
        signals = self._signals_by_date.get(signal_date, []) if signal_date else []
        candidates: list[ModelTargetCandidate] = []
        seen: set[str] = set()
        entry_rank = 0
        for signal in signals:
            stock_code = signal["stock_code"]
            instrument = self._instrument_by_stock.get(stock_code)
            if instrument is None:
                continue
            instrument_id = str(instrument)
            if instrument_id in seen:
                continue
            seen.add(instrument_id)
            skip_reason = self._entry_skip_reason(stock_code, trading_date)
            if skip_reason is None and self._today_open_price(instrument_id) is None:
                self._log_missing_new_entry_open_price(
                    trading_date=trading_date,
                    signal_date=signal["date"],
                    instrument_id=instrument_id,
                    stock_code=stock_code,
                )
                skip_reason = "missing_open_price"
            if skip_reason:
                self._record_signal(
                    signal_date=signal["date"],
                    instrument_id=instrument_id,
                    stock_code=stock_code,
                    signal_name="entry_filtered",
                    score=signal["score"],
                    rank=signal.get("rank"),
                    side="buy",
                    selected=False,
                    extra={"reason": skip_reason},
                )
                continue
            price, _source = self._open_price_with_source(instrument_id)
            if price is not None:
                open_prices[instrument_id] = price
            entry_rank += 1
            candidates.append(
                ModelTargetCandidate(
                    instrument_id=instrument_id,
                    stock_code=normalize_stock_code(stock_code),
                    score=float(signal["score"]),
                    open_price=price,
                    expected_return=float(signal["pred_return_live"]),
                ),
            )
            self._record_signal(
                signal_date=signal["date"],
                instrument_id=instrument_id,
                stock_code=stock_code,
                signal_name="model_prediction_score",
                score=signal["score"],
                rank=entry_rank,
                side="buy",
                selected=True,
            )
        return candidates

    def _build_current_holdings(
        self,
        trading_date: date,
        signal_date: date | None,
        open_prices: dict[str, float],
    ) -> list[CurrentHolding]:
        """
        Build the current-holdings list (the planner's ``current_weights``) from the
        currently-held positions only — no merge with signals.

        Untradable holdings (ST / suspended) and holdings a local hard-exit rule
        (stop-loss / trailing-take-profit) fires on are excluded via
        ``_holding_exclusion`` so the optimizer unwinds them. Each surviving holding
        carries its share count, sizing price, and recency (``recent_target_date`` /
        holding days).
        """
        held_ids = sorted(self._held_instrument_ids())
        recent_target_dates = self._recent_target_dates(trading_date, held_ids)
        holdings: list[CurrentHolding] = []
        exit_rank = 0
        for instrument_id in held_ids:
            stock_code = normalize_stock_code(self._stock_by_instrument.get(instrument_id))
            if not stock_code:
                continue
            quantity = int(self._current_quantity(InstrumentId.from_str(instrument_id)))
            if quantity <= 0:
                continue
            exit_rank += 1
            exclusion = self._holding_exclusion(trading_date, signal_date, instrument_id, exit_rank)
            if exclusion is not None:
                continue
            price, _source = self._open_price_with_source(instrument_id)
            if price is None:
                continue
            open_prices.setdefault(instrument_id, price)
            recent_target_date = recent_target_dates.get(instrument_id)
            recent_holding_days = self._recent_holding_days(trading_date, recent_target_date)
            holdings.append(
                CurrentHolding(
                    instrument_id=instrument_id,
                    stock_code=stock_code,
                    quantity=quantity,
                    price=price,
                    recent_target_date=recent_target_date,
                    recent_holding_days=recent_holding_days,
                ),
            )
        return holdings

    def _recent_target_dates(
        self,
        trading_date: date,
        held_ids: list[str],
    ) -> dict[str, date]:
        """
        Resolve each held instrument's most-recent positive-target date (internal name
        ``recent_target_date``), keyed by instrument id.

        Live: query ``live_target_portfolio`` (the last ``target_qty > 0`` date within
        the trailing 30-trading-day window, excluding today) via the injected loader.
        Backtest: fall back to the position's entry date from ``_active_positions``.
        """
        result: dict[str, date] = {}
        loader = self._recent_target_loader
        if loader is not None:
            cutoff = self._recent_target_cutoff_date(trading_date)
            stock_codes = [
                normalize_stock_code(self._stock_by_instrument.get(instrument_id))
                for instrument_id in held_ids
            ]
            stock_codes = [code for code in stock_codes if code]
            try:
                raw = loader(trading_date, cutoff, stock_codes)
            except Exception as exc:
                if self.log is not None:
                    self.log.warning(f"recent target loader failed: {exc}", color=LogColor.YELLOW)
                raw = {}
            by_stock = {normalize_stock_code(code): value for code, value in (raw or {}).items()}
            for instrument_id in held_ids:
                stock_code = normalize_stock_code(self._stock_by_instrument.get(instrument_id))
                value = by_stock.get(stock_code)
                if value is not None:
                    result[instrument_id] = pd.Timestamp(value).date()
            return result
        # Backtest fallback: the position's entry date.
        for instrument_id in held_ids:
            state = self._active_positions.get(instrument_id)
            if not isinstance(state, dict):
                continue
            entry_date = state.get("entry_date")
            if entry_date is not None:
                result[instrument_id] = pd.Timestamp(entry_date).date()
        return result

    def _recent_target_cutoff_date(self, trading_date: date, window: int = 30) -> date:
        """The 30th-prior trading day (inclusive lower bound of the recency window)."""
        prior_dates = [value for value in self._trading_dates if value < trading_date]
        if not prior_dates:
            return trading_date
        prior_dates.sort()
        if len(prior_dates) <= window:
            return prior_dates[0]
        return prior_dates[-window]

    def _recent_holding_days(self, trading_date: date, recent_target_date: date | None) -> int:
        """
        Count trading days from ``recent_target_date`` to ``trading_date`` inclusive
        (today == recent_target_date → 1; 3 trading days back → 3). Returns 0 when the
        recent target date is unknown.
        """
        if recent_target_date is None:
            return 0
        trading_dates = sorted(self._trading_dates)
        try:
            today_index = trading_dates.index(trading_date)
        except ValueError:
            # Today may not be in the loaded calendar (e.g. edge dates): fall back to a
            # calendar-day span so we never report a negative / zero span.
            return max(1, (trading_date - recent_target_date).days + 1)
        recent_index = None
        for index, value in enumerate(trading_dates):
            if value >= recent_target_date:
                recent_index = index
                break
        if recent_index is None:
            return 1
        return max(1, today_index - recent_index + 1)

    def _open_price_with_source(self, instrument_id_text: str) -> tuple[float | None, str | None]:
        """
        Resolve the sizing price for an instrument and where it came from.

        Prefer today's open (``_today_open``); fall back to the previous close
        (``_last_close``). The source is recorded so the persisted target rows show
        when a prev-close fallback (an abnormal case) was used.
        """
        opens = getattr(self, "_today_open", None)
        if isinstance(opens, dict):
            open_price = opens.get(instrument_id_text)
            if open_price is not None and open_price > 0:
                return float(open_price), "open"
        close_price = self._last_close.get(instrument_id_text)
        if close_price is not None and close_price > 0:
            return float(close_price), "prev_close"
        return None, None

    def _plan_version(self, plan: ModelTargetPlan) -> str:
        signal_text = "none" if plan.signal_date is None else plan.signal_date.isoformat()
        total = sum(int(qty) for qty in plan.target_qty.values())
        return f"model-{plan.trading_date.isoformat()}-{signal_text}-{len(plan.target_qty)}-{total}"

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


def latest_signal_index(
    signals_by_date: dict[date, list[dict[str, Any]]],
) -> dict[str, list[tuple[date, dict[str, Any]]]]:
    result: dict[str, list[tuple[date, dict[str, Any]]]] = {}
    for signal_date, signals in signals_by_date.items():
        for signal in signals:
            stock_code = signal["stock_code"]
            result.setdefault(stock_code, []).append((signal_date, signal))
    for rows in result.values():
        rows.sort(key=lambda item: item[0])
    return result
