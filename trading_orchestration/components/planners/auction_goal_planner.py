from __future__ import annotations

from task_orchestration.models import Decision, RunState
from strategies.opening_auction import auction_time
from trading_orchestration.services.whole_market_snapshot import SnapshotProvider
from trading_orchestration.components.planners.planning_context_builder import PlanningContextProvider


class AuctionGoalPlanner:
    """Prepare one frozen auction context, then delegate formal goal planning."""

    def __init__(self, bridge, snapshots: SnapshotProvider,
                 context_builder: PlanningContextProvider, planner_factory):
        self.bridge, self.snapshots, self.builder = bridge, snapshots, context_builder
        self.planner_factory = planner_factory
        self.request = None
        self.delegate = None
        self.snapshot = None
        self.evidence = {}
        self.epoch = None
        self.invalidated = False

    def _failure(self, day, reason):
        self.bridge.host.auction_session.fail(day, reason)
        return Decision(RunState.FAILED, reason, self.evidence)

    def plan(self, context):
        host = self.bridge.host
        now = self.bridge.local_now()
        day = now.date()
        if self.invalidated or (self.epoch is not None and self.epoch != context.owner_epoch):
            return Decision(RunState.CANCELLED, 'planning_authorization_expired')
        if (not host.config.auction_enabled or day not in host._trading_dates
                or not auction_time(host.config.auction_start_time) <= now.time()
                < auction_time(host.config.auction_submit_cutoff)):
            self.abort('planning_window_closed')
            return Decision(RunState.EXPIRED, 'planning_window_closed')
        if host._trading_paused or host._target_updates_paused:
            return Decision(RunState.WAITING, 'user_paused')
        if day in host.auction_session.failed_dates:
            return Decision(RunState.FAILED, host.auction_session.failed_dates[day])
        if self.request is None:
            host._roll_trading_day(day)
            self.epoch = context.owner_epoch
            self.request = self.snapshots.request_fresh('auction_prepare')
            self.request.add_done_callback(lambda _: self.bridge.wake_event())
        if not self.request.done():
            return Decision(RunState.WAITING, 'auction_planning_market_pending')
        if self.snapshot is None:
            try:
                batch = self.request.result()
            except Exception as exc:
                return self._failure(day, f'auction_planning_market_failed: {exc}')
            self.evidence = {'market_request_id': batch.request_id,
                             'market_requested_at_ns': batch.requested_at_ns,
                             'market_ready_at_ns': batch.completed_at_ns}
            if self.bridge.date_at(batch.requested_at_ns) != day:
                return self._failure(day, 'auction_planning_market_wrong_date')
            fields = batch.fields()
            host.apply_full_tick_snapshot(fields, 'auction_prepare')
            snapshot = self.builder.build(day, stage='opening_auction', snapshot=fields,
                                          ts_init=batch.completed_at_ns)
            if not fields:
                return self._failure(day, 'invalid_auction_planning_snapshot')
            self.snapshot = snapshot
            if host._target_date != day or not host._target_version:
                self.delegate = self.planner_factory(snapshot)
        result = (Decision(RunState.SUCCEEDED, 'reuse_current_target') if self.delegate is None
                  else self.delegate.plan(context))
        if result.state == RunState.SUCCEEDED:
            host._processed_dates.add(day)
            host.auction_session.frozen_dates.add(day)
            signal_day = host._resolve_signal_date(day)
            host.auction_session.ranks = {
                str(host._instrument_by_stock[code]): rank
                for code, rank in host._prediction_ranks_by_date.get(signal_day, {}).items()
                if code in host._instrument_by_stock}
        return Decision(result.state, result.reason, {**self.evidence, **result.evidence})

    def abort(self, reason='planning_interrupted'):
        self.invalidated = True
        if self.request is not None and not self.request.done():
            self.request.cancel()
        if self.delegate is not None:
            self.delegate.abort(reason)
