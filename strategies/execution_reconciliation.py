from __future__ import annotations

import asyncio
import inspect
import time
from concurrent.futures import Future as ConcurrentFuture
from decimal import Decimal
from typing import Any
from typing import Callable

import pandas as pd

from nautilus_trader.common.enums import LogColor

from strategies.async_scheduling import AsyncAwaitableScheduler


class ExecutionStateReconciler:
    """Runs and schedules the strategy's execution-state reconciliation."""

    PRE_OPEN_ALERT = "TARGET-WEIGHT-PRE-OPEN-RECONCILE"

    def __init__(
        self,
        timezone_name: str,
        async_scheduler: AsyncAwaitableScheduler,
    ) -> None:
        self._clock: Any | None = None
        self._log: Any | None = None
        self._timezone_name = timezone_name
        self._async_scheduler = async_scheduler
        self._reconcile: Callable[..., Any] | None = None
        self._reconcile_time: tuple[int, int] | None = None
        self._timeout_secs = 30.0
        self._task: asyncio.Future[Any] | ConcurrentFuture[Any] | None = None
        self._venue_sellable: dict[str, Decimal] = {}
        self._event_reporter: Callable[[str, str, dict[str, Any]], None] | None = None

    def bind_runtime(self, clock: Any, log: Any) -> None:
        """Bind dependencies installed by Nautilus when the strategy is registered."""
        self._clock = clock
        self._log = log

    def configure(
        self,
        reconcile: Callable[..., Any],
        reconcile_time: str,
        timeout_secs: float,
        event_reporter: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        parsed_time = self._parse_hh_mm(reconcile_time)
        if parsed_time is None:
            raise ValueError("pre-open reconcile time is required")
        self._reconcile = reconcile
        self._reconcile_time = parsed_time
        self._timeout_secs = float(timeout_secs)
        self._event_reporter = event_reporter

    def schedule_daily(self) -> None:
        clock = self._runtime_clock()
        log = self._runtime_log()
        alert_time = self._next_daily_time()
        clock.set_time_alert(
            name=self.PRE_OPEN_ALERT,
            alert_time=alert_time,
            callback=self._on_timer,
            override=True,
        )
        log.info(
            f"Next pre-open execution-state reconciliation scheduled for {alert_time.isoformat()} "
            f"({self._timezone_name})",
            color=LogColor.BLUE,
        )

    def run(self, report_scheduled_event: bool = False) -> None:
        started = time.monotonic()
        log = self._runtime_log()
        if self._task is not None and not self._task.done():
            log.warning(
                "Previous pre-open execution-state reconciliation is still running; skipping",
            )
            self._report_event(
                report_scheduled_event,
                "skipped",
                started,
                {"reason": "previous reconciliation is still running"},
            )
            return
        if self._reconcile is None:
            self._report_event(
                report_scheduled_event,
                "failed",
                started,
                {"error": "execution-state reconciliation is not configured"},
            )
            raise RuntimeError("execution-state reconciliation is not configured")
        try:
            result = self._reconcile(timeout_secs=self._timeout_secs)
        except Exception as exc:
            log.warning(
                f"Pre-open execution-state reconciliation failed to start: {exc}",
            )
            self._report_event(
                report_scheduled_event,
                "failed",
                started,
                {"error": str(exc)},
            )
            return
        if inspect.isawaitable(result):
            self._task = self._async_scheduler.schedule(result)
            self._task.add_done_callback(
                lambda task: self._on_done(task, report_scheduled_event, started),
            )
            log.info(
                "Started pre-open execution-state reconciliation",
                color=LogColor.BLUE,
            )
            return
        succeeded = bool(result)
        self._log_result(succeeded)
        self._report_event(
            report_scheduled_event,
            "success" if succeeded else "failed",
            started,
            {"reconciled": succeeded},
        )

    def subscribe_mass_status(self, venue: Any, msgbus: Any) -> None:
        """Subscribe to reconciled execution reports for broker sellable quantities."""
        log = self._runtime_log()
        try:
            msgbus.subscribe(
                topic=f"reports.execution.{venue}",
                handler=self.update_from_mass_status,
            )
        except Exception as exc:  # pragma: no cover - defensive for backtests
            log.warning(f"Could not subscribe to execution mass status: {exc}")

    def update_from_mass_status(self, mass_status: Any) -> None:
        """Replace broker sellable quantities from one execution mass-status report."""
        position_reports = getattr(mass_status, "position_reports", None)
        if not position_reports:
            return
        sellable: dict[str, Decimal] = {}
        for reports in position_reports.values():
            report_list = reports if isinstance(reports, (list, tuple)) else [reports]
            for report in report_list:
                can_use = getattr(report, "can_use_volume", None)
                instrument_id = getattr(report, "instrument_id", None)
                if can_use is None or instrument_id is None:
                    continue
                try:
                    sellable[str(instrument_id)] = Decimal(str(can_use))
                except Exception:
                    continue
        self._venue_sellable = sellable
        self._runtime_log().info(
            f"Updated broker sellable map from mass status: instruments={len(sellable)}",
            color=LogColor.BLUE,
        )

    def venue_sellable_quantity(self, instrument_id: str) -> Decimal | None:
        return self._venue_sellable.get(instrument_id)

    def _on_timer(self, _event: Any) -> None:
        self.schedule_daily()
        self.run(report_scheduled_event=True)

    def _on_done(
        self,
        task: asyncio.Future[Any] | ConcurrentFuture[Any],
        report_scheduled_event: bool = False,
        started: float | None = None,
    ) -> None:
        self._task = None
        try:
            result = task.result()
        except Exception as exc:
            self._runtime_log().warning(
                f"Pre-open execution-state reconciliation failed: {exc}",
            )
            self._report_event(
                report_scheduled_event,
                "failed",
                started,
                {"error": str(exc)},
            )
            return
        succeeded = bool(result)
        self._log_result(succeeded)
        self._report_event(
            report_scheduled_event,
            "success" if succeeded else "failed",
            started,
            {"reconciled": succeeded},
        )

    def _report_event(
        self,
        enabled: bool,
        status: str,
        started: float | None,
        details: dict[str, Any],
    ) -> None:
        if not enabled or self._event_reporter is None:
            return
        summary = dict(details)
        if self._reconcile_time is not None:
            summary["configured_time"] = (
                f"{self._reconcile_time[0]:02d}:{self._reconcile_time[1]:02d}"
            )
        if started is not None:
            summary["duration_ms"] = (time.monotonic() - started) * 1000.0
        try:
            self._event_reporter(self.PRE_OPEN_ALERT, status, summary)
        except Exception as exc:
            self._runtime_log().warning(
                f"Could not queue DingTalk event {self.PRE_OPEN_ALERT}: {exc}",
            )

    def _log_result(self, succeeded: bool) -> None:
        log = self._runtime_log()
        if succeeded:
            log.info(
                "Pre-open execution-state reconciliation succeeded",
                color=LogColor.GREEN,
            )
        else:
            log.warning(
                "Pre-open execution-state reconciliation did not complete successfully",
            )

    def _next_daily_time(self) -> pd.Timestamp:
        if self._reconcile_time is None:
            raise RuntimeError("execution-state reconciliation is not configured")
        now = pd.Timestamp(self._runtime_clock().utc_now()).tz_convert(
            self._timezone_name,
        )
        hh, mm = self._reconcile_time
        target = now.normalize() + pd.Timedelta(hours=hh, minutes=mm)
        if target <= now:
            target = target + pd.Timedelta(days=1)
        return target

    def _runtime_clock(self) -> Any:
        if self._clock is None:
            raise RuntimeError("execution-state reconciler runtime clock is not bound")
        return self._clock

    def _runtime_log(self) -> Any:
        if self._log is None:
            raise RuntimeError("execution-state reconciler runtime log is not bound")
        return self._log

    @staticmethod
    def _parse_hh_mm(value: str | None) -> tuple[int, int] | None:
        if not value or not str(value).strip():
            return None
        hh, mm = str(value).strip().split(":")
        return int(hh), int(mm)
