from task_orchestration.models import Decision, RunState


class AuctionExecutionPreparation:
    def __init__(self, bridge):
        self.bridge = bridge
        self.request = None
        self.ready = False
        self.evidence = {}

    def prepare(self, context):
        host = self.bridge.host
        day = host._clock_date()
        if (day not in host._trading_dates or not host.config.auction_enabled
                or host._target_date != day or not host._target_version):
            return Decision(RunState.CANCELLED, 'auction_target_unavailable')
        if day in host.auction_session.failed_dates:
            return Decision(RunState.FAILED, host.auction_session.failed_dates[day])
        if host._trading_paused or host._target_updates_paused:
            return Decision(RunState.WAITING, 'user_paused')
        if host._awaiting_pre_open_reconcile():
            return Decision(RunState.WAITING, 'awaiting_pre_open_reconciliation')
        if self.ready:
            return Decision(RunState.SUCCEEDED, 'auction_execution_ready', self.evidence)
        if self.request is None:
            host._roll_trading_day(day)
            self.request = host.market_snapshots.request_fresh('auction_execute')
            self.request.add_done_callback(lambda _: self.bridge.wake_event())
        if not self.request.done():
            return Decision(RunState.WAITING, 'auction_execution_market_pending')
        try:
            batch = self.request.result()
        except Exception as exc:
            reason = f'auction_execution_market_failed: {exc}'
            host.auction_session.fail(day, reason)
            return Decision(RunState.FAILED, reason)
        if self.bridge.date_at(batch.requested_at_ns) != day:
            return Decision(RunState.EXPIRED, 'auction_execution_market_wrong_date')
        host.apply_full_tick_snapshot(batch.fields(), 'auction_execute')
        required = host._held_instrument_ids() | set(host._target_quantities)
        suspended = host._suspended_by_date.get(day, set())
        missing = [iid for iid in sorted(required)
                   if host._stock_code_for_instrument(iid) not in suspended
                   and not host._is_suspended_status(iid)
                   and host._select_opening_auction_reference(iid)[0] is None]
        if missing:
            host.auction_session.fail(day, 'invalid_auction_execution_snapshot')
            return Decision(RunState.FAILED, 'invalid_auction_execution_snapshot',
                            {'missing_instruments': missing})
        signal_day = host._resolve_signal_date(day)
        for code, rank in host._prediction_ranks_by_date.get(signal_day, {}).items():
            if code in host._instrument_by_stock:
                host.auction_session.ranks.setdefault(str(host._instrument_by_stock[code]), rank)
        host.auction_session.frozen_dates.add(day)
        host.auction_session.execution_ready_dates.add(day)
        host._processed_dates.add(day)
        self.ready = True
        self.evidence = {'market_request_id': batch.request_id,
                         'market_requested_at_ns': batch.requested_at_ns,
                         'market_ready_at_ns': batch.completed_at_ns}
        return Decision(RunState.SUCCEEDED, 'auction_execution_ready', self.evidence)

    def abort(self):
        if self.request is not None and not self.request.done():
            self.request.cancel()
