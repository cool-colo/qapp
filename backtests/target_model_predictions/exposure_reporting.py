"""Read-only phase snapshots and fill attribution for tick replay."""
from __future__ import annotations

from decimal import Decimal
import pandas as pd

from nautilus_trader.common.actor import Actor
from strategies.cancellation import parse_cancel_windows


class ExposureReplayRecorder(Actor):
    def __init__(self, strategy, window):
        super().__init__()
        self.strategy, self.window = strategy, window
        self.rows = []

    def on_start(self):
        correction = parse_cancel_windows(self.strategy.orchestration.config.exposure.windows)
        normal = parse_cancel_windows(self.strategy.config.execution_windows)
        for lower, upper in self.window.sessions:
            day = lower.date()
            if day not in self.strategy._trading_dates:
                continue
            points = [('session_end', upper.value - 1)]
            for index, (start, end) in enumerate(correction):
                for suffix, clock in (('before', start), ('after', end)):
                    due = pd.Timestamp(f'{day} {clock}').tz_localize(lower.tz).value - 1
                    points.append((f'correction_{index}_{suffix}', due))
            if normal:
                due = pd.Timestamp(f'{day} {normal[0][0]}').tz_localize(lower.tz).value - 1
                points.append(('normal_before', due))
            for stage, due in points:
                if lower.value <= due < upper.value:
                    self.clock.set_time_alert(
                        name=f'exposure-report-{day}-{stage}',
                        alert_time=pd.Timestamp(due, unit='ns', tz='UTC'),
                        callback=lambda _, stage=stage: self.capture(stage),
                    )

    def capture(self, stage):
        context = self.strategy.orchestration.snapshot_provider.capture_positions()
        assessment = self.strategy.orchestration.snapshot_provider.assessor.assess(context)
        target = context.target
        value = sum((p.quantity * p.price for p in context.positions.values()), Decimal(0))
        valid = not context.quality_error and context.total_assets > 0
        target_current = target is not None and target.trading_date == context.trading_date
        self.rows.append(dict(
            trading_date=context.trading_date, stage=stage, time_ns=context.now_ns,
            quality_error=context.quality_error, assessment_status=assessment.status,
            assessment_reason=assessment.reason,
            total_assets=context.total_assets if valid else None,
            market_value=value if valid else None,
            gross_actual_ratio=value / context.total_assets if valid else None,
            target_version=target.version if target_current else None,
            fixed_cash_buffer=target.buffer_amount if target_current else None,
            target_ratio=assessment.target_ratio, actual_ratio=assessment.actual_ratio,
            deviation=assessment.deviation,
            gross_target_ratio=(target.ratio * (context.total_assets - target.buffer_amount)
                                / context.total_assets if valid and target_current else None),
            pending_orders=context.pending_orders,
            positions={iid: {'quantity': str(p.quantity), 'price': str(p.price)}
                       for iid, p in context.positions.items()},
        ))


def exposure_replay_reports(strategy, snapshots, fills):
    """Attribute executions by originating order, including late corrective fills."""
    orders = {event.order_id: event for event in strategy.order_events
              if event.status == 'submitted' and event.order_id}
    attributed = []
    windows = parse_cancel_windows(strategy.orchestration.config.exposure.windows)
    for fill in fills.reset_index().to_dict('records'):
        order_id = str(fill['client_order_id'])
        order = orders.get(order_id)
        if order is None:
            raise ValueError(f'Fill has no strategy submission: {order_id}')
        version = order.extra['target_version']
        phase = ('exposure_correction' if version.startswith('exposure:') else
                 'opening_auction' if order.extra['intent_context']['execution_phase'] == 'opening_auction'
                 else 'normal_execution')
        stamp = pd.Timestamp(fill['ts_event']).tz_convert(strategy.config.timezone_name)
        in_window = any(start <= stamp.time() < end for start, end in windows)
        attributed.append(dict(
            trading_date=stamp.date().isoformat(), time_ns=stamp.value, order_id=order_id,
            instrument_id=str(fill['instrument_id']), phase=phase,
            side=str(fill['order_side']).lower(), quantity=Decimal(str(fill['last_qty'])),
            price=Decimal(str(fill['last_px'])),
            amount=Decimal(str(fill['last_qty'])) * Decimal(str(fill['last_px'])),
            within_correction_window=in_window,
        ))
    summary = []
    for day in sorted({row['trading_date'] for row in snapshots}):
        points = {row['stage']: row for row in snapshots if row['trading_date'] == day}
        before, after = points.get('correction_0_before'), points.get('correction_0_after')
        normal = points.get('normal_before')
        daily = [row for row in attributed if row['trading_date'] == day and row['phase'] == 'exposure_correction']
        records = [r for r in strategy.orchestration.engine.records
                   if r.task_id == 'exposure_correction'
                   and pd.Timestamp(r.now_ns, unit='ns', tz='UTC').tz_convert(strategy.config.timezone_name).date().isoformat() == day]
        row = dict(trading_date=day,
                   correction_buy_amount=sum((r['amount'] for r in daily if r['side'] == 'buy'), Decimal(0)),
                   correction_sell_amount=sum((r['amount'] for r in daily if r['side'] == 'sell'), Decimal(0)),
                   late_correction_fill_amount=sum((r['amount'] for r in daily if not r['within_correction_window']), Decimal(0)),
                   correction_runs=len({r.run_id for r in records if r.action == 'ADMITTED'}),
                   correction_reasons=sorted({r.reason for r in records}),
                   final_task_action=records[-1].action if records else None,
                   final_task_reason=records[-1].reason if records else None)
        for label, point in (('before', before), ('after', after), ('before_normal', normal)):
            for key in ('gross_target_ratio', 'gross_actual_ratio', 'target_ratio', 'actual_ratio',
                        'deviation', 'pending_orders', 'quality_error', 'target_version', 'assessment_status'):
                row[f'{label}_{key}'] = None if point is None else point[key]
        summary.append(row)
    return {'exposure_snapshots': pd.DataFrame(snapshots),
            'execution_fill_attribution': pd.DataFrame(attributed),
            'daily_exposure_correction': pd.DataFrame(summary)}
