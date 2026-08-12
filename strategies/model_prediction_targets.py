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

from market_data import DailyStockData
from market_data import index_daily_stock_data
from strategies.model_common import ModelPredictionSignalEvent
from strategies.model_common import normalize_initial_active_positions
from strategies.model_common import normalize_signals
from strategies.model_common import previous_trading_date
from strategies.model_target_planners import CurrentHolding
from strategies.model_target_planners import ModelTargetCandidate
from strategies.model_target_planners import ModelTargetPlan
from strategies.model_target_planners import ModelTargetPlanningRequest
from strategies.model_target_planners import TargetContext
from strategies.model_target_planners import TargetInfo
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
    daily_stock_data: tuple[DailyStockData, ...] = ()
    consecutive_up_limit_days: int = 3
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
    local_exit_authoritative: bool = True
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
    _PRICE_COMPARISON_TOLERANCE = 1e-6

    def __init__(self, config: TargetModelPredictionsStrategyConfig) -> None:
        super().__init__(config)
        if int(config.consecutive_up_limit_days) < 0:
            raise ValueError("consecutive_up_limit_days must be non-negative")
        self._stock_by_instrument: dict[str, str] = {}
        self._instrument_by_stock: dict[str, InstrumentId] = {}
        self._update_instrument_stock_codes(config.instrument_stock_codes)
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
        self._daily_stock_data = index_daily_stock_data(config.daily_stock_data)
        self._limit_history_warnings: set[tuple[date, str, str]] = set()
        self._active_positions = normalize_initial_active_positions(config.initial_active_positions)
        self._processed_dates: set[date] = set()
        self._target_planner = build_model_target_planner(config, self.log)
        self._live_target_portfolio_loader: Callable[[date, date | None], list[dict[str, Any]]] | None = None
        self._live_target_plan_persister: Callable[[ModelTargetPlan], bool] | None = None
        self._recent_target_loader: Callable[[date, date, list[str]], dict[str, date]] | None = None
        self._target_alert_reporter: Callable[[str, str, dict[str, Any]], bool | None] | None = None
        self.signal_events: list[ModelPredictionSignalEvent] = []

    def configure_live_target_portfolio_loader(
        self,
        loader: Callable[[date, date | None], list[dict[str, Any]]] | None,
    ) -> None:
        """
        Inject a loader for persisted daily live targets.

        The strategy remains storage-agnostic: live wiring owns MySQL access and
        provides rows from its configured target-portfolio table. Callers without a
        persisted daily target leave this unset and use the computed plan path.
        """
        self._live_target_portfolio_loader = loader

    def configure_live_target_plan_persister(
        self,
        persister: Callable[[ModelTargetPlan], bool] | None,
    ) -> None:
        """Inject persistence for plans generated by the live strategy timer."""
        self._live_target_plan_persister = persister

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
        fall back to each position's entry date when no loader is configured.
        """
        self._recent_target_loader = loader

    def configure_target_alert_reporter(
        self,
        reporter: Callable[[str, str, dict[str, Any]], bool | None] | None,
    ) -> None:
        """Inject live monitoring for target-input and planning failures."""
        self._target_alert_reporter = reporter

    def _update_instrument_stock_codes(
        self,
        instrument_stock_codes: dict[str, str],
    ) -> None:
        for instrument_id, stock_code in instrument_stock_codes.items():
            normalized = normalize_stock_code(stock_code)
            if normalized:
                self._stock_by_instrument[str(instrument_id)] = normalized
        self._instrument_by_stock = {
            stock_code: InstrumentId.from_str(instrument_id)
            for instrument_id, stock_code in self._stock_by_instrument.items()
        }

    def _stock_code_for_instrument(self, instrument_id_text: str) -> str:
        stock_code = normalize_stock_code(
            self._stock_by_instrument.get(instrument_id_text),
        )
        if stock_code:
            return stock_code
        instrument_id = InstrumentId.from_str(instrument_id_text)
        stock_code = normalize_stock_code(instrument_id.symbol)
        if stock_code:
            self._stock_by_instrument[instrument_id_text] = stock_code
            self._instrument_by_stock[stock_code] = instrument_id
        return stock_code

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
        daily_stock_data: tuple[DailyStockData, ...],
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
        self._update_instrument_stock_codes(instrument_stock_codes)
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
        self._daily_stock_data = index_daily_stock_data(daily_stock_data)
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
        persister = self._live_target_plan_persister
        if plan.persistable and persister is not None:
            try:
                # Persist the newly generated plan before it becomes executable. The
                # live wiring owns the MySQL implementation; the strategy stays DB-free.
                persisted = persister(plan)
            except Exception as exc:
                self.log.warning(f"could not persist generated live target plan: {exc}")
                return
            if not persisted:
                self.log.warning("generated live target plan was not persisted; target not applied")
                return
        # The risk-manager planner commits explicit share counts (固定目标股数); the
        # executor trades toward those quantities. Weights on the plan are audit-only
        # and are not consulted for execution.
        self.update_target_quantities(
            quantities=self._target_quantities_from_plan(plan),
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
        Build the day's target request and return the resulting plan
        **without submitting orders or accepting the target**.

        Two independent inputs are built and filtered separately (no merge):

        * **candidates** — the signal stocks, minus entry-ineligible ones (name
          prefixes, ST, suspension, minimum listed days, missing sizing price).
        * **current_holdings** — the held positions, minus the ones a local hard-exit
          rule (ST / stop-loss / trailing take-profit) fires on. Suspended holdings
          remain frozen in current state.

        Missing signal data and risk-manager failures return a transient local safety
        plan. Such plans are executable but deliberately not restart-persistent.

        The bar/timer path (_process_trading_day) uses this and then applies the plan
        via update_target_quantities. The snapshot recorder uses it before-trading to
        derive the day's frozen share counts. Both paths therefore run the same logic.
        """
        self._seed_active_positions_from_portfolio(trading_date)
        signal_date = self._resolve_signal_date(trading_date)
        has_signal_data = self._has_expected_signal_date(trading_date)
        today_signals = self._signals_by_date.get(signal_date, []) if signal_date else []
        self.log.info(
            f"model target day {trading_date}: signal_date={signal_date} "
            f"signals={len(today_signals)} active={len(self._active_positions)}",
            color=LogColor.BLUE,
        )
        return self._target_plan(
            trading_date,
            signal_date,
            degraded_reason=None if has_signal_data else "missing_signal_data",
        )

    def _has_expected_signal_date(self, trading_date: date) -> bool:
        expected_signal_date = previous_trading_date(self._trading_dates, trading_date)
        if expected_signal_date is None:
            raise RuntimeError(f"no previous trading date available for {trading_date}")
        if expected_signal_date in self._signals_by_date:
            return True
        message = (
            "Previous-trading-date signal data is missing; applying a transient local safety plan: "
            f"trading_date={trading_date} expected_signal_date={expected_signal_date}"
        )
        self.log.error(message)
        reporter = self._target_alert_reporter
        if reporter is None:
            return False
        try:
            reporter(
                "TARGET-MODEL-SIGNAL-DATA-MISSING",
                "failed",
                {
                    "trading_date": trading_date,
                    "expected_signal_date": expected_signal_date,
                },
            )
        except Exception as exc:
            self.log.warning(f"Could not queue DingTalk missing-signal-data alert: {exc}")
        return False

    def plan_version(self, plan: ModelTargetPlan) -> str:
        """Public alias of the version string used by update_target_quantities."""
        return self._plan_version(plan)

    def _resolve_signal_date(self, trading_date: date) -> date | None:
        return previous_trading_date(self._trading_dates, trading_date)

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
            stock_code = self._stock_code_for_instrument(instrument_id_text)
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
                stock_code=self._stock_code_for_instrument(instrument_id),
                fallback_price=exit_price,
            )
        cost_price = float(state.get("entry_price") or exit_price or 0.0)
        trailing = self._update_trailing_state(state, exit_price, cost_price)
        stock_code = self._stock_code_for_instrument(instrument_id)
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
            stock_code=stock_code,
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

    def _target_plan(
        self,
        trading_date: date,
        signal_date: date | None,
        degraded_reason: str | None = None,
    ) -> ModelTargetPlan:
        request, forced_exits = self._target_planning_request(trading_date, signal_date)
        if degraded_reason is not None:
            return self._local_safety_plan(request, forced_exits, degraded_reason)
        try:
            plan = self._target_planner.plan(request)
        except Exception as exc:
            self._report_risk_manager_failure(trading_date, signal_date, exc)
            return self._local_safety_plan(
                request,
                forced_exits,
                "risk_manager_failure",
            )
        annotated = self._annotate_plan(plan, request)
        if not bool(self.config.local_exit_authoritative):
            return annotated
        return self._apply_forced_exit_overrides(annotated, forced_exits)

    def _report_risk_manager_failure(
        self,
        trading_date: date,
        signal_date: date | None,
        exc: Exception,
    ) -> None:
        message = (
            f"risk-manager planning failed; applying a transient local safety plan: "
            f"trading_date={trading_date} signal_date={signal_date} error={exc}"
        )
        self.log.error(message)
        reporter = self._target_alert_reporter
        if reporter is None:
            return
        try:
            reporter(
                "TARGET-MODEL-RISK-MANAGER-FAILURE",
                "failed",
                {
                    "trading_date": trading_date,
                    "signal_date": signal_date,
                    "error": str(exc),
                },
            )
        except Exception as report_exc:
            self.log.warning(f"Could not queue DingTalk risk-manager failure alert: {report_exc}")

    def _local_safety_plan(
        self,
        request: ModelTargetPlanningRequest,
        forced_exits: dict[str, str],
        degraded_reason: str,
    ) -> ModelTargetPlan:
        targets = [
            self._local_target_info(
                instrument_id,
                0 if instrument_id in forced_exits else quantity,
            )
            for instrument_id, quantity in self._held_quantities().items()
        ]
        return ModelTargetPlan(
            trading_date=request.trading_date,
            signal_date=request.signal_date,
            targets=targets,
            reason=f"local_exit_{degraded_reason}",
            total_asset=request.total_asset,
            investable_asset=request.investable_asset,
            persistable=False,
            degraded_reason=degraded_reason,
        )

    def _apply_forced_exit_overrides(
        self,
        plan: ModelTargetPlan,
        forced_exits: dict[str, str],
    ) -> ModelTargetPlan:
        if not forced_exits:
            return plan
        targets_by_instrument = {target.instrument_id: target for target in plan.targets}
        for instrument_id in forced_exits:
            targets_by_instrument[instrument_id] = self._local_target_info(instrument_id, 0)
        return dataclasses.replace(
            plan,
            targets=[targets_by_instrument[key] for key in sorted(targets_by_instrument)],
        )

    def _held_quantities(self) -> dict[str, int]:
        quantities: dict[str, int] = {}
        for instrument_id in sorted(self._held_instrument_ids()):
            quantity = int(self._current_quantity(InstrumentId.from_str(instrument_id)))
            if quantity > 0:
                quantities[instrument_id] = quantity
        return quantities

    def _local_target_info(self, instrument_id: str, quantity: int) -> TargetInfo:
        price, price_source = self._open_price_with_source(instrument_id)
        stock_code = self._stock_code_for_instrument(instrument_id)
        return TargetInfo(
            stock_code=stock_code,
            weight=None,
            quantity=int(quantity),
            target_context=TargetContext(
                price=price,
                price_source=price_source,
                current_qty=int(self._current_quantity(InstrumentId.from_str(instrument_id))),
                market_status=self._market_status_for(instrument_id),
            ),
            target_version=None,
            instrument_id=instrument_id,
            is_locked=self._is_suspended_status(instrument_id),
        )

    def _annotate_plan(
        self,
        plan: ModelTargetPlan,
        request: ModelTargetPlanningRequest,
    ) -> ModelTargetPlan:
        """
        Stamp the sizing-input audit fields onto the plan so the bar path and the
        snapshot recorder persist consistent request metadata and asset figures
        without recomputing them. Planners stay unaware of price provenance.
        """
        candidates = {candidate.instrument_id: candidate for candidate in request.candidates}
        holdings = {holding.instrument_id: holding for holding in request.current_holdings}
        targets: list[TargetInfo] = []
        for target in plan.targets:
            candidate = candidates.get(target.instrument_id)
            holding = holdings.get(target.instrument_id)
            price_source = self._open_price_with_source(target.instrument_id)[1]
            targets.append(
                dataclasses.replace(
                    target,
                    target_context=TargetContext(
                        price=request.open_prices.get(target.instrument_id),
                        price_source=price_source,
                        score=None if candidate is None else float(candidate.score),
                        expected_return=(
                            None
                            if candidate is None or candidate.expected_return is None
                            else float(candidate.expected_return)
                        ),
                        current_qty=None if holding is None else int(holding.quantity),
                        recent_target_date=(
                            None if holding is None else holding.recent_target_date
                        ),
                        recent_holding_days=None
                        if holding is None
                        else int(holding.recent_holding_days),
                        market_status=self._market_status_for(target.instrument_id),
                    ),
                ),
            )
        return dataclasses.replace(
            plan,
            targets=targets,
            total_asset=request.total_asset,
            investable_asset=request.investable_asset,
        )

    def _target_planning_request(
        self,
        trading_date: date,
        signal_date: date | None,
    ) -> tuple[ModelTargetPlanningRequest, dict[str, str]]:
        open_prices: dict[str, float] = {}
        forced_exits: dict[str, str] = {}
        if bool(self.config.local_exit_authoritative):
            current_holdings = self._build_current_holdings(
                trading_date,
                signal_date,
                open_prices,
                forced_exits=forced_exits,
            )
            candidates = self._build_candidates(
                trading_date,
                signal_date,
                open_prices,
                excluded_instrument_ids=set(forced_exits),
            )
        else:
            # Preserve the pre-switch request path exactly: candidates are built
            # normally, while triggered holdings are only removed from current state.
            candidates = self._build_candidates(trading_date, signal_date, open_prices)
            current_holdings = self._build_current_holdings(
                trading_date,
                signal_date,
                open_prices,
                forced_exits=forced_exits,
            )
        # TEMP: for account 86008933, force recent_holding_days=3 so these holdings are
        # easy to drop/liquidate via the risk manager.
        #if self._account_id_equals(86008933):
        #    current_holdings = [
        #        dataclasses.replace(holding, recent_holding_days=3)
        #        for holding in current_holdings
        #    ]
        active_ids = sorted(
            {candidate.instrument_id for candidate in candidates}
            | {holding.instrument_id for holding in current_holdings},
        )
        total_asset = float(self._portfolio_value())
        if self._account_id_equals(86904088):
            holdings_value = sum(
                holding.quantity * holding.price for holding in current_holdings
            )
            investable_asset = float(holdings_value * 1.01)
        else:
            investable_asset = total_asset * (
                1.0 - float(self.config.target_cash_buffer_percent)
            )
        if investable_asset <= 0:
            investable_asset = float(self.config.initial_cash)
        request = ModelTargetPlanningRequest(
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
        return request, forced_exits

    def _account_id_equals(self, account_number: int) -> bool:
        for account in self._broker_accounts():
            account_id = account.id
            if str(account_id.get_id()) == str(account_number):
                return True
        return False

    def _build_candidates(
        self,
        trading_date: date,
        signal_date: date | None,
        open_prices: dict[str, float],
        excluded_instrument_ids: set[str] | None = None,
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
            skip_reason = (
                "local_exit"
                if instrument_id in (excluded_instrument_ids or set())
                else self._entry_skip_reason(stock_code, trading_date)
            )
            price = self._today_open_price(instrument_id)
            if skip_reason is None and price is None:
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
        forced_exits: dict[str, str] | None = None,
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
            stock_code = self._stock_code_for_instrument(instrument_id)
            if not stock_code:
                self.log.warning(f"Skipping holding with invalid stock code: {instrument_id}")
                continue
            quantity = int(self._current_quantity(InstrumentId.from_str(instrument_id)))
            if quantity <= 0:
                self.log.warning(f"Skipping holding with zero or negative quantity: {instrument_id}")
                continue
            exit_rank += 1
            # Suspended holdings stay in current_weights so the risk manager can
            # preserve them. They cannot be bought or sold, whether suspension is
            # reported by live market status or the daily suspension calendar.
            is_suspended = (
                self._is_suspended_status(instrument_id)
                or stock_code in self._suspended_by_date.get(trading_date, set())
            )
            if is_suspended:
                recent_target_date = recent_target_dates.get(instrument_id)
                last_close = self._last_close.get(instrument_id)
                if last_close is None or last_close <= 0:
                    self.log.warning(
                        f"Skipping suspended holding with no last_close price: "
                        f"{instrument_id} stock_code={stock_code}",
                        color=LogColor.YELLOW,
                    )
                    continue
                price = float(last_close)
                self.log.warning(
                    f"Suspended holding frozen: instrument_id={instrument_id} "
                    f"stock_code={stock_code} price=last_close({price}) recent_holding_days=0 "
                    f"quantity={quantity} can_buy=False can_sell=False",
                    color=LogColor.YELLOW,
                )
                open_prices.setdefault(instrument_id, price)
                holdings.append(
                    CurrentHolding(
                        instrument_id=instrument_id,
                        stock_code=stock_code,
                        quantity=quantity,
                        price=price,
                        recent_target_date=recent_target_date,
                        recent_holding_days=0,
                        can_buy=False,
                        can_sell=False,
                    ),
                )
                continue
            exclusion = self._holding_exclusion(trading_date, signal_date, instrument_id, exit_rank)
            if exclusion is not None:
                if forced_exits is not None:
                    forced_exits[instrument_id] = exclusion
                self.log.warning(f"Excluding holding from current_holdings: {instrument_id} reason={exclusion}")
                continue
            price = self._today_open_price(instrument_id)
            if price is None:
                self._log_missing_new_entry_open_price(
                    trading_date=trading_date,
                    signal_date=signal_date or trading_date,
                    instrument_id=instrument_id,
                    stock_code=stock_code,
                )
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
        self.log.info(
            f"Built current_holdings list with {len(holdings)} items., "
            f"original held={len(held_ids)}",
            color=LogColor.BLUE,
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

        Query the configured target-portfolio store (the last ``target_qty > 0`` date
        within the trailing window, excluding today) via the injected loader. Without
        a loader, fall back to the position's entry date from ``_active_positions``.
        """
        result: dict[str, date] = {}
        loader = self._recent_target_loader
        if loader is not None:
            cutoff = self._recent_target_cutoff_date(trading_date)
            stock_codes = [
                self._stock_code_for_instrument(instrument_id)
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
                stock_code = self._stock_code_for_instrument(instrument_id)
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

    def _recent_target_cutoff_date(self, trading_date: date, window: int = 90) -> date:
        """The 90th-prior trading day (inclusive lower bound of the recency window)."""
        prior_dates = [value for value in self._trading_dates if value < trading_date]
        if not prior_dates:
            return trading_date
        prior_dates.sort()
        if len(prior_dates) <= window:
            return prior_dates[0]
        return prior_dates[-window]

    def _recent_holding_days(self, trading_date: date, recent_target_date: date | None) -> int:
        """
        Count the number of trading days elapsed since ``recent_target_date``, with a
        floor of 1: today == recent_target_date → 1; 1 trading day back → 1; 3 trading
        days back → 3 (i.e. the trading-day index distance, min 1). Returns 0 when the
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
            return max(1, (trading_date - recent_target_date).days)
        recent_index = None
        for index, value in enumerate(trading_dates):
            if value >= recent_target_date:
                recent_index = index
                break
        if recent_index is None:
            return 1
        return max(1, today_index - recent_index)

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
        quantities = self._target_quantities_from_plan(plan)
        total = sum(int(qty) for qty in quantities.values())
        return f"model-{plan.trading_date.isoformat()}-{signal_text}-{len(quantities)}-{total}"

    @staticmethod
    def _target_quantities_from_plan(plan: ModelTargetPlan) -> dict[str, int]:
        return {
            target.instrument_id: int(target.quantity)
            for target in plan.targets
            if target.quantity is not None
        }

    def _entry_skip_reason(self, stock_code: str, trading_date: date) -> str | None:
        name_reason = self._name_skip_reason(stock_code)
        if name_reason:
            return name_reason
        instrument = self._instrument_by_stock.get(stock_code)
        if instrument is not None and self._is_suspended_status(str(instrument)):
            return "suspended"
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
        if self._has_consecutive_up_limits(stock_code, trading_date):
            return "consecutive_up_limit"
        return None

    def _has_consecutive_up_limits(self, stock_code: str, trading_date: date) -> bool:
        consecutive_days = int(self.config.consecutive_up_limit_days)
        if consecutive_days <= 0:
            return False

        instrument_id = self._instrument_by_stock.get(stock_code)
        if instrument_id is None:
            return False
        instrument_id_text = str(instrument_id)
        today_open = self._today_open_price(instrument_id_text)
        if today_open is None:
            # The existing candidate sizing path reports and filters this separately.
            return False

        today_up_limit, _ = self._price_limits(instrument_id_text)
        if today_up_limit is None:
            today_row = self._daily_stock_data.get((stock_code, trading_date))
            if today_row is not None:
                today_up_limit = today_row.up_limit
        if today_up_limit is None or today_up_limit <= 0:
            self._warn_incomplete_limit_history(
                stock_code,
                trading_date,
                "missing current-day up_limit from instrument info and daily history",
            )
            return False
        if today_open < today_up_limit - self._PRICE_COMPARISON_TOLERANCE:
            return False

        history_days = consecutive_days - 1
        if history_days == 0:
            return True
        prior_trading_dates = sorted(
            value for value in set(self._trading_dates) if value < trading_date
        )
        required_dates = prior_trading_dates[-history_days:]
        if len(required_dates) != history_days:
            self._warn_incomplete_limit_history(
                stock_code,
                trading_date,
                f"need {history_days} prior trading dates, found {len(required_dates)}",
            )
            return False

        incomplete: list[str] = []
        rows: list[DailyStockData] = []
        for required_date in required_dates:
            row = self._daily_stock_data.get((stock_code, required_date))
            if row is None:
                incomplete.append(f"{required_date}:missing_row")
                continue
            missing_fields = []
            if row.close is None or row.close <= 0:
                missing_fields.append("close")
            if row.up_limit is None or row.up_limit <= 0:
                missing_fields.append("up_limit")
            if missing_fields:
                incomplete.append(f"{required_date}:missing_{'+'.join(missing_fields)}")
                continue
            rows.append(row)
        if incomplete:
            self._warn_incomplete_limit_history(
                stock_code,
                trading_date,
                ",".join(incomplete),
            )
            return False

        return all(
            row.close >= row.up_limit - self._PRICE_COMPARISON_TOLERANCE
            for row in rows
            if row.close is not None and row.up_limit is not None
        )

    def _warn_incomplete_limit_history(
        self,
        stock_code: str,
        trading_date: date,
        detail: str,
    ) -> None:
        warning_key = (trading_date, stock_code, detail)
        if warning_key in self._limit_history_warnings:
            return
        self._limit_history_warnings.add(warning_key)
        self.log.warning(
            "consecutive up-limit filter admitted candidate because reference data "
            f"is incomplete: date={trading_date} stock_code={stock_code} detail={detail}",
            color=LogColor.YELLOW,
        )

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
        self.log.info(
            f"recorded signal event: date={signal_date} instrument_id={instrument_id} "
            f"stock_code={stock_code} signal_name={signal_name} score={score} "
            f"rank={rank} side={side} selected={selected} extra={extra}",
            color=LogColor.BLUE,
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
