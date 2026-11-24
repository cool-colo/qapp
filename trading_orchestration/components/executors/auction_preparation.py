from task_orchestration.models import Decision, RunState


class AuctionExecutionPreparation:
    """Prepare auction state; each order validates its own cached reference."""

    def __init__(self, bridge):
        self.bridge = bridge
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
        host._roll_trading_day(day)
        signal_day = host._resolve_signal_date(day)
        for code, rank in host._prediction_ranks_by_date.get(signal_day, {}).items():
            if code in host._instrument_by_stock:
                host.auction_session.ranks.setdefault(str(host._instrument_by_stock[code]), rank)
        host.auction_session.frozen_dates.add(day)
        host.auction_session.execution_ready_dates.add(day)
        host._processed_dates.add(day)
        self.ready = True
        self.evidence = {'prepared_at_ns': context.now_ns}
        return Decision(RunState.SUCCEEDED, 'auction_execution_ready', self.evidence)

    def abort(self):
        """No asynchronous acquisition is owned by execution preparation."""
