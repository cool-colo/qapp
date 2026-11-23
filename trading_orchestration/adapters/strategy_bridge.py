from __future__ import annotations

from contextlib import contextmanager
from collections import Counter, deque
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
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
        self._execution_passes = Counter()
        self._depth_ack_pending = False
        self._depth_ack_event_ns = None
        self.snapshot_provider = ExposureSnapshotProvider(host, config.exposure)
        from trading_orchestration.components.allocations.exposure_order_admission import ExposureOrderAdmission
        self.exposure_admission = ExposureOrderAdmission(config.exposure.tolerance)
        self.exposure_block = None
        self.pending_tick = False
        self._command_lock = threading.Lock()
        self._commands = deque()
        self._command_results = {}
        self._command_sequence = 0
        self._tick_running = False
        self._timer_pending = False
        self._event_pending = False
        self.execution_not_before_ns = 0
        self.last_exposure_assessment = {}
        self.engine = TaskOrchestrator(
            stabilizer_factory=lambda _scope: self.components.build("stabilizer", self.config.cleanup_component),
            audit=self._audit)
        self._assemble()
        from trading_orchestration.adapters.scheduler_description import describe_scheduler
        self._configuration_view = describe_scheduler(config, host.config)
        self._published_state = {**self.engine.snapshot(), "planning_status": self._planning_state()}

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
            self, validator=component("validator")))
        registry.register("stabilizer", "cancel_reconcile", lambda: NautilusStateStabilizer(
            self, reconciliation_enabled=self.config.cleanup_reconciliation_enabled))
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
        registry.build("stabilizer", self.config.cleanup_component)
        def component(category):
            return registry.build(category, self.config.exposure_components[category])
        # Validate every selected component at composition time, even before a trigger.
        for category, name in self.config.exposure_components.items():
            registry.build(category, name)
        self.engine.register(TaskSpec("bootstrap", 1000, preemptible=False), lambda: BootstrapTask(self),
                             bootstrap=True)
        from trading_orchestration.components.planners.auction_goal_planner import AuctionGoalPlanner
        from trading_orchestration.components.planners.initial_goal_planner import InitialGoalPlanner
        def risk_goal(source='automatic'):
            planner = RiskManagerGoalPlanner(self, source=source)
            return InitialGoalPlanner(self, planner) if source == 'initial' else planner
        registry.register('goal_planner', 'risk_manager', risk_goal)
        registry.register('goal_planner', 'auction_goal', lambda source='initial': AuctionGoalPlanner(
            self, self.host.market_snapshots, self.host.planning_context_builder,
            lambda snapshot: RiskManagerGoalPlanner(self, source='initial', planning_context=snapshot)))
        for selected in self.config.planning_components.values():
            registry.build('goal_planner', selected)
        def planning(name, source='automatic'):
            planner = registry.build('goal_planner', self.config.planning_components[name], source=source)
            return PlanningTask(self, planner, source=source, scheduled=name == 'scheduled_replan')
        factories = {
            'daily_planning': lambda: planning('daily_planning', 'initial'),
            'auction_planning': lambda: planning('auction_planning', 'initial'),
            'normal_execution': lambda: NormalExecutionTask(self),
            'auction_execution': lambda: AuctionExecutionTask(self),
            'price_limit_replan': lambda: planning('price_limit_replan'),
            'scheduled_replan': lambda: planning('scheduled_replan'),
            'manual_replan': lambda: planning('manual_replan', 'manual'),
            'manual_execution': lambda: ManualExecutionTask(self),
            'exposure_correction': lambda: ExposureCorrectionTask(
                self, assessor=component('assessor'), planner=component('planner'),
                executor_factory=lambda: component('executor'),
                risk_planner_factory=lambda: component('risk_planner'),
                outcome_policy=component('outcome')),
        }
        from task_orchestration.components.triggers.scheduled_trigger import ScheduledTrigger
        from trading_orchestration.adapters.timer_clock import NautilusTimerClock
        from strategies.replan_triggers import normalize_scheduled_replan_times
        schedules = {
            'auction_planning': ((self.host.config.auction_start_time, self.host.config.auction_submit_cutoff),),
            'auction_execution': ((self.host.config.auction_execution_time, self.host.config.auction_submit_cutoff),),
            'scheduled_replan': tuple((value, '11:30:00' if time.fromisoformat(value) < time(11, 30)
                                      else '15:00:00') for value in normalize_scheduled_replan_times(
                                          self.host.config.scheduled_replan_times)),
        }
        self.scheduled_triggers = {name: ScheduledTrigger(name, NautilusTimerClock(self.host),
            self.host.config.timezone_name, schedule, self._receive_scheduled)
            for name, schedule in schedules.items()}
        triggers = {'normal_execution': NormalExecutionTrigger(self),
                    'exposure_correction': ExposureTrigger(self),
                    'price_limit_replan': PriceLimitTrigger(self)}
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
        for name in self.scheduled_triggers:
            self._update_scheduled(name, self.config.tasks[name].enabled)
        self.host.clock.set_timer(self.TIMER, timedelta(seconds=self.config.poll_interval_secs),
                                  callback=lambda _event: self._on_timer(), fire_immediately=False)

    def _update_scheduled(self, name, enabled):
        if name not in self.scheduled_triggers:
            return
        trigger = self.scheduled_triggers[name]
        if enabled and (not name.startswith('auction_') or self.host.config.auction_enabled):
            trigger.start()
        else:
            trigger.stop()

    def date_at(self, stamp):
        return datetime.fromtimestamp(stamp / 1e9, ZoneInfo(self.host.config.timezone_name)).date()

    def _receive_scheduled(self, occurrence):
        from strategies.replan_triggers import SessionReplanValidity
        zone = ZoneInfo(self.host.config.timezone_name)
        payload = (SessionReplanValidity(datetime.fromtimestamp(occurrence.scheduled_at_ns / 1e9, zone),
                                        datetime.fromtimestamp(occurrence.expires_at_ns / 1e9, zone))
                   if occurrence.trigger_id == 'scheduled_replan' else None)
        proposal = Proposal(occurrence.key, payload, occurrence.expires_at_ns,
                            {'scheduled_at_ns': occurrence.scheduled_at_ns,
                             'fired_at_ns': occurrence.fired_at_ns, 'trigger_id': occurrence.trigger_id})
        with self._command_lock:
            if not self.started:
                return
            self._command_sequence += 1
            self._commands.append((f'schedule-{self._command_sequence}', 'submit',
                                   {'task_id': occurrence.trigger_id, 'proposal': proposal}))
        self.wake_event()

    def wake_event(self):
        # Defer even under TestClock: equal-time triggers enter one batch, and
        # event callbacks can never re-enter the current business execution pass.
        with self._command_lock:
            if not self.started or self._event_pending:
                return
            self._event_pending = True
        if self.host._async_scheduler.has_running_loop:
            self.host._async_scheduler.schedule(self._event_async())
        else:
            from trading_orchestration.adapters.timer_clock import NautilusTimerClock
            clock = NautilusTimerClock(self.host)
            clock.arm('TASK-ORCHESTRATION-EVENT', clock.now_ns() + 1, self._event_tick)

    async def _event_async(self):
        self._event_tick()

    def _event_tick(self):
        with self._command_lock:
            self._event_pending = False
        self._tick(evaluate_triggers=False)

    def wake(self):
        # Event callbacks only mark work. The timer owns the scheduling boundary
        # in both replay and live; never run detection inside an execution loop.
        with self._command_lock:
            self.pending_tick = True

    def _on_timer(self):
        with self._command_lock:
            if not self.started or self._timer_pending:
                return
            self._timer_pending = True
        if self.host._async_scheduler.has_running_loop:
            self.host._async_scheduler.schedule(self._tick_async())
        else:
            try:
                self._tick()
            finally:
                with self._command_lock:
                    self._timer_pending = False

    async def _tick_async(self):
        try:
            self._tick()
        finally:
            with self._command_lock:
                self._timer_pending = False

    def _tick(self, *, evaluate_triggers=True):
        with self.host._trading_action_lock:
            if self._tick_running:
                self.wake()
                return
            self._tick_running = True
            try:
                self._tick_once(evaluate_triggers=evaluate_triggers)
            finally:
                self._tick_running = False

    def _tick_once(self, *, evaluate_triggers=True):
        with self.host._trading_action_lock:
            with self._command_lock:
                self.pending_tick = False
                if not self.started:
                    return
                commands = list(self._commands)
                self._commands.clear()
            def receive_commands():
                for command_id, action, arguments in commands:
                    try:
                        run_id = None
                        if action == "submit":
                            run_id = self.engine.submit(arguments["task_id"], arguments["proposal"])
                        elif action == "enable":
                            self.engine.set_enabled(arguments["task_id"], arguments["enabled"])
                            self._update_scheduled(arguments["task_id"], arguments["enabled"])
                        elif action == "cancel":
                            self.engine.cancel(arguments["run_id"])
                        elif action == "pause":
                            self.engine.paused = True
                        elif action == "resume":
                            self.engine.paused = False
                        elif action == "reevaluate":
                            self.engine.reevaluate()
                        result = {"status": "applied", "action": action}
                        if action == "submit":
                            result["run_id"] = run_id
                    except Exception as exc:
                        result = {"status": "rejected", "reason": str(exc), "action": action}
                    with self._command_lock:
                        self._command_results[command_id] = result
            self.engine.tick(int(self.host.clock.timestamp_ns()), ingress=receive_commands,
                             evaluate_triggers=evaluate_triggers)
            self._acknowledge_idle_depth()
            try:
                execution = self.execution_state()
            except Exception as exc:
                execution = {"quality_error": f"{type(exc).__name__}: {exc}"}
            published = {**self.engine.snapshot(), "exposure": dict(self.last_exposure_assessment),
                         "execution": execution, "planning_status": self._planning_state()}
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
        # The owner publishes a new graph by reference replacement and never
        # mutates an old graph. Capture that reference once; HTTP readers neither
        # hold a trading lock nor wait for an in-progress scheduling turn.
        published = self._published_state
        return {**deepcopy(published), "enabled": True,
                "configuration": deepcopy(self._configuration_view)}

    def _planning_state(self):
        attempts = list(self.host.planning_attempts.values())
        last = attempts[-1] if attempts else None
        active = next((item for item in reversed(attempts) if item["status"] == "planning"), None)
        day = self.host._target_date
        counts = Counter(item["source"] for item in attempts
                         if item["trading_date"] == str(day))
        return {"auto_replan_enabled": self.host._auto_replan_enabled,
                "replanning": active is not None, "planning": active,
                "last_planning": last, "replan_date": str(day) if day else None,
                "replan_counts": {key: counts[key] for key in ("automatic", "manual")},
                'scheduled_replan_next_times': self.scheduled_triggers['scheduled_replan'].state()['next_times'],
                'scheduled_triggers': {name: trigger.state() for name, trigger in self.scheduled_triggers.items()}}

    def planning_state(self):
        return deepcopy(self._published_state["planning_status"])

    def published_actions(self, *, after_sequence=0, limit=100):
        if (not isinstance(after_sequence, int) or isinstance(after_sequence, bool)
                or after_sequence < 0 or not isinstance(limit, int) or isinstance(limit, bool)
                or not 1 <= limit <= 1000):
            raise ValueError("invalid action query range")
        # The journal is append-only with consecutive sequence numbers. Only
        # expose the prefix covered by a completed, published scheduling turn.
        revision = self._published_state["revision"]
        records = self.engine.records[after_sequence:min(revision, after_sequence + limit)]
        return [{"sequence": r.sequence, "time_ns": r.now_ns, "action": r.action,
                 "task_id": r.task_id, "run_id": r.run_id, "reason": r.reason,
                 "evidence": deepcopy(r.evidence)} for r in records]

    def stop(self):
        # The Nautilus stop callback cannot await late venue reports. Revoke new
        # work immediately and attempt cleanup once; bootstrap reconciles on restart.
        with self.host._trading_action_lock:
            with self._command_lock:
                self.started = False
            for trigger in self.scheduled_triggers.values():
                trigger.stop()
            if "TASK-ORCHESTRATION-EVENT" in self.host.clock.timer_names:
                self.host.clock.cancel_timer("TASK-ORCHESTRATION-EVENT")
            self.engine.stop()
            self.engine.tick(int(self.host.clock.timestamp_ns()))
            with self._command_lock:
                self._published_state = {**self.engine.snapshot(), "planning_status": self._planning_state()}
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
        mode = self.engine.operation_phase()
        if mode not in {"execute", "cleanup", "release"}:
            raise RuntimeError("trading operation requires the active orchestration owner")
        old_mode = self._operation_state.mode
        self._operation_state.mode = mode
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
            self._operation_state.mode = old_mode

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
            if self.engine.lifecycle in {"stopping", "stopped"}:
                return None
            self._command_sequence += 1
            request_id = f"command-{self._command_sequence}"
            self._command_results[request_id] = {"status": "accepted", "action": "submit"}
            self._commands.append((request_id, "submit", {"task_id": task_id, "proposal": proposal}))
        self.wake()
        return request_id

    def execution_requested(self, trigger, ts_event=None, replacement=None, *, delay_secs=0):
        with self._command_lock:
            completed_before = self._execution_passes[trigger]
            if trigger == ConvergenceTrigger.ORDER_BOOK_DEPTH:
                self._depth_ack_pending = True
                self._depth_ack_event_ns = ts_event
            self.last_trigger, self.last_ts_event, self.last_replacement = trigger, ts_event, replacement
            if delay_secs:
                self.execution_not_before_ns = max(self.execution_not_before_ns,
                    int(self.host.clock.timestamp_ns()) + int(delay_secs * 1e9))
        self.wake()
        with self._command_lock:
            completed = self._execution_passes[trigger] > completed_before
        return completed

    def _acknowledge_idle_depth(self):
        # This is a completion assessment too: only inspect facts at the cycle
        # boundary, never inside a market callback or an order submission loop.
        with self._command_lock:
            pending, ts_event = self._depth_ack_pending, self._depth_ack_event_ns
        if (pending and self.engine.lifecycle == "ready" and self.engine.owner_task() is None
                and (self.host._within_execution_window(ts_event)
                     or self.host._within_cancel_window(ts_event))
                and self.target_satisfied()):
            self.host._depth_converged_today = True
            with self._command_lock:
                self._depth_ack_pending = False

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
        if host.clock.timestamp_ns() < self.execution_not_before_ns:
            return False
        windows = self.config.exposure.windows if exposure else None
        with self._command_lock:
            last_trigger, last_ts_event, last_replacement = (
                self.last_trigger, self.last_ts_event, self.last_replacement)
        trigger = trigger or (ConvergenceTrigger.OPENING_AUCTION if auction else
                             ConvergenceTrigger.TIMER if exposure else last_trigger)
        if trigger == ConvergenceTrigger.OPENING_AUCTION and not auction:
            trigger = ConvergenceTrigger.TIMER
        with self.operations(windows=windows):
            completed = host._converge_to_target(
                current_date=host._clock_date(), trigger=trigger,
                ts_event=host._last_market_data_ts_event if exposure else last_ts_event,
                replacement=None if exposure else last_replacement)
            if completed:
                with self._command_lock:
                    self._execution_passes[trigger] += 1
                if trigger == ConvergenceTrigger.ORDER_BOOK_DEPTH:
                    # Deferred dispatch cannot return its result to the original
                    # depth callback; acknowledge on the serialized owner loop.
                    host._depth_converged_today = True
                    with self._command_lock:
                        self._depth_ack_pending = False
            return completed

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

    def discard_execution_requests(self):
        """Cancel deferred intentions, never accounting or order event history."""
        self.host._pending_replacements.clear()
        self.host._deferred_buys.clear()
        with self._command_lock:
            self.last_replacement = None
            self.last_trigger = ConvergenceTrigger.TIMER
            self.execution_not_before_ns = 0

    def reject_unowned_operation(self, operation="submit"):
        if (not self.operations_open or self._operation_state.mode == "release"
                or (self._operation_state.mode == "cleanup" and operation != "cancel")):
            raise RuntimeError("trading operation requires the active orchestration owner")

