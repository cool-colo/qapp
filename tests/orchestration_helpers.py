"""Drive the actual planning component and framework without venue I/O."""
from types import SimpleNamespace
from unittest.mock import Mock

from task_orchestration.models import Proposal, TaskContext
from trading_orchestration.components.planners.risk_manager_goal_planner import RiskManagerGoalPlanner


def evaluate_plan(strategy, trading_date, *, planning_context=None):
    """Capture a component result for business-rule tests, without installing it."""
    bridge = SimpleNamespace(host=strategy, install_model=Mock())
    component = RiskManagerGoalPlanner(bridge, source="initial", planning_context=planning_context)
    now = int(strategy.clock.timestamp_ns())
    component.plan(TaskContext(now, 1, "test-plan", 1, Proposal("test")))
    return bridge.install_model.call_args.args[0] if bridge.install_model.called else None


def cycle(strategy, now_ns):
    strategy.clock.set_time(now_ns)
    strategy.orchestration._tick()


def start_runtime(strategy):
    strategy.orchestration.start()
    now = strategy.clock.timestamp_ns()
    cycle(strategy, now + 500_000_000)
    cycle(strategy, now + 1_000_000_000)


def committed_archive(values):
    """Build a complete committed archive fixture, including its formal model."""
    from datetime import date, timedelta
    from strategies.model_target_planners import ModelTargetPlan, TargetInfo, TargetContext
    from strategies.replanning import planning_json
    signal = values['signal_date']
    if isinstance(signal, str):
        signal = date.fromisoformat(signal)
    day = values.get('trading_date', signal + timedelta(days=1))
    model = ModelTargetPlan(
        trading_date=day, signal_date=signal, reason=values['reason'],
        targets=[TargetInfo(stock_code=i.rsplit('.', 1)[0], instrument_id=i, quantity=q,
                            weight=None, target_version=values['version'], is_locked=False,
                            target_context=TargetContext()) for i, q in values['quantities'].items()])
    return {**values, 'plan': planning_json(model)}


def advance_clock(strategy, now_ns):
    """Deliver TestClock callbacks at their event time, as Nautilus does."""
    for handler in strategy.clock.advance_time(now_ns, set_time=False):
        strategy.clock.set_time(handler.event.ts_event)
        handler.handle()
    strategy.clock.set_time(now_ns)


def flush_requests(strategy, seconds=2):
    if not strategy.orchestration.started:
        strategy.orchestration.start()
    advance_clock(strategy, strategy.clock.timestamp_ns() + int(seconds * 1e9))


def auction_contexts(strategy):
    """Inspect the planner-owned frozen inputs, never a strategy cache."""
    return {run.process.planner.snapshot.trading_date: run.process.planner.snapshot
            for run in strategy.orchestration.engine._runs.values()
            if run.task_id == 'auction_planning' and run.process is not None
            and run.process.planner.snapshot is not None}
