from __future__ import annotations

from contextlib import contextmanager
from collections import deque
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
import asyncio
import json
import threading
from copy import deepcopy

from task_orchestration.models import Proposal, TaskSpec
from task_orchestration.core.orchestrator import TaskOrchestrator
from strategies.convergence import ConvergenceTrigger
from trading_orchestration.configuration.orchestration_config import OrchestrationConfig
from trading_orchestration.adapters.exposure_snapshot_provider import ExposureSnapshotProvider
from trading_orchestration.adapters.operation_state import OperationState


class StrategyBridge:
    """Composition root and explicit boundary to the existing Nautilus strategy."""

    TIMER = "TASK-ORCHESTRATION"

    def __init__(self, host, config: OrchestrationConfig):
        self.host, self.config = host, config
        self._operation_state = OperationState()
        self.started = False
        self.last_trigger = ConvergenceTrigger.TIMER
        self.last_ts_event = None
        self.last_replacement = None
        self.snapshot_provider = ExposureSnapshotProvider(host, config.exposure)
        from trading_orchestration.components.allocations.exposure_order_admission import ExposureOrderAdmission
        self.exposure_admission = ExposureOrderAdmission(config.exposure.tolerance)
        self.exposure_block = None
        self.pending_tick = False
        self._command_lock = threading.Lock()
        self._commands = deque()
        self._command_results = {}
        self._command_sequence = 0
        self._startup_requests = []
        self.last_exposure_assessment = {}
        self.engine = TaskOrchestrator(audit=self._audit)
        self._assemble()
        self._published_state = self.engine.snapshot()

    @property
    def driving(self):
        return self._operation_state.driving

    @driving.setter
    def driving(self, value):
        self._operation_state.driving = value

    @property
    def operations_open(self):
        return self._operation_state.operations_open

    @operations_open.setter
    def operations_open(self, value):
        self._operation_state.operations_open = value

    @property
    def profile_windows(self):
        return self._operation_state.profile_windows

    @profile_windows.setter
    def profile_windows(self, value):
        self._operation_state.profile_windows = value

    @property
    def active_profile(self):
        return self._operation_state.active_profile

    @active_profile.setter
    def active_profile(self, value):
        self._operation_state.active_profile = value

    @property
    def installing(self):
        return self._operation_state.installing

    @installing.setter
    def installing(self, value):
        self._operation_state.installing = value

    def _assemble(self):
        from task_orchestration.core.component_registry import ComponentRegistry
        from trading_orchestration.components.assessors.exposure_assessor import ExposureAssessor
        from trading_orchestration.components.planners.remaining_target_exposure_planner import RemainingTargetExposurePlanner
        from trading_orchestration.components.planners.risk_manager_goal_planner import RiskManagerGoalPlanner
        from trading_orchestration.components.executors.exposure_executor import ExposureExecutor
        from trading_orchestration.components.stabilizers.nautilus_state_stabilizer import NautilusStateStabilizer
        from trading_orchestration.components.outcomes.exposure_outcome_policy import ExposureOutcomePolicy
        from trading_orchestration.components.validators.exposure_validator import ExposureValidator
        from trading_orchestration.components.splitters.existing_order_splitter import ExistingOrderSplitter
        from trading_orchestration.components.cancellations.existing_cancellation_policy import ExistingCancellationPolicy
        from trading_orchestration.configuration.execution_profile import ExecutionProfile
        from trading_orchestration.tasks.bootstrap_task import BootstrapTask
        from trading_orchestration.tasks.normal_execution_task import NormalExecutionTask
        from trading_orchestration.tasks.manual_execution_task import ManualExecutionTask
        from trading_orchestration.tasks.auction_execution_task import AuctionExecutionTask
        from trading_orchestration.tasks.planning_task import PlanningTask
        from trading_orchestration.tasks.exposure_correction_task import ExposureCorrectionTask
        from trading_orchestration.components.triggers.normal_execution_trigger import NormalExecutionTrigger
        from trading_orchestration.components.triggers.exposure_trigger import ExposureTrigger
        from trading_orchestration.components.triggers.price_limit_trigger import PriceLimitTrigger
        registry = ComponentRegistry()
        registry.register("assessor", "position_ratio", lambda: ExposureAssessor(self.config.exposure.tolerance))
        registry.register("planner", "remaining_target", lambda: RemainingTargetExposurePlanner(self.config.exposure))
        registry.register("executor", "nautilus_exposure", lambda: ExposureExecutor(
            self, validator=component("validator"), stabilizer=component("stabilizer")))
        registry.register("stabilizer", "cancel_reconcile", lambda: NautilusStateStabilizer(self))
        registry.register("risk_planner", "risk_manager", lambda: RiskManagerGoalPlanner(self))
        registry.register("outcome", "capacity_replan", ExposureOutcomePolicy)
        registry.register("validator", "formal_remainder", ExposureValidator)
        registry.register("splitter", "existing_notional", lambda: ExistingOrderSplitter(
            self.host.config.order_slice_notional))
        registry.register("cancellation", "existing", lambda: ExistingCancellationPolicy(
            self.host.orchestration_reconcile_existing_orders))
        self.execution_profiles = {
            name: ExecutionProfile(registry.build("splitter", selection["splitter"]),
                                   registry.build("cancellation", selection["cancellation"]))
            for name, selection in self.config.execution_components.items()}
        self.components = registry
        def component(category):
            return registry.build(category, self.config.exposure_components[category])
        # Validate every selected component at composition time, even before a trigger.
        for category, name in self.config.exposure_components.items():
            registry.build(category, name)
        self.engine.register(TaskSpec("bootstrap", 1000, preemptible=False), lambda: BootstrapTask(self),
                             bootstrap=True)
        factories = {
            "daily_planning": lambda: PlanningTask(self, source="initial"),
            "auction_planning": lambda: PlanningTask(self, source="initial", auction=True),
            "normal_execution": lambda: NormalExecutionTask(self),
            "auction_execution": lambda: AuctionExecutionTask(self),
            "price_limit_replan": lambda: PlanningTask(self),
            "scheduled_replan": lambda: PlanningTask(self),
            "manual_replan": lambda: PlanningTask(self, source="manual"),
            "manual_execution": lambda: ManualExecutionTask(self),
            "exposure_correction": lambda: ExposureCorrectionTask(
                self, assessor=component("assessor"), planner=component("planner"),
                executor_factory=lambda: component("executor"),
                stabilizer_factory=lambda: component("stabilizer"),
                risk_planner_factory=lambda: component("risk_planner"),
                outcome_policy=component("outcome")),
        }
        triggers = {"normal_execution": NormalExecutionTrigger(self),
                    "exposure_correction": ExposureTrigger(self),
                    "price_limit_replan": PriceLimitTrigger(self)}
        for name, spec in self.config.tasks.items():
            self.engine.register(spec, factories[name], triggers.get(name))

    def _audit(self, record):
        self.host.log.info("Task decision " + json.dumps(
            {"sequence": record.sequence, "time_ns": record.now_ns, "action": record.action,
             "task": record.task_id, "run": record.run_id, "reason": record.reason,
             "evidence": record.evidence}, ensure_ascii=False, default=str, sort_keys=True))

    def start(self):
        with self.host._trading_action_lock:
            self.engine.start(int(self.host.clock.timestamp_ns()))
            with self._command_lock:
                self.started = True
                requests, self._startup_requests = self._startup_requests, []
            for task_id, proposal in requests:
                self.engine.submit(task_id, proposal)
        self.host.clock.set_timer(self.TIMER, timedelta(seconds=self.config.poll_interval_secs),
                                  callback=lambda _event: self.wake(), fire_immediately=False)

    def wake(self):
        with self._command_lock:
            if not self.started or self.pending_tick:
                return
            self.pending_tick = True
        if self.host._async_scheduler.has_running_loop:
            self.host._async_scheduler.schedule(self._tick_async())
        else:
            self._tick()

    async def _tick_async(self):
        self._tick()

    def _tick(self):
        with self.host._trading_action_lock:
            with self._command_lock:
                self.pending_tick = False
                if not self.started:
                    return
                commands = list(self._commands)
                self._commands.clear()
            for command_id, action, arguments in commands:
                try:
                    if action == "enable":
                        self.engine.set_enabled(arguments["task_id"], arguments["enabled"])
                    elif action == "cancel":
                        self.engine.cancel(arguments["run_id"])
                    elif action == "pause":
                        self.engine.paused = True
                    elif action == "resume":
                        self.engine.paused = False
                    elif action == "reevaluate":
                        self.engine.reevaluate()
                    result = {"status": "applied", "action": action}
                except Exception as exc:
                    result = {"status": "rejected", "reason": str(exc), "action": action}
                with self._command_lock:
                    self._command_results[command_id] = result
            self.engine.tick(int(self.host.clock.timestamp_ns()))
            published = {**self.engine.snapshot(), "exposure": dict(self.last_exposure_assessment),
                         "execution": self.execution_state()}
            with self._command_lock:
                self._published_state = published

    def submit_control(self, action, arguments):
        allowed = {"enable": {"task_id", "enabled"}, "cancel": {"run_id"},
                   "pause": set(), "resume": set(), "reevaluate": set()}
        if action not in allowed or set(arguments) != allowed[action]:
            raise ValueError("invalid scheduler command or arguments")
        if action == "enable" and not isinstance(arguments["enabled"], bool):
            raise ValueError("enabled must be boolean")
        with self._command_lock:
            self._command_sequence += 1
            command_id = f"control-{self._command_sequence}"
            self._command_results[command_id] = {"status": "accepted", "action": action}
            self._commands.append((command_id, action, dict(arguments)))
        self.wake()
        return command_id

    def control_results(self):
        with self._command_lock:
            return {key: dict(value) for key, value in self._command_results.items()}

    def state(self):
        with self._command_lock:
            return deepcopy(self._published_state)

    def stop(self):
        # The Nautilus stop callback cannot await late venue reports. Revoke new
        # work immediately and attempt cleanup once; bootstrap reconciles on restart.
        with self.host._trading_action_lock:
            with self._command_lock:
                self.started = False
            self.engine.stop()
            self.engine.tick(int(self.host.clock.timestamp_ns()))
            with self._command_lock:
                self._published_state = self.engine.snapshot()
            if self.TIMER in self.host.clock.timer_names:
                self.host.clock.cancel_timer(self.TIMER)

    @contextmanager
    def pricing_windows(self, windows):
        previous = self.profile_windows
        self.profile_windows = windows
        try:
            yield
        finally:
            self.profile_windows = previous

    @contextmanager
    def operations(self, *, windows=None):
        old = self.driving, self.operations_open, self.profile_windows, self.active_profile
        old_splitter = self.host._order_splitter
        self.driving, self.operations_open = True, True
        self.profile_windows = windows
        self.active_profile = self.execution_profiles.get(self.engine.owner_task())
        if self.active_profile is not None:
            self.host._order_splitter = self.active_profile.splitter
        try:
            yield
        finally:
            self.driving, self.operations_open, self.profile_windows, self.active_profile = old
            self.host._order_splitter = old_splitter

    def local_now(self):
        return self.host.clock.utc_now().astimezone(ZoneInfo(self.host.config.timezone_name))

    def in_windows(self, windows, ts_event=None):
        host, now = self.host, self.local_now()
        if now.date() not in host._trading_dates:
            return False
        index = host._time_window_index(now.time(), windows)
        stamp = ts_event or host._last_market_data_ts_event
        if index is None or not stamp or stamp > host.clock.timestamp_ns():
            return False
        event = datetime.fromtimestamp(stamp / 1e9, ZoneInfo(host.config.timezone_name))
        return (event.date() == now.date()
                and host._time_window_index(event.time(), windows) == index
                and host._time_window_index(now.time(), host.config.exchange_trading_windows) is not None)

    def exposure_allowed(self):
        return (self.in_windows(self.config.exposure.windows)
                and self.host.automatic_replanning_allowed()
                and not self.host._awaiting_pre_open_reconcile())

    def deadline(self, *, auction=False):
        now = self.local_now()
        if auction:
            end = time.fromisoformat(self.host.config.auction_submit_cutoff)
        else:
            end = time(11, 30) if now.time() < time(11, 30) else time(15)
        return int(datetime.combine(now.date(), end, now.tzinfo).timestamp() * 1e9)

    def request(self, task_id, payload=None, *, key=None, expires_ns=None):
        proposal = Proposal(key or f"{task_id}:{self.host._clock_date()}", payload, expires_ns)
        with self._command_lock:
            if not self.started and self.engine.lifecycle == "initializing":
                self._startup_requests.append((task_id, proposal))
                return None
        run_id = self.engine.submit(task_id, proposal)
        self.wake()
        return run_id

    def execution_requested(self, trigger, ts_event=None, replacement=None):
        with self._command_lock:
            self.last_trigger, self.last_ts_event, self.last_replacement = trigger, ts_event, replacement
        if trigger == ConvergenceTrigger.OPENING_AUCTION:
            self.request("auction_execution", expires_ns=self.deadline(auction=True))
        elif trigger in (ConvergenceTrigger.MANUAL_SELL, ConvergenceTrigger.MANUAL_SELL_ALL):
            self.request("manual_execution", trigger, expires_ns=self.deadline())
        else:
            self.wake()
        return False

    def install_model(self, plan):
        host = self.host
        with self.operations():
            old = host._convergence_suspended
            host._convergence_suspended = True
            self.installing = True
            try:
                host.trading_state.planned_models[host._plan_version(plan)] = plan
                host.accept_model_target_plan(plan, bypass_pause=True)
            finally:
                self.installing = False
                host._convergence_suspended = old

    def install_quantities(self, quantities, version, reason, *, temporary=False):
        host = self.host
        with self.operations():
            old = host._convergence_suspended
            host._convergence_suspended = True
            self.installing = True
            try:
                host.update_target_quantities(quantities, host._clock_date(), reason, version=version,
                                              signal_date=host._target_signal_date,
                                              bypass_pause=True, bypass_replanning=True)
            finally:
                self.installing = False
                host._convergence_suspended = old

    def restore_formal(self):
        plan = self.host.trading_state.formal_model
        if plan is not None and self.host._target_version != self.host._plan_version(plan):
            self.install_model(plan)

    def execute(self, *, exposure=False, auction=False, trigger=None):
        host = self.host
        windows = self.config.exposure.windows if exposure else None
        with self._command_lock:
            last_trigger, last_ts_event, last_replacement = (
                self.last_trigger, self.last_ts_event, self.last_replacement)
        trigger = trigger or (ConvergenceTrigger.OPENING_AUCTION if auction else
                             ConvergenceTrigger.TIMER if exposure else last_trigger)
        if trigger == ConvergenceTrigger.OPENING_AUCTION and not auction:
            trigger = ConvergenceTrigger.TIMER
        with self.operations(windows=windows):
            host._converge_to_target(current_date=host._clock_date(), trigger=trigger,
                                     ts_event=host._last_market_data_ts_event if exposure else last_ts_event,
                                     replacement=None if exposure else last_replacement)

    def target_satisfied(self):
        host = self.host
        from nautilus_trader.model.identifiers import InstrumentId
        if host.replanning_orders() or host._pending_replacements:
            return False
        return all(host._current_quantity(InstrumentId.from_str(iid)) == quantity
                   for iid, quantity in host._desired_quantities().items())

    def execution_state(self):
        from nautilus_trader.model.identifiers import InstrumentId
        host = self.host
        return {"version": host._target_version, "reason": host._target_reason,
                "quantities": {key: str(value) for key, value in host._desired_quantities().items()},
                "remaining": {key: str(value - host._current_quantity(InstrumentId.from_str(key)))
                              for key, value in host._desired_quantities().items()},
                "pending_orders": len(host.replanning_orders()),
                "frozen": dict(host._frozen_instruments), "complete": self.target_satisfied()}

    def bootstrap_load(self):
        host, day = self.host, self.host._clock_date()
        with self.operations():
            old = host._convergence_suspended
            host._convergence_suspended = True
            self.installing = True
            try:
                if host._target_date != day or not host._target_version:
                    if host._committed_plan_loader is not None:
                        saved = host._committed_plan_loader(day)
                        if saved is not None:
                            host.restore_committed_plan(day, saved)
            finally:
                self.installing = False
                host._convergence_suspended = old

    def reject_unowned_operation(self):
        if not self.operations_open:
            raise RuntimeError("trading operation requires the active orchestration owner")

