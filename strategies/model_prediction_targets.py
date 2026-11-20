from __future__ import annotations

import dataclasses
import math
from uuid import uuid4
from datetime import date
from datetime import timedelta
from decimal import Decimal
from typing import Any
from typing import Callable

import pandas as pd

from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.enums import MarketStatusAction

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
from strategies.model_target_planners import ModelTargetPlanningContext
from strategies.model_target_planners import ModelTargetInstrumentContext
from strategies.model_target_planners import TargetContext
from strategies.model_target_planners import TargetInfo
from strategies.model_target_planners import build_model_target_planner
from strategies.model_target_planners import normalize_stock_code
from strategies.model_target_planners.base import ForcedExit
from strategies.replan_triggers import ReplanTriggerTracker, price_limit_state
from strategies.replan_triggers import PRICE_COMPARISON_TOLERANCE
from strategies.replan_triggers import (
    PriceLimitReplanTriggerPolicy, ScheduledReplanTriggerPolicy, ReplanValidity,
)
from strategies.replanning import PlanningAttempt, ReplanningBusyError, ReplanningCoordinator, planning_json
from strategies.convergence import ConvergenceTrigger
from strategies.opening_auction import auction_time
from strategies.trading_control import SELL_REASON, SELL_ALL_REASON
from strategies.target_quantities import TargetQuantityStrategy
from strategies.target_quantities import TargetQuantityStrategyConfig
from strategies.target_quantities import TickSnapshot
from strategies.target_quantities import bar_date


class TargetModelPredictionsStrategyConfig(TargetQuantityStrategyConfig, kw_only=True, frozen=True):
    instrument_stock_codes: dict[str, str]
    signals_by_date: dict[str, list[dict[str, Any]]]
    prediction_ranks_by_date: dict[str, dict[str, int]]
    trading_dates: list[str]
    listed_dates: dict[str, str]
    st_by_date: dict[str, list[str]]
    suspended_by_date: dict[str, list[str]]
    daily_stock_data: tuple[DailyStockData, ...] = ()
    consecutive_up_limit_days: int = 3
    max_open_gap_up: float = 0.05
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
    predictions_database: str = ""
    predictions_table: str = ""
    risk_manager_base_url: str = ""
    risk_manager_risk_model_id: str = ""
    alpha_model_id: str = ""
    risk_manager_mode: str = "simulation"
    risk_manager_timeout_secs: float = 10.0
    process_targets_on_timer: bool = False
    process_targets_interval_secs: float = 2.0
    auto_replan_enabled: bool = True
    scheduled_replan_times: tuple[str, ...] = ()


class TargetModelPredictionsStrategy(TargetQuantityStrategy):
    """
    Model-prediction target provider using the reusable target-quantity executor.

    This class decides the target share counts (via the risk-manager planner). The
    inherited executor decides how to reach them through Nautilus account, cache, and
    order APIs.
    """

    _PROCESS_TARGETS_TIMER = "TARGET-MODEL-PROCESS-TARGETS"
    _PRICE_COMPARISON_TOLERANCE = PRICE_COMPARISON_TOLERANCE

    def __init__(self, config: TargetModelPredictionsStrategyConfig) -> None:
        super().__init__(config)
        self._auction_replay_model = None
        self._auction_planning_contexts: dict[date, ModelTargetPlanningContext] = {}
        self._auction_execution_snapshot_dates: set[date] = set()
        self._auction_snapshot_failed_dates: set[date] = set()
        self._frozen_auction_plans: dict[date, ModelTargetPlan] = {}
        self._auction_frozen_dates: set[date] = set()
        if not isinstance(config.auto_replan_enabled, bool):
            raise ValueError("auto_replan_enabled must be a boolean")
        if int(config.consecutive_up_limit_days) < 0:
            raise ValueError("consecutive_up_limit_days must be non-negative")
        if not 0.0 <= float(config.max_open_gap_up) <= 1.0:
            raise ValueError("max_open_gap_up must be between 0 and 1")
        self._stock_by_instrument: dict[str, str] = {}
        self._instrument_by_stock: dict[str, InstrumentId] = {}
        self._update_instrument_stock_codes(config.instrument_stock_codes)
        self._signals_by_date = normalize_signals(config.signals_by_date)
        self._prediction_ranks_by_date = normalize_prediction_ranks(
            config.prediction_ranks_by_date,
        )
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
        self._open_gap_warnings: set[tuple[date, str]] = set()
        self._active_positions = normalize_initial_active_positions(config.initial_active_positions)
        self._processed_dates: set[date] = set()
        self._target_planner = build_model_target_planner(config, self.log)
        self._live_target_portfolio_loader: Callable[[date, date | None], list[dict[str, Any]]] | None = None
        self._live_target_plan_persister: Callable[[ModelTargetPlan], bool] | None = None
        self._recent_target_loader: Callable[[date, date, list[str]], dict[str, date]] | None = None
        self._position_span_start_loader: Callable[[date, date, list[str]], dict[str, date]] | None = None
        self._divid_events_loader: Callable[[list[str]], dict[str, list[tuple[date, float]]]] | None = None
        self._target_alert_reporter: Callable[[str, str, dict[str, Any]], bool | None] | None = None
        self._span_close_coverage_warnings: set[tuple[date, str]] = set()
        self._entry_cost_adjust_warnings: set[tuple[date, str]] = set()
        self._stop_cost_adjust_warnings: set[tuple[date, str]] = set()
        self.signal_events: list[ModelPredictionSignalEvent] = []
        self.replanning = ReplanningCoordinator(self)
        self._replan_triggers = ReplanTriggerTracker()
        self._price_limit_replan_triggers = PriceLimitReplanTriggerPolicy(self._replan_triggers)
        self._scheduled_replan_triggers = ScheduledReplanTriggerPolicy(self, config.scheduled_replan_times)
        self._planning_attempt_persister = None
        self._committed_plan_loader = None
        self._latest_planning_inputs: dict[str, Any] = {}
        self.planning_attempts: dict[str, dict[str, Any]] = {}

    def configure_planning_archive(self, persister, loader=None) -> None:
        self._planning_attempt_persister = persister
        self._committed_plan_loader = loader

    def archive_planning_attempt(self, attempt: PlanningAttempt, plan=None) -> None:
        if self._planning_attempt_persister is not None:
            self._planning_attempt_persister(attempt, plan)
        elif plan is not None and plan.persistable and self._live_target_plan_persister is not None:
            if not self._live_target_plan_persister(plan):
                raise RuntimeError("target plan persistence failed")
        self.planning_attempts[attempt.request_id] = attempt.payload(plan)

    def accept_model_target_plan(self, plan: ModelTargetPlan, *, bypass_pause: bool = False) -> None:
        self.update_target_quantities(self._target_quantities_from_plan(plan),
                                      target_date=plan.trading_date, reason=plan.reason,
                                      version=self._plan_version(plan), signal_date=plan.signal_date,
                                      bypass_pause=bypass_pause, bypass_replanning=True)

    def replan_resume_trigger(self) -> ConvergenceTrigger:
        return ConvergenceTrigger.TIMER

    def request_replanning(self) -> str:
        return self.replanning.request("manual")

    def automatic_replanning_allowed(self) -> bool:
        # Manual sell targets remain authoritative until an ordinary model target
        # update (or an explicit manual replan) replaces them.
        return (self.replanning.auto_enabled and not self._trading_paused
                and not self._target_updates_paused
                and self._target_reason not in (SELL_REASON, SELL_ALL_REASON))

    def check_replanning_trigger(self, trading_date: date) -> bool:
        coordinator = self.replanning
        with self._trading_action_lock:
            if (coordinator.initial_in_progress or not self.automatic_replanning_allowed()
                    or not self._target_version):
                return False
            triggers = self._price_limit_replan_triggers.collect(self, trading_date)
            if triggers:
                self.submit_automatic_replanning(triggers, self._price_limit_replan_triggers.validity)
                return True
            return coordinator.busy

    def submit_automatic_replanning(self, triggers, validity: ReplanValidity) -> str | None:
        """All automatic trigger policies enter the same order/planning coordinator."""
        try:
            return self.replanning.request("automatic", triggers, validity=validity)
        except ReplanningBusyError:
            return None

    def run_pending_replanning(self) -> None:
        self._scheduled_replan_triggers.drain()

    def replanning_trigger_state(self) -> dict:
        return self._scheduled_replan_triggers.state()

    def on_stop(self) -> None:
        self.replanning.stopped = True
        self._scheduled_replan_triggers.stop()
        if "MODEL-REPLAN-STEP" in self.clock.timer_names:
            self.clock.cancel_timer("MODEL-REPLAN-STEP")
        task = self.replanning.task
        if task is not None and not task.done():
            task.cancel()
        elif self.replanning.current is not None:
            self.replanning.interrupt(self.replanning.current)
        super().on_stop()

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

    def configure_position_span_start_loader(
        self,
        loader: Callable[[date, date, list[str]], dict[str, date]] | None,
    ) -> None:
        """
        Inject a loader for the start date of each held stock's current holding span.

        Signature: ``loader(trading_date, cutoff_trade_date, stock_codes) ->
        {stock_code: span_start_date}``. Live wiring queries ``live_position_snapshot``
        for the most-recent flat->held transition (``after_trading`` held with no
        concurrent ``before_trading`` held) within ``[cutoff_trade_date, trading_date]``.
        Backtests leave this unset and fall back to each position's entry date. The span
        bounds the highest-close computation that seeds the trailing high-water mark.
        """
        self._position_span_start_loader = loader

    def configure_divid_events_loader(
        self,
        loader: Callable[[list[str]], dict[str, list[tuple[date, float]]]] | None,
    ) -> None:
        """
        Inject a loader for per-stock dividend/split ex-events.

        Signature: ``loader(stock_codes) -> {stock_code: [(ex_date, coef), ...]}``, where
        ``coef`` is the per-event back-adjustment coefficient (复权系数). Live wiring
        queries the QMT ``get_divid_factors`` adapter after the daily full-tick snapshot.
        The strategy filters events to each position's ``(entry_date, trading_date]``
        window and takes their product to restate the entry cost onto today's basis for
        the stop-loss check. Backtests leave this unset and derive the same ratio from
        ClickHouse ``adj_factor`` in ``_daily_stock_data``.
        """
        self._divid_events_loader = loader

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

    def instrument_id_for_stock_code(self, stock_code: str) -> InstrumentId | None:
        """Resolve a normalized stock code through the configured Nautilus universe."""
        normalized = normalize_stock_code(stock_code)
        return self._instrument_by_stock.get(normalized)

    def refresh_reference_data(
        self,
        instrument_ids: list[InstrumentId],
        bar_types: dict[str, BarType],
        instrument_stock_codes: dict[str, str],
        signals_by_date: dict[str, list[dict[str, Any]]],
        prediction_ranks_by_date: dict[str, dict[str, int]],
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
        self._prediction_ranks_by_date = normalize_prediction_ranks(
            prediction_ranks_by_date,
        )
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
        if not (self.config.auction_enabled and today in self._auction_frozen_dates):
            self._processed_dates.discard(today)
        if self._within_trading_window():
            self._process_trading_day_once(today, "refresh")

    def on_target_bar(self, bar: Bar) -> None:
        trading_date = bar_date(bar, self.config.timezone_name)
        self._process_trading_day_once(trading_date, "bar")

    def on_start(self) -> None:
        self.replanning.stopped = False
        super().on_start()
        self._start_process_targets_timer()
        self._scheduled_replan_triggers.start()

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
        if self._replanning_blocked or self._target_updates_paused:
            return False
        if trading_date in self._processed_dates:
            return False
        if self._process_trading_day(trading_date) is False:
            return False
        self._processed_dates.add(trading_date)
        self.log.info(
            f"processed model target day from {trigger}: date={trading_date}",
            color=LogColor.BLUE,
        )
        return True

    def _process_trading_day(self, trading_date: date) -> bool:
        if self._restore_saved_target(trading_date):
            return True
        plan = self.compute_daily_target_plan(trading_date)
        if plan is None:
            return True
        persister = self._live_target_plan_persister
        if not plan.persistable:
            self.archive_planning_attempt(PlanningAttempt(
                plan.request_id or uuid4().hex, trading_date, "initial", self._target_version or None,
                status="safety", inputs=self._latest_planning_inputs), plan)
        elif persister is not None:
            try:
                # Persist the newly generated plan before it becomes executable. The
                # live wiring owns the MySQL implementation; the strategy stays DB-free.
                persisted = persister(plan)
            except Exception as exc:
                self.log.warning(f"could not persist generated live target plan: {exc}")
                return False
            if not persisted:
                self.log.warning("generated live target plan was not persisted; target not applied")
                return False
        # The risk-manager planner commits explicit share counts (固定目标股数); the
        # executor trades toward those quantities. Weights on the plan are audit-only
        # and are not consulted for execution.
        with self._trading_action_lock:
            if self._replanning_blocked or plan.parent_version != (self._target_version or None):
                return False
            self.update_target_quantities(
                quantities=self._target_quantities_from_plan(plan),
                target_date=trading_date,
                reason=plan.reason,
                version=self._plan_version(plan),
                signal_date=plan.signal_date,
            )

        return True

    def _restore_saved_target(self, trading_date: date) -> bool:
        """Restore the committed version before consulting per-instrument rows.

        An empty committed plan has no projection rows but is still a saved target.
        Daily and auction planning must make the same distinction.
        """
        if self._committed_plan_loader is not None:
            archived = self._committed_plan_loader(trading_date)
            if archived is not None:
                self.restore_committed_plan(trading_date, archived)
                return True
        loaded_target = self._live_target_portfolio_target(trading_date)
        if loaded_target is not None:
            quantities, reason, version, signal_date = loaded_target
            self.update_target_quantities(
                quantities=quantities,
                target_date=trading_date,
                reason=reason,
                version=version,
                signal_date=signal_date,
            )
            return True
        return False

    def restore_committed_plan(self, trading_date: date, archived: dict[str, Any]) -> None:
        self._roll_trading_day(trading_date)
        for iid, extra in archived.get("target_extra", {}).items():
            if extra.get("execution_phase") == "opening_auction":
                self._auction_frozen_dates.add(trading_date)
                if extra.get("prediction_rank") is not None:
                    self._auction_prediction_ranks[iid] = int(extra["prediction_rank"])
        self.update_target_quantities(archived["quantities"], target_date=trading_date,
                                      reason=archived["reason"], version=archived["version"],
                                      signal_date=archived["signal_date"])

    def on_price_snapshot(self, trading_date: date) -> None:
        if self._target_date == trading_date and self._within_execution_window():
            self.check_replanning_trigger(trading_date)

    def _live_target_portfolio_target(
        self,
        trading_date: date,
    ) -> tuple[dict[str, int], str, str | None, date | None] | None:
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
        if self.config.auction_enabled:
            import json

            auction_rows = False
            for row in rows:
                extra = row.get("extra") or {}
                if isinstance(extra, str):
                    extra = json.loads(extra)
                if extra.get("execution_phase") == "opening_auction":
                    auction_rows = True
                    rank = extra.get("prediction_rank")
                    if rank is not None:
                        self._auction_prediction_ranks[str(row["instrument_id"])] = int(rank)
            if auction_rows:
                self._auction_frozen_dates.add(trading_date)
        self._log_live_target_portfolio_info(
            f"loaded live target portfolio: date={trading_date} signal_date={signal_date} "
            f"frozen_qty={len(quantities)} version={version}",
            color=LogColor.GREEN,
        )
        return quantities, reason, version, signal_date

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

    def build_planning_context(
        self, trading_date: date, *, stage: str | None = None,
        snapshot: dict[str, dict[str, Any]] | None = None,
        ts_init: int | None = None,
    ) -> ModelTargetPlanningContext:
        """Capture inputs once; planning helpers never reread mutable price caches."""
        if stage is None:
            now = pd.Timestamp(self.clock.utc_now()).tz_convert(self.config.timezone_name)
            stage = "pre_open" if now.time() < auction_time("09:30") else "continuous"
        instruments: dict[str, ModelTargetInstrumentContext] = {}
        if snapshot is None:
            # Preserve the existing bar-based path when no full-tick was supplied.
            for i in self._today_open.keys() | self._last_close.keys() | self._market_status.keys():
                tick = self._market_status.get(i)
                if tick is None:
                    price = self._today_open.get(i)
                    source = "open" if price is not None else None
                else:
                    price = tick.last_price
                    if price is not None and (not math.isfinite(price) or price <= 0):
                        price = None
                    source = "full_tick_last" if price is not None else None
                instruments[i] = ModelTargetInstrumentContext(
                    price=price, open_price=self._today_open.get(i),
                    previous_close=self._last_close.get(i),
                    market_status=None if tick is None else tick.market_status,
                    price_source=source,
                    price_ts_event=None if tick is None else tick.ts_event,
                )
        else:
            instruments = {i: ModelTargetInstrumentContext(previous_close=close)
                           for i, close in self._last_close.items()}
            for instrument_id, fields in snapshot.items():
                i = str(instrument_id)
                tick = TickSnapshot.from_fields(fields)
                price = tick.last_price
                if price is not None and (not math.isfinite(price) or price <= 0):
                    price = None
                open_price = tick.open
                if open_price is not None and (not math.isfinite(open_price) or open_price <= 0):
                    open_price = None
                close = tick.last_close
                if close is None or not math.isfinite(close) or close <= 0:
                    close = self._last_close.get(i)
                instruments[i] = ModelTargetInstrumentContext(
                    price=price, open_price=open_price, previous_close=close,
                    market_status=tick.market_status,
                    price_source=("auction_indicative" if stage == "opening_auction" else "full_tick_last")
                                 if price is not None else None,
                    price_ts_event=tick.ts_event,
                )
        return ModelTargetPlanningContext(
            trading_date=trading_date, stage=stage, instruments=instruments,
            ts_init=(self._market_status_updated_at_ns if snapshot is None and ts_init is None else ts_init),
        )

    def accept_opening_auction_snapshot(
        self, trading_date: date, snapshot: dict[str, dict[str, Any]], *, ts_init: int,
    ) -> bool:
        """Accept exactly one completed auction batch; missing prices fail the batch."""
        if (trading_date in self._auction_planning_contexts
                or trading_date in self._auction_snapshot_failed_dates):
            return False
        if trading_date != self._clock_date():
            self.log.warning(f"Opening auction snapshot belongs to another day: {trading_date}")
            return False
        self._roll_trading_day(trading_date)
        self.apply_full_tick_snapshot(snapshot, "auction_prepare")
        context = self.build_planning_context(trading_date, stage="opening_auction",
                                              snapshot=snapshot, ts_init=ts_init)
        signal_date = self._resolve_signal_date(trading_date)
        required = self._held_instrument_ids() | {
            str(self._instrument_by_stock[signal["stock_code"]])
            for signal in self._signals_by_date.get(signal_date, [])
            if signal["stock_code"] in self._instrument_by_stock
        }
        if not self._validate_opening_auction_snapshot(context, required, snapshot):
            return False
        self._auction_planning_contexts[trading_date] = context
        return True

    def _validate_opening_auction_snapshot(
        self, context: ModelTargetPlanningContext, required: set[str],
        snapshot: dict[str, dict[str, Any]],
    ) -> bool:
        trading_date = context.trading_date
        suspended = self._suspended_by_date.get(trading_date, set())
        missing = []
        for i in sorted(required):
            inputs = context.instruments.get(i)
            if (inputs is None or inputs.price is None) and (
                    self._stock_code_for_instrument(i) not in suspended
                    and (inputs is None or inputs.market_status != MarketStatusAction.SUSPEND)):
                missing.append(i)
        if missing or not snapshot:
            self.fail_opening_auction_snapshot(trading_date, f"missing last_price: {missing}")
            return False
        return True

    def accept_opening_auction_execution_update(
        self, trading_date: date, *, ts_init: int,
    ) -> bool:
        """Validate updated caches without freezing the execution reference prices."""
        if (trading_date != self._clock_date()
                or trading_date in self._auction_execution_snapshot_dates
                or trading_date in self._auction_snapshot_failed_dates
                or not self._within_auction_submit_time() or trading_date not in self._trading_dates
                or self._trading_paused or self._target_updates_paused or self._replanning_blocked):
            return False
        self._roll_trading_day(trading_date)
        if self._awaiting_pre_open_reconcile():
            self.log.warning("Opening auction execution skipped: pre-open reconciliation is pending")
            return False
        if not self._restore_opening_auction_target(trading_date):
            return False
        if ts_init is None or ts_init <= 0:
            self.fail_opening_auction_snapshot(trading_date, "execution full-market update is not ready")
            return False
        updated_at = pd.Timestamp(ts_init, unit="ns", tz="UTC").tz_convert(self.config.timezone_name)
        if (self._whole_market_status_updated_at_ns != ts_init or not self._whole_market_status
                or updated_at.date() != trading_date
                or updated_at.time() < auction_time(self.config.auction_execution_time)):
            self.fail_opening_auction_snapshot(trading_date, "execution full-market update is not ready")
            return False
        snapshot = {i: dataclasses.asdict(tick) for i, tick in self._whole_market_status.items()}
        self.apply_full_tick_snapshot(snapshot, "auction_execute")
        required = self._held_instrument_ids() | set(self._target_quantities)
        suspended = self._suspended_by_date.get(trading_date, set())
        missing = [i for i in sorted(required)
                   if self._stock_code_for_instrument(i) not in suspended
                   and not self._is_suspended_status(i)
                   and self._select_opening_auction_reference(i)[0] is None]
        if missing:
            self.fail_opening_auction_snapshot(trading_date, f"missing auction reference: {missing}")
            return False
        self._auction_execution_snapshot_dates.add(trading_date)
        return True

    def _restore_opening_auction_target(self, trading_date: date) -> bool:
        """Recover a same-day target before validating execution prices, without planning."""
        with self._trading_action_lock:
            if self._trading_paused or self._target_updates_paused or self._replanning_blocked:
                return False
            has_current_target = self._target_date == trading_date and (
                bool(self._target_version) or trading_date in self._auction_frozen_dates
            )
            if not has_current_target and not self._restore_saved_target(trading_date):
                self.log.warning(f"Opening auction execution skipped: date={trading_date} no accepted or saved target")
                return False
            signal_date = self._resolve_signal_date(trading_date)
            for stock_code, rank in self._prediction_ranks_by_date.get(signal_date, {}).items():
                if stock_code in self._instrument_by_stock:
                    self._auction_prediction_ranks.setdefault(str(self._instrument_by_stock[stock_code]), rank)
            self._auction_frozen_dates.add(trading_date)
            self._processed_dates.add(trading_date)
            return True

    def fail_opening_auction_snapshot(self, trading_date: date, reason: str) -> None:
        self._auction_snapshot_failed_dates.add(trading_date)
        self.log.warning(f"Opening auction skipped: date={trading_date} {reason}")

    def process_opening_auction(self) -> None:
        """Plan or restore the day's fixed auction targets, without submitting orders."""
        now = pd.Timestamp(self.clock.utc_now()).tz_convert(self.config.timezone_name)
        trading_date = now.date()
        if (not self.config.auction_enabled or trading_date not in self._trading_dates
                or not auction_time(self.config.auction_start_time) <= now.time()
                < auction_time(self.config.auction_submit_cutoff)
                or trading_date in self._auction_snapshot_failed_dates
                or self._trading_paused or self._target_updates_paused):
            return
        self._roll_trading_day(trading_date)
        if self._awaiting_pre_open_reconcile():
            self.log.warning("Opening auction skipped: pre-open reconciliation is pending")
            return
        context = self._auction_planning_contexts.get(trading_date)
        if context is None:
            return
        signal_date = self._resolve_signal_date(trading_date)
        if trading_date not in self._auction_frozen_dates:
            self._auction_prediction_ranks = {
                str(self._instrument_by_stock[stock_code]): rank
                for stock_code, rank in self._prediction_ranks_by_date.get(signal_date, {}).items()
                if stock_code in self._instrument_by_stock
            }
            # Target provenance and prior daily processing do not restrict auction
            # execution. Keep an accepted same-day target ahead of persisted state.
            has_current_target = self._target_date == trading_date and bool(self._target_version)
            if not has_current_target and not self._restore_saved_target(trading_date):
                try:
                    plan = self.compute_daily_target_plan(trading_date, planning_context=context)
                    if plan is None or not plan.persistable:
                        return
                    persister = self._live_target_plan_persister
                    if persister is not None and not persister(plan):
                        return
                except Exception as exc:
                    self.log.warning(f"Opening auction planning failed: {exc}")
                    return
                if pd.Timestamp(self.clock.utc_now()).tz_convert(self.config.timezone_name).time() >= auction_time(self.config.auction_submit_cutoff):
                    self.log.warning("Opening auction planning missed submission cutoff")
                    return
                self._frozen_auction_plans[trading_date] = plan
                self._auction_prediction_ranks.update({
                    target.instrument_id: target.target_context.prediction_rank
                    for target in plan.targets if target.target_context.prediction_rank is not None
                })
                self.update_target_quantities(self._target_quantities_from_plan(plan),
                                              target_date=trading_date, reason=plan.reason,
                                              version=self._plan_version(plan), signal_date=plan.signal_date)
            self._auction_frozen_dates.add(trading_date)
            self._processed_dates.add(trading_date)
            self.log.info(
                f"Opening auction target ready: date={trading_date} "
                f"target_version={self._target_version} reason={self._target_reason}",
            )

    def process_opening_auction_execution(self) -> None:
        """Submit fixed targets only after the execution phase's fresh snapshot."""
        trading_date = self._clock_date()
        if (not self._within_auction_submit_time() or trading_date not in self._trading_dates
                or trading_date in self._auction_snapshot_failed_dates
                or self._trading_paused or self._target_updates_paused):
            return
        if (trading_date not in self._auction_frozen_dates or self._target_date != trading_date
                or trading_date not in self._auction_execution_snapshot_dates):
            self.log.warning(f"Opening auction execution skipped: date={trading_date} plan or snapshot is not ready")
            return
        if self._awaiting_pre_open_reconcile():
            self.log.warning("Opening auction execution skipped: pre-open reconciliation is pending")
            return
        self.execute_opening_auction()

    def compute_daily_target_plan(
        self, trading_date: date, *, planning_context: ModelTargetPlanningContext | None = None,
    ) -> ModelTargetPlan | None:
        with self.replanning._lock:
            if self.replanning.busy:
                raise ReplanningBusyError("planning is already in progress")
            self.replanning.initial_in_progress = True
            parent_version = self._target_version or None
        try:
            if self.config.auction_enabled and trading_date in self._frozen_auction_plans:
                return self._frozen_auction_plans[trading_date]
            if planning_context is None:
                planning_context = self.build_planning_context(trading_date)
            if planning_context.trading_date != trading_date:
                raise ValueError("Planning context trading date does not match request")
            plan = self._compute_daily_target_plan(trading_date, planning_context=planning_context)
            if plan is not None:
                plan = dataclasses.replace(plan, parent_version=parent_version)
            if plan is None:
                self.archive_planning_attempt(PlanningAttempt(
                    uuid4().hex, trading_date, "initial", self._target_version,
                    status="keep_current", inputs=self._latest_planning_inputs,
                    error=self._last_planning_error))
            return plan
        finally:
            with self.replanning._lock:
                self.replanning.initial_in_progress = False
            self._scheduled_replan_triggers.wake_pending()

    def _compute_daily_target_plan(
        self, trading_date: date, *, planning_context: ModelTargetPlanningContext | None = None,
    ) -> ModelTargetPlan | None:
        """
        Build the day's target request and return the resulting plan
        **without submitting orders or accepting the target**.

        Two independent inputs are built and filtered separately (no merge):

        * **candidates** — the signal stocks, minus entry-ineligible ones (name
          prefixes, ST, suspension, minimum listed days, missing sizing price).
        * **current_holdings** — the held positions, minus the ones a local hard-exit
          rule (ST / stop-loss / trailing take-profit) fires on. Suspended holdings
          remain frozen in current state.

        Missing signal data and planner failures retain an accepted target. Without
        an accepted target they use the existing transient local safety plan.

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
            planning_context=planning_context,
        )

    def _has_expected_signal_date(self, trading_date: date) -> bool:
        expected_signal_date = previous_trading_date(self._trading_dates, trading_date)
        if expected_signal_date is None:
            raise RuntimeError(f"no previous trading date available for {trading_date}")
        if expected_signal_date in self._signals_by_date:
            return True
        message = (
            "Previous-trading-date signal data is missing; "
            f"{'keeping current targets' if self._target_version else 'using local safety plan'}: "
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
            # Floor only; the authoritative high-water mark is the highest back-adjusted
            # close over the holding span, injected each cycle by
            # _raise_high_price_to_span_peak (which never lowers this value). Put the
            # floor on the adjusted basis too so max(existing, span_peak) never mixes
            # bases; fall back to the raw entry price when the entry-date factor is
            # unavailable.
            entry_row = self._daily_stock_data.get((stock_code, trading_date))
            if entry_row is not None and entry_row.adj_factor is not None and entry_row.adj_factor > 0:
                high_price_floor = float(entry_price) * float(entry_row.adj_factor)
            else:
                high_price_floor = float(entry_price)
            self._active_positions[instrument_id_text] = {
                "entry_date": trading_date,
                "entry_price": entry_price,
                "high_price": high_price_floor,
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
        span_start_date: date | None = None,
        *,
        planning_context: ModelTargetPlanningContext | None = None,
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
        exit_price, price_source = self._exit_price_with_source(instrument_id, planning_context=planning_context)
        if price_source == "prev_close":
            self._log_missing_exit_open_price(
                trading_date=trading_date,
                instrument_id=instrument_id,
                stock_code=self._stock_code_for_instrument(instrument_id),
                fallback_price=exit_price,
            )
        stock_code = self._stock_code_for_instrument(instrument_id)
        cost_price = float(state.get("entry_price") or exit_price or 0.0)
        # Authoritative entry date for the dividend-adjustment logic: the holding
        # span-start date (real flat->held transition from live_position_snapshot).
        # NOT state["entry_date"] — the latter is set to `trading_date` at seed time
        # (restart-safe seeding has no memory of the true open date), so in live it
        # reads as today for every held position. None => no adjustment (ratio 1.0).
        entry_date = span_start_date
        # Trailing take-profit runs entirely on the back-adjusted basis: the peak
        # (over [span_start, T-2]) and the T-1 comparison close are both adjusted, so
        # the activation gate must compare against an adjusted entry cost too.
        trailing_price = self._prev_trading_close(stock_code, trading_date)
        if planning_context is not None:
            # Change only the comparison price. The historical close high-water
            # mark is never ratcheted by intraday observations.
            factor = self._current_adjustment_factor(stock_code, trading_date)
            trailing_price = (exit_price * factor if exit_price is not None and factor is not None
                              else None)
        adjusted_cost = self._adjusted_entry_cost(
            entry_date, stock_code, trading_date, cost_price,
        )
        trailing = self._update_trailing_state(state, trailing_price, adjusted_cost)
        untradable = self._untradable_reason(stock_code, trading_date)
        # Stop-loss: APPLIED decision is still the original raw-cost comparison
        # (today's open vs raw entry cost). The dividend/split-adjusted variant below
        # is computed for SHADOW logging only (not applied) so it can be validated
        # against the running log before switching over.
        stop_triggered = (
            exit_price is not None
            and cost_price > 0
            and exit_price <= cost_price * (1.0 - self.config.stop_loss)
        )
        # Shadow: restate the entry cost onto today's post-ex basis. No ex-event in
        # (entry_date, today] => ratio 1.0 => identical to the raw decision.
        stop_cost_ratio = self._stop_cost_ratio(entry_date, stock_code, trading_date)
        cost_today = cost_price * stop_cost_ratio
        stop_triggered_adjusted = (
            exit_price is not None
            and cost_today > 0
            and exit_price <= cost_today * (1.0 - self.config.stop_loss)
        )
        self._log_stop_loss_shadow(
            stock_code=stock_code,
            trading_date=trading_date,
            exit_price=exit_price,
            cost_price=cost_price,
            cost_today=cost_today,
            stop_cost_ratio=stop_cost_ratio,
            stop_triggered=stop_triggered,
            stop_triggered_adjusted=stop_triggered_adjusted,
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
                "trailing_price": trailing_price,
                "high_price": trailing["high_price"],
                "trailing_stop_price": trailing["stop_price"],
                # open_price / entry_price are raw; trailing_price / high_price /
                # trailing_stop_price are back-adjusted (close * adj_factor). The
                # APPLIED stop-loss compares raw open against raw entry_price.
                # stop_cost_ratio / stop_cost_today / stop_triggered_adjusted are the
                # SHADOW dividend-adjusted variant (not applied), recorded for
                # validation before switching over.
                "trailing_basis": "adjusted",
                "stop_cost_ratio": stop_cost_ratio,
                "stop_cost_today": cost_today,
                "stop_triggered_adjusted": stop_triggered_adjusted,
            },
        )
        return signal_name

    def _log_stop_loss_shadow(
        self,
        *,
        stock_code: str,
        trading_date: date,
        exit_price: float | None,
        cost_price: float,
        cost_today: float,
        stop_cost_ratio: float,
        stop_triggered: bool,
        stop_triggered_adjusted: bool,
    ) -> None:
        """
        Shadow-log the dividend-adjusted stop-loss variant WITHOUT applying it.

        The applied stop-loss still uses the raw entry cost. This records what the
        adjusted-cost variant (cost_today = entry_price * factor_entry/factor_today)
        would have decided, so it can be validated against the running log before the
        adjusted logic is switched on. Divergence between the two decisions is logged
        at WARNING so it is easy to grep; a non-trivial ratio with matching decisions
        is logged at INFO.
        """
        if self.log is None:
            return
        threshold = 1.0 - self.config.stop_loss
        diverged = stop_triggered != stop_triggered_adjusted
        # Only worth logging when a corporate action actually shifts the basis, or the
        # two decisions disagree.
        if not diverged and abs(stop_cost_ratio - 1.0) < 1e-9:
            return
        message = (
            "[stop-loss shadow] "
            f"date={trading_date} stock_code={stock_code} "
            f"open={exit_price} entry_raw={cost_price} "
            f"ratio={stop_cost_ratio:.6f} cost_today={cost_today:.6f} "
            f"threshold={threshold:.4f} "
            f"applied(raw)={stop_triggered} shadow(adjusted)={stop_triggered_adjusted} "
            f"diverged={diverged}"
        )
        if diverged:
            self.log.warning(message, color=LogColor.YELLOW)
        else:
            self.log.info(message, color=LogColor.BLUE)

    def _untradable_reason(self, stock_code: str, trading_date: date) -> str | None:
        if stock_code in self._suspended_by_date.get(trading_date, set()):
            return "suspended"
        if stock_code in self._st_by_date.get(trading_date, set()):
            return "st"
        return None

    def _exit_price_with_source(
        self, instrument_id_text: str, *, planning_context: ModelTargetPlanningContext | None = None,
    ) -> tuple[float | None, str | None]:
        if planning_context is not None:
            return self._open_price_with_source(instrument_id_text, planning_context=planning_context)
        price = self._today_open_price(instrument_id_text)
        if price is not None:
            return price, "open"
        close = self._last_close.get(instrument_id_text)
        return (float(close), "prev_close") if close is not None and close > 0 else (None, None)

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

    def _log_missing_holding_open_price(
        self,
        trading_date: date,
        instrument_id: str,
        stock_code: str,
        quantity: int,
    ) -> None:
        if self.log is None:
            return
        self.log.warning(
            f"dropping held position from current_holdings: no sizing price "
            f"(open and last_close both unavailable) date={trading_date} "
            f"instrument_id={instrument_id} stock_code={stock_code} quantity={quantity}; "
            f"holding will not be sent to the planner and may be liquidated as a non-target",
            color=LogColor.YELLOW,
        )

    def _target_plan(
        self,
        trading_date: date,
        signal_date: date | None,
        degraded_reason: str | None = None,
        *,
        planning_context: ModelTargetPlanningContext | None = None,
    ) -> ModelTargetPlan | None:
        if planning_context is None:
            planning_context = self.build_planning_context(trading_date)
        request, forced_exits = self._target_planning_request(
            trading_date, signal_date, planning_context=planning_context)
        self._latest_planning_inputs = self.capture_planning_inputs(request, forced_exits, planning_context)
        if degraded_reason is not None:
            return self.finish_target_planning(request, forced_exits, planning_context, None,
                                               degraded_reason=degraded_reason)
        try:
            plan = self._target_planner.plan(request)
        except Exception as exc:
            return self.finish_target_planning(request, forced_exits, planning_context, None, error=exc)
        self._latest_planning_inputs["optimizer_result"] = planning_json(plan)
        return self.finish_target_planning(request, forced_exits, planning_context, plan)

    def prepare_target_planning(self, trading_date: date, context: ModelTargetPlanningContext):
        self._seed_active_positions_from_portfolio(trading_date)
        signal_date = self._resolve_signal_date(trading_date)
        degraded = None if self._has_expected_signal_date(trading_date) else "missing_signal_data"
        request, forced_exits = self._target_planning_request(trading_date, signal_date,
                                                             planning_context=context)
        return request, forced_exits, degraded

    def capture_planning_inputs(self, request, forced_exits, context) -> dict[str, Any]:
        quantities = self._held_quantities()
        fills = {}
        for iid in sorted(set(request.active_instrument_ids) | quantities.keys()):
            buy, sell = self.planning_today_fills(iid, request.trading_date)
            fills[iid] = {"buy_qty": str(buy), "sell_qty": str(sell)}
        return {"context": planning_json(context), "request": planning_json(request),
                "forced_exits": planning_json(forced_exits), "actual_holdings": quantities,
                "today_fills": fills}

    def finish_target_planning(self, request, forced_exits, planning_context, plan,
                               *, error=None, degraded_reason=None) -> ModelTargetPlan | None:
        self._last_planning_error = str(error) if error is not None else degraded_reason
        if error is not None:
            self._report_risk_manager_failure(request.trading_date, request.signal_date, error)
        if error is not None or degraded_reason is not None:
            if self._target_version:
                return None
            return self._local_safety_plan(
                request,
                forced_exits,
                degraded_reason or "risk_manager_failure",
                planning_context=planning_context,
            )
        annotated = self._annotate_plan(plan, request, planning_context=planning_context)
        if bool(self.config.local_exit_authoritative):
            annotated = self._apply_forced_exit_overrides(annotated, forced_exits,
                                                         planning_context=planning_context)
        try:
            self._validate_planning_permissions(annotated, request)
        except ValueError as exc:
            return self.finish_target_planning(request, forced_exits, planning_context, None, error=exc)
        return annotated

    @staticmethod
    def _validate_planning_permissions(plan: ModelTargetPlan, request: ModelTargetPlanningRequest) -> None:
        holdings = {holding.instrument_id: holding for holding in request.current_holdings}
        candidates = {candidate.instrument_id: candidate for candidate in request.candidates}
        targets = {target.instrument_id: target.quantity for target in plan.targets}
        if len(targets) != len(plan.targets):
            raise ValueError("planner returned duplicate target instruments")
        for iid in holdings.keys() | candidates.keys():
            holding, candidate = holdings.get(iid), candidates.get(iid)
            quantity = 0 if holding is None else holding.quantity
            target = targets.get(iid, 0)
            if target is None or target < 0:
                raise ValueError(f"planner returned invalid target quantity for {iid}")
            can_buy = (holding is None or holding.can_buy) and (candidate is None or candidate.can_buy)
            can_sell = (holding is None or holding.can_sell) and (candidate is None or candidate.can_sell)
            if (not can_buy and target > quantity) or (not can_sell and target < quantity):
                raise ValueError(f"planner violated trading permissions for {iid}")

    def _report_risk_manager_failure(
        self,
        trading_date: date,
        signal_date: date | None,
        exc: Exception,
    ) -> None:
        message = (
            f"risk-manager planning failed; "
            f"{'keeping current targets' if self._target_version else 'using local safety plan'}: "
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
        forced_exits: dict[str, ForcedExit],
        degraded_reason: str,
        *,
        planning_context: ModelTargetPlanningContext | None = None,
    ) -> ModelTargetPlan:
        targets = [
            self._local_target_info(
                instrument_id,
                forced_exits[instrument_id].target_quantity if instrument_id in forced_exits else quantity,
                planning_context=planning_context,
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
        forced_exits: dict[str, ForcedExit],
        *,
        planning_context: ModelTargetPlanningContext | None = None,
    ) -> ModelTargetPlan:
        if not forced_exits:
            return plan
        targets_by_instrument = {target.instrument_id: target for target in plan.targets}
        for instrument_id in forced_exits:
            targets_by_instrument[instrument_id] = self._local_target_info(
                instrument_id, forced_exits[instrument_id].target_quantity, planning_context=planning_context)
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

    def _local_target_info(
        self, instrument_id: str, quantity: int, *,
        planning_context: ModelTargetPlanningContext | None = None,
    ) -> TargetInfo:
        price, price_source = self._open_price_with_source(instrument_id, planning_context=planning_context)
        stock_code = self._stock_code_for_instrument(instrument_id)
        inputs = None if planning_context is None else planning_context.instruments.get(instrument_id)
        return TargetInfo(
            stock_code=stock_code,
            weight=None,
            quantity=int(quantity),
            target_context=TargetContext(
                price=price,
                price_source=price_source,
                planning_stage=None if planning_context is None else planning_context.stage,
                price_ts_init=None if planning_context is None else planning_context.ts_init,
                current_qty=int(self._current_quantity(InstrumentId.from_str(instrument_id))),
                market_status=(self._market_status_for(instrument_id) if planning_context is None
                               else None if inputs is None else inputs.market_status),
            ),
            target_version=None,
            instrument_id=instrument_id,
            is_locked=self._is_suspended_status(instrument_id),
        )

    def _annotate_plan(
        self,
        plan: ModelTargetPlan,
        request: ModelTargetPlanningRequest,
        *,
        planning_context: ModelTargetPlanningContext | None = None,
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
            inputs = None if planning_context is None else planning_context.instruments.get(target.instrument_id)
            price_source = self._open_price_with_source(target.instrument_id, planning_context=planning_context)[1]
            targets.append(
                dataclasses.replace(
                    target,
                    target_context=TargetContext(
                        price=request.open_prices.get(target.instrument_id),
                        price_source=price_source,
                        price_ts_event=None if inputs is None else inputs.price_ts_event,
                        price_ts_init=None if planning_context is None else planning_context.ts_init,
                        planning_stage=None if planning_context is None else planning_context.stage,
                        prediction_rank=(candidate.rank if candidate is not None else
                                         self._prediction_ranks_by_date.get(request.signal_date, {}).get(
                                             target.stock_code)),
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
                        market_status=(self._market_status_for(target.instrument_id) if planning_context is None
                                       else None if inputs is None else inputs.market_status),
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
        *,
        planning_context: ModelTargetPlanningContext | None = None,
    ) -> tuple[ModelTargetPlanningRequest, dict[str, ForcedExit]]:
        self.refresh_today_fills(trading_date)
        open_prices: dict[str, float] = {}
        forced_exits: dict[str, ForcedExit] = {}
        if bool(self.config.local_exit_authoritative):
            current_holdings = self._build_current_holdings(
                trading_date,
                signal_date,
                open_prices,
                forced_exits=forced_exits,
                planning_context=planning_context,
            )
            candidates = self._build_candidates(
                trading_date,
                signal_date,
                open_prices,
                excluded_instrument_ids=set(forced_exits),
                planning_context=planning_context,
            )
        else:
            # Preserve the pre-switch request path exactly: candidates are built
            # normally, while triggered holdings are only removed from current state.
            candidates = self._build_candidates(trading_date, signal_date, open_prices, planning_context=planning_context)
            current_holdings = self._build_current_holdings(
                trading_date,
                signal_date,
                open_prices,
                forced_exits=forced_exits,
                planning_context=planning_context,
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
        # The sizing basis includes every actual holding, before local exclusions
        # or sellable-quantity reductions applied to current_holdings.
        holdings_value = 0.0
        for instrument_id, quantity in self._held_quantities().items():
            price = open_prices.get(instrument_id)
            if price is None:
                price, _ = self._open_price_with_source(
                    instrument_id, planning_context=planning_context,
                )
            if price is None or not math.isfinite(price) or price <= 0:
                raise ValueError(
                    f"Planning requires a valid price to value holding {instrument_id}",
                )
            holdings_value += quantity * price
        if self._account_id_equals(86904088000):
            investable_asset = float(holdings_value * 1.01)
        else:
            investable_asset = total_asset * (
                1.0 - float(self.config.target_cash_buffer_percent)
            )
        if investable_asset <= 0:
            investable_asset = float(self.config.initial_cash)
        if investable_asset <= holdings_value:
            self.log.warning(
                f"investable_asset <= holdings_value: investable_asset={investable_asset} "
                f"holdings_value={holdings_value} total_asset={total_asset} ")
            investable_asset = float(holdings_value) * 1.005
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
        *,
        planning_context: ModelTargetPlanningContext | None = None,
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
                else self._entry_skip_reason(stock_code, trading_date, planning_context=planning_context)
            )
            inputs = None if planning_context is None else planning_context.instruments.get(instrument_id)
            price = (self._today_open_price(instrument_id) if planning_context is None
                     else None if inputs is None else inputs.price)
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
            can_buy, can_sell = self._planning_permissions(instrument_id, trading_date,
                                                          planning_context=planning_context)
            open_prices[instrument_id] = price
            entry_rank += 1
            candidates.append(
                ModelTargetCandidate(
                    instrument_id=instrument_id,
                    stock_code=normalize_stock_code(stock_code),
                    score=float(signal["score"]),
                    open_price=price,
                    expected_return=float(signal["pred_return_live"]),
                    rank=int(signal["rank"]),
                    can_buy=can_buy,
                    can_sell=can_sell,
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
        forced_exits: dict[str, ForcedExit] | None = None,
        *,
        planning_context: ModelTargetPlanningContext | None = None,
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
        span_start_dates = self._position_span_start_dates(trading_date, held_ids)
        # Candidate signals are truncated by top_frac/max_positions. Holding ranks
        # come from the separately retained, untruncated prediction universe.
        rank_by_stock = (
            self._prediction_ranks_by_date.get(signal_date, {})
            if signal_date
            else {}
        )
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
            inputs = None if planning_context is None else planning_context.instruments.get(instrument_id)
            is_suspended = (
                (self._is_suspended_status(instrument_id) if planning_context is None else
                 inputs is not None and inputs.market_status == MarketStatusAction.SUSPEND)
                or stock_code in self._suspended_by_date.get(trading_date, set())
            )
            if is_suspended:
                recent_target_date = recent_target_dates.get(instrument_id)
                last_close = (self._last_close.get(instrument_id) if planning_context is None
                              else None if inputs is None else inputs.previous_close)
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
                        rank=rank_by_stock.get(stock_code),
                    ),
                )
                continue
            # Seed the trailing high-water mark from the highest close over the current
            # holding span, ending at T-2, before the exit rule runs, so trailing
            # take-profit measures against the true peak (not just the latest close /
            # today's open). The T-1 close is the comparison price tested against the
            # resulting stop, so it is excluded from the peak. Missing span coverage
            # falls back to the existing mark (never regresses).
            prev_trading_date = previous_trading_date(self._trading_dates, trading_date)
            span_end_date = (
                previous_trading_date(self._trading_dates, prev_trading_date)
                if prev_trading_date is not None
                else None
            )
            self._raise_high_price_to_span_peak(
                instrument_id,
                stock_code,
                span_start_dates.get(instrument_id),
                span_end_date,
            )
            exclusion = self._holding_exclusion(
                trading_date,
                signal_date,
                instrument_id,
                exit_rank,
                span_start_dates.get(instrument_id),
                planning_context=planning_context,
            )
            if exclusion is not None:
                remainder = quantity - self.planning_sellable_quantity(instrument_id, trading_date)
                # Stops override today's buy-direction lock, but never a limit
                # or suspension restriction.
                price = None if inputs is None else inputs.price
                limit = price_limit_state(price, *self._price_limits(instrument_id))
                if limit is not None:
                    remainder = quantity
                if forced_exits is not None:
                    forced_exits[instrument_id] = ForcedExit(exclusion, remainder)
                self.log.warning(f"Excluding holding from current_holdings: {instrument_id} reason={exclusion}")
                if remainder <= 0:
                    continue
                quantity = remainder
            price, _ = self._open_price_with_source(instrument_id, planning_context=planning_context)
            if price is None:
                self._log_missing_holding_open_price(
                    trading_date=trading_date,
                    instrument_id=instrument_id,
                    stock_code=stock_code,
                    quantity=quantity,
                )
                continue
            open_prices.setdefault(instrument_id, price)
            recent_target_date = recent_target_dates.get(instrument_id)
            recent_holding_days = self._recent_holding_days(trading_date, recent_target_date)
            can_buy, can_sell = self._planning_permissions(instrument_id, trading_date,
                                                          planning_context=planning_context)
            if exclusion is not None:
                can_buy = can_sell = False
            holdings.append(
                CurrentHolding(
                    instrument_id=instrument_id,
                    stock_code=stock_code,
                    quantity=quantity,
                    price=price,
                    recent_target_date=recent_target_date,
                    recent_holding_days=recent_holding_days,
                    rank=rank_by_stock.get(stock_code),
                    can_buy=can_buy,
                    can_sell=can_sell,
                ),
            )
        self.log.info(
            f"Built current_holdings list with {len(holdings)} items., "
            f"original held={len(held_ids)}",
            color=LogColor.BLUE,
        )
        return holdings

    def _planning_permissions(self, instrument_id: str, trading_date: date, *,
                              planning_context: ModelTargetPlanningContext | None = None) -> tuple[bool, bool]:
        buy_qty, sell_qty = self.planning_today_fills(instrument_id, trading_date)
        can_buy, can_sell = sell_qty == 0, buy_qty == 0
        if planning_context is not None:
            inputs = planning_context.instruments.get(instrument_id)
            price = None if inputs is None else inputs.price
            suspended = inputs is not None and inputs.market_status == MarketStatusAction.SUSPEND
        else:
            tick = self._market_status.get(instrument_id)
            price = self._today_open_price(instrument_id) if tick is None else tick.last_price
            suspended = tick is not None and tick.market_status == MarketStatusAction.SUSPEND
        if suspended or price_limit_state(price, *self._price_limits(instrument_id)) is not None:
            return False, False
        state = self._active_positions.get(instrument_id)
        if state is not None and price is not None:
            cost = float(state.get("entry_price") or 0)
            if cost > 0 and price > cost * (1 + float(self.config.trailing_take_profit_start)):
                can_buy = False
        return can_buy, can_sell

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

    def _position_span_start_dates(
        self,
        trading_date: date,
        held_ids: list[str],
    ) -> dict[str, date]:
        """
        Resolve the start date of each held instrument's current holding span, keyed by
        instrument id.

        Query the configured position-snapshot store (the most recent flat->held
        transition within the trailing window, up to and including today) via the
        injected loader. Without a loader (backtest), fall back to the position's entry
        date from ``_active_positions``.
        """
        result: dict[str, date] = {}
        loader = self._position_span_start_loader
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
                    self.log.warning(
                        f"position span-start loader failed: {exc}",
                        color=LogColor.YELLOW,
                    )
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

    def _adjusted_close(self, stock_code: str, day: date) -> float | None:
        """
        Back-adjusted daily close (``close * adj_factor``) for ``(stock_code, day)``.

        ``adj_factor`` is a cumulative back-adjustment factor, so ``close * adj_factor``
        is a continuous series across dividend/split ex-dates and is directly comparable
        between any two dates. The trailing take-profit runs entirely on this basis so an
        ex-event inside the window does not register as a phantom price move. Returns
        ``None`` when the row, close, or factor is missing/non-positive.
        """
        row = self._daily_stock_data.get((stock_code, day))
        if row is None:
            return None
        if row.close is None or row.close <= 0:
            return None
        if row.adj_factor is None or row.adj_factor <= 0:
            return None
        return float(row.close) * float(row.adj_factor)

    def _adj_factor(self, stock_code: str, day: date) -> float | None:
        """
        Cumulative back-adjustment factor for ``(stock_code, day)`` from
        ``_daily_stock_data``. Returns ``None`` when the row or factor is
        missing/non-positive. Used by the backtest path of ``_stop_cost_ratio``.
        """
        row = self._daily_stock_data.get((stock_code, day))
        if row is None or row.adj_factor is None or row.adj_factor <= 0:
            return None
        return float(row.adj_factor)

    def _stop_cost_ratio(
        self,
        entry_date: date | None,
        stock_code: str,
        trading_date: date,
    ) -> float:
        """
        ``factor_entry / factor_today`` used to restate the raw entry cost onto today's
        post-ex basis for the stop-loss check, so a dividend/split between entry and today
        does not trip a phantom stop. ``1.0`` (no adjustment) when there is no ex-event in
        ``(entry_date, trading_date]`` or the required data is missing.

        ``entry_date`` is the authoritative holding span-start date (from
        ``live_position_snapshot`` in live), NOT the seed-time ``state["entry_date"]``.

        Live (events loader set): product of per-event coefficients for ex-events in
        ``(entry_date, trading_date]``. Backtest (no loader): ``adj_factor(entry_date) /
        adj_factor(trading_date)`` from ClickHouse, which equals that product because
        ``adj_factor`` is the cumulative coefficient.
        """
        if entry_date is None or not stock_code:
            return 1.0
        entry_date = pd.Timestamp(entry_date).date()
        if entry_date >= trading_date:
            return 1.0
        loader = self._divid_events_loader
        if loader is not None:
            try:
                events_by_stock = loader([stock_code]) or {}
            except Exception as exc:
                if self.log is not None:
                    self.log.warning(
                        f"dividend events loader failed: {exc}",
                        color=LogColor.YELLOW,
                    )
                events_by_stock = {}
            events = events_by_stock.get(stock_code)
            if events is None:
                return self._warn_stop_cost_unadjusted(trading_date, stock_code)
            ratio = 1.0
            for ex_date, coef in events:
                ex_date = pd.Timestamp(ex_date).date()
                if entry_date < ex_date <= trading_date and coef and coef > 0:
                    ratio *= float(coef)
            return ratio
        # Backtest fallback: cumulative adj_factor ratio.
        factor_entry = self._adj_factor(stock_code, entry_date)
        factor_today = self._adj_factor(stock_code, trading_date)
        if factor_entry is None or factor_today is None or factor_today <= 0:
            return self._warn_stop_cost_unadjusted(trading_date, stock_code)
        return factor_entry / factor_today

    def _warn_stop_cost_unadjusted(self, trading_date: date, stock_code: str) -> float:
        warning_key = (trading_date, stock_code)
        if warning_key not in self._stop_cost_adjust_warnings:
            self._stop_cost_adjust_warnings.add(warning_key)
            if self.log is not None:
                self.log.warning(
                    "stop-loss using raw entry cost: dividend factor unavailable "
                    f"date={trading_date} stock_code={stock_code}",
                    color=LogColor.YELLOW,
                )
        return 1.0

    def _highest_close_over_span(
        self,
        stock_code: str,
        span_start_date: date | None,
        span_end_date: date | None,
    ) -> float | None:
        """
        Highest back-adjusted daily close in ``self._daily_stock_data`` over the holding
        span ``[span_start_date, span_end_date]`` for ``stock_code``.

        The span end is the second-to-last trading day (T-2), not today: the trailing
        high-water mark is measured through T-2, and the T-1 close is then tested against
        the resulting stop. Returns ``None`` when either bound is unknown or no covered
        positive close is found; warns once per (span_end_date, stock_code) when the span
        is only partially covered so under-coverage (rolling live window / suspension
        gaps) is visible.
        """
        if span_start_date is None or span_end_date is None or not stock_code:
            return None
        span_dates = [
            value
            for value in self._trading_dates
            if span_start_date <= value <= span_end_date
        ]
        if not span_dates:
            return None
        highest: float | None = None
        covered = 0
        for value in span_dates:
            adjusted = self._adjusted_close(stock_code, value)
            if adjusted is None:
                continue
            covered += 1
            if highest is None or adjusted > highest:
                highest = adjusted
        if covered < len(span_dates):
            warning_key = (span_end_date, stock_code)
            if warning_key not in self._span_close_coverage_warnings:
                self._span_close_coverage_warnings.add(warning_key)
                self.log.warning(
                    "trailing high-water span not fully covered by daily close data: "
                    f"span_end={span_end_date} stock_code={stock_code} "
                    f"span_start={span_start_date} covered={covered}/{len(span_dates)}",
                    color=LogColor.YELLOW,
                )
        return highest

    def _raise_high_price_to_span_peak(
        self,
        instrument_id: str,
        stock_code: str,
        span_start_date: date | None,
        span_end_date: date | None,
    ) -> None:
        """
        Raise a held position's tracked ``high_price`` to the highest close over its
        current holding span ``[span_start_date, span_end_date]`` (end = T-2). Never
        lowers the existing mark (missing-data safe): when the span is unknown or
        uncovered, the previously-seeded value stands.
        """
        state = self._active_positions.get(instrument_id)
        if not isinstance(state, dict):
            return
        span_peak = self._highest_close_over_span(stock_code, span_start_date, span_end_date)
        if span_peak is None or span_peak <= 0:
            return
        previous_high = state.get("high_price")
        if previous_high is None or span_peak > float(previous_high):
            state["high_price"] = span_peak

    def _prev_trading_close(self, stock_code: str, trading_date: date) -> float | None:
        """
        Back-adjusted close of the previous trading day (T-1) from
        ``self._daily_stock_data``.

        This is the comparison price for the trailing-take-profit check: the peak is
        measured (adjusted) through T-2, then T-1's adjusted close is tested against the
        resulting stop. Using the adjusted basis on both sides keeps a dividend/split
        ex-date between T-2 and T-1 from registering as a phantom move. Returns ``None``
        when T-1, its close, or its factor is unavailable.
        """
        if not stock_code:
            return None
        prev_date = previous_trading_date(self._trading_dates, trading_date)
        if prev_date is None:
            return None
        return self._adjusted_close(stock_code, prev_date)

    def _current_adjustment_factor(self, stock_code: str, trading_date: date) -> float | None:
        """Restate intraday prices onto the existing adjusted historical basis."""
        if self._divid_events_loader is None:
            return self._adj_factor(stock_code, trading_date)
        # Live reference data ends at T-1. The existing corporate-action loader
        # supplies today's ex-date coefficient: raw_today = raw_prev * ratio,
        # hence adjusted_today = raw_today * factor_prev / ratio.
        previous = previous_trading_date(self._trading_dates, trading_date)
        if previous is None:
            return None
        factor = self._adj_factor(stock_code, previous)
        if factor is None:
            return None
        return factor / self._stop_cost_ratio(previous, stock_code, trading_date)

    def _adjusted_entry_cost(
        self,
        entry_date: date | None,
        stock_code: str,
        trading_date: date,
        cost_price: float,
    ) -> float | None:
        """
        Entry cost on the back-adjusted basis, for the trailing activation gate only.

        The peak and T-1 comparison close are adjusted, so the activation gate
        (``high_price >= cost*(1+activation)``) must compare against an adjusted cost:
        ``entry_price * adj_factor(entry_date)``. ``entry_date`` is the authoritative
        holding span-start date (not the seed-time ``state["entry_date"]``).

        Returns ``None`` when the entry-date factor cannot be resolved (unknown entry
        date, or the entry-date ``adj_factor`` row is absent — e.g. the rolling live
        window predates entry). ``None`` means the gate has no comparable cost and
        trailing MUST NOT arm: comparing the adjusted peak against a raw cost would be a
        basis mismatch that spuriously arms (for a back-adjust factor > 1, always in the
        arming direction) and fires a phantom take-profit. Warns once per
        (trading_date, stock_code) so the missing coverage is visible.
        """
        if cost_price <= 0:
            return None
        if entry_date is not None and stock_code:
            entry_day = pd.Timestamp(entry_date).date()
            # Same-day (or future) entry: no trading day has elapsed since entry, so no
            # ex-event can sit between entry and now — raw cost IS the adjusted cost on
            # this basis (factor ratio 1). Return it directly (no warning; it would
            # otherwise fire for every fresh position).
            if entry_day >= trading_date:
                return cost_price
            row = self._daily_stock_data.get((stock_code, entry_day))
            if row is not None and row.adj_factor is not None and row.adj_factor > 0:
                return cost_price * float(row.adj_factor)
        warning_key = (trading_date, stock_code)
        if warning_key not in self._entry_cost_adjust_warnings:
            self._entry_cost_adjust_warnings.add(warning_key)
            self.log.warning(
                "trailing activation disabled: adj_factor unavailable on entry date "
                f"trading_date={trading_date} stock_code={stock_code} entry_date={entry_date}",
                color=LogColor.YELLOW,
            )
        return None

    def _open_price_with_source(
        self, instrument_id_text: str, *, planning_context: ModelTargetPlanningContext | None = None,
    ) -> tuple[float | None, str | None]:
        if planning_context is not None:
            inputs = planning_context.instruments.get(instrument_id_text)
            if inputs is not None and inputs.price is not None:
                return inputs.price, inputs.price_source
            if planning_context.stage == "opening_auction":
                return None, None
            close = None if inputs is None else inputs.previous_close
        else:
            price = self._today_open_price(instrument_id_text)
            if price is not None:
                return price, "open"
            close = self._last_close.get(instrument_id_text)
        return (float(close), "prev_close") if close is not None and close > 0 else (None, None)

    def _plan_version(self, plan: ModelTargetPlan) -> str:
        signal_text = "none" if plan.signal_date is None else plan.signal_date.isoformat()
        quantities = self._target_quantities_from_plan(plan)
        total = sum(int(qty) for qty in quantities.values())
        version = f"model-{plan.trading_date.isoformat()}-{signal_text}-{len(quantities)}-{total}"
        return f"{version}-{plan.request_id}" if plan.request_id else version

    @staticmethod
    def _target_quantities_from_plan(plan: ModelTargetPlan) -> dict[str, int]:
        return {
            target.instrument_id: int(target.quantity)
            for target in plan.targets
            if target.quantity is not None
        }

    def _entry_skip_reason(
        self, stock_code: str, trading_date: date, *,
        planning_context: ModelTargetPlanningContext | None = None,
    ) -> str | None:
        name_reason = self._name_skip_reason(stock_code)
        if name_reason:
            return name_reason
        instrument = self._instrument_by_stock.get(stock_code)
        inputs = (None if planning_context is None or instrument is None
                  else planning_context.instruments.get(str(instrument)))
        if instrument is not None and (
                self._is_suspended_status(str(instrument)) if planning_context is None else
                inputs is not None and inputs.market_status == MarketStatusAction.SUSPEND):
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
        if self._has_excessive_open_gap(stock_code, trading_date, planning_context=planning_context):
            return "open_gap_up"
        if self._has_consecutive_up_limits(stock_code, trading_date):
            return "consecutive_up_limit"
        return None

    def _has_excessive_open_gap(
        self, stock_code: str, trading_date: date, *,
        planning_context: ModelTargetPlanningContext | None = None,
    ) -> bool:
        threshold = float(self.config.max_open_gap_up)
        if threshold <= 0:
            return False
        instrument_id = self._instrument_by_stock.get(stock_code)
        if instrument_id is None:
            return False
        instrument_id_text = str(instrument_id)
        inputs = None if planning_context is None else planning_context.instruments.get(instrument_id_text)
        if planning_context is None:
            today_open = self._today_open_price(instrument_id_text)
        elif inputs is None:
            today_open = None
        else:
            today_open = inputs.price if planning_context.stage == "opening_auction" else inputs.open_price
        if today_open is None:
            return False
        last_close = (self._last_close.get(instrument_id_text) if planning_context is None
                      else None if inputs is None else inputs.previous_close)
        if last_close is None or last_close <= 0:
            warning_key = (trading_date, stock_code)
            if warning_key not in self._open_gap_warnings:
                self._open_gap_warnings.add(warning_key)
                self.log.warning(
                    "open-gap filter admitted candidate because previous close is missing: "
                    f"date={trading_date} stock_code={stock_code} open={today_open}",
                    color=LogColor.YELLOW,
                )
            return False
        threshold_price = float(last_close) * (1.0 + threshold)
        return today_open >= threshold_price - self._PRICE_COMPARISON_TOLERANCE

    def _has_consecutive_up_limits(self, stock_code: str, trading_date: date) -> bool:
        consecutive_days = int(self.config.consecutive_up_limit_days)
        if consecutive_days <= 0:
            return False

        history_days = consecutive_days
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
        cost_price: float | None,
    ) -> dict[str, Any]:
        result = {"triggered": False, "high_price": state.get("high_price"), "stop_price": None}
        # The high-water mark is the highest close over [span_start, T-2], injected by
        # _raise_high_price_to_span_peak before this runs. close_price here is the T-1
        # close used only to test the stop -- it must NOT ratchet the mark (the peak
        # excludes T-1 by construction).
        high_price = state.get("high_price")
        if high_price is None or float(high_price) <= 0:
            return result
        high_price = float(high_price)
        result["high_price"] = high_price
        if close_price is None or close_price <= 0:
            return result
        trailing_pct = float(self.config.trailing_take_profit)
        # cost_price is the adjusted entry cost; None means the entry-date factor was
        # unavailable (see _adjusted_entry_cost). Both the peak and the T-1 close are on
        # the adjusted basis, so without a comparable adjusted cost the activation gate
        # cannot be evaluated -- do NOT arm (comparing against a raw cost would be a
        # basis mismatch that spuriously fires). Leaving the stop unset here does not
        # touch stop-loss, which runs independently on the raw basis.
        if trailing_pct <= 0 or cost_price is None or cost_price <= 0:
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


def normalize_prediction_ranks(
    ranks_by_date: dict[str, dict[str, int]],
) -> dict[date, dict[str, int]]:
    result: dict[date, dict[str, int]] = {}
    for date_value, ranks in ranks_by_date.items():
        signal_date = pd.Timestamp(date_value).date()
        normalized_ranks: dict[str, int] = {}
        for stock_code, rank in ranks.items():
            normalized = normalize_stock_code(stock_code)
            if not normalized:
                raise ValueError(f"invalid stock code in prediction ranks: {stock_code!r}")
            rank_value = int(rank)
            if rank_value <= 0:
                raise ValueError(
                    f"prediction rank must be positive: {stock_code}={rank_value}",
                )
            normalized_ranks[normalized] = rank_value
        result[signal_date] = normalized_ranks
    return result
