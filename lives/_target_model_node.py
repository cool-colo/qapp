#!/usr/bin/env python3
"""
Shared, venue-agnostic node builder for the target-model-predictions live strategy.

Both the QMT (``live_qmt_target_model_predictions``) and Big QMT
(``live_bigqmt_target_model_predictions``) entrypoints construct the same strategy,
snapshot recorder, reconciliation wiring, Prometheus exporter and status server.
The only difference is the venue's data/exec client configs + factories, passed in
via :class:`VenueClients`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pandas as pd

from lives import live_common as legacy
from lives.live_reconciliation import LiveExecutionReconciliation
from lives.monitoring import PrometheusExporter
from lives.monitoring import PrometheusExporterConfig
from lives.control_server import ControlServerConfig
from lives.control_server import LiveControlServer
from lives.status_server import LiveStatusServer
from lives.status_server import StatusServerConfig
from monitoring.dingtalk_alert import DingTalkAlerter
from monitoring.dingtalk_alert import FixedTimeEventReporter
from strategies.model_prediction_targets import TargetModelPredictionsStrategy
from strategies.model_prediction_targets import TargetModelPredictionsStrategyConfig
from strategies.strategy_params import StrategyParams
from nautilus_trader.common.enums import LogColor


_PRE_OPEN_RECONCILE_TIME_SHANGHAI = "09:15"


@dataclass
class VenueClients:
    """Venue-specific client wiring for the trading node."""

    client_id: str
    data_client_config: Any
    exec_client_config: Any
    data_factory: type
    exec_factory: type
    reconciliation_instrument_ids: Any = None
    timeout_connection_secs: float = 90.0


class LiveTargetModelPredictionsStrategy(TargetModelPredictionsStrategy):
    _REFRESH_ALERT = "TARGET-MODEL-DATA-REFRESH"

    def __init__(
        self,
        config: TargetModelPredictionsStrategyConfig,
        refresh_context: Any,
        refresh_interval_secs: float = 0.0,
        refresh_time: str | None = "09:10",
        event_reporter: FixedTimeEventReporter | None = None,
    ) -> None:
        super().__init__(config)
        self._refresh_context = refresh_context
        self._refresh_interval_secs = float(refresh_interval_secs)
        self._refresh_time = self._parse_hh_mm(refresh_time)
        self._event_reporter = event_reporter
        self.configure_target_alert_reporter(
            event_reporter.report if event_reporter is not None else None,
        )
        self._execution_reconciliation = LiveExecutionReconciliation(
            request_reconcile=self.request_execution_reconcile,
            warn=lambda message: self.log.warning(message),
        )

    @staticmethod
    def _parse_hh_mm(value: str | None) -> tuple[int, int] | None:
        if not value or not str(value).strip():
            return None
        hh, mm = str(value).strip().split(":")
        return int(hh), int(mm)

    def _next_refresh_time(self) -> pd.Timestamp:
        tz = self.config.timezone_name
        now = pd.Timestamp(self.clock.utc_now()).tz_convert(tz)
        hh, mm = self._refresh_time
        target = now.normalize() + pd.Timedelta(hours=hh, minutes=mm)
        if target <= now:
            target = target + pd.Timedelta(days=1)
        return target

    def on_start(self) -> None:
        super().on_start()
        self._execution_reconciliation.request_after_strategy_start()
        if self._refresh_time is not None:
            self._schedule_daily_refresh()
        elif self._refresh_interval_secs > 0:
            self.clock.set_timer(
                name=self._REFRESH_ALERT,
                interval=timedelta(seconds=self._refresh_interval_secs),
                callback=self._on_refresh_timer,
                fire_immediately=False,
            )

    def _schedule_daily_refresh(self) -> None:
        alert_time = self._next_refresh_time()
        self.clock.set_time_alert(
            name=self._REFRESH_ALERT,
            alert_time=alert_time,
            callback=self._on_refresh_timer,
            override=True,
        )
        self.log.info(
            f"Next target-model refresh scheduled for {alert_time.isoformat()} "
            f"({self.config.timezone_name})",
            color=LogColor.BLUE,
        )

    def _on_refresh_timer(self, _event: Any) -> None:
        started = time.monotonic()
        fixed_time_event = self._refresh_time is not None
        if self._refresh_time is not None:
            self._schedule_daily_refresh()
        status = "success"
        details: dict[str, Any] = {}
        try:
            context = self._refresh_context(self._active_stock_codes())
            self.refresh_reference_data(
                instrument_ids=context.instrument_ids,
                bar_types=context.bar_types,
                instrument_stock_codes=context.instrument_stock_codes,
                signals_by_date=context.signals_by_date,
                prediction_ranks_by_date={
                    key.isoformat(): values
                    for key, values in context.bundle.prediction_ranks_by_date.items()
                },
                trading_dates=[value.isoformat() for value in context.bundle.trading_dates],
                listed_dates={key: value.isoformat() for key, value in context.bundle.listed_dates.items()},
                st_by_date={key.isoformat(): sorted(values) for key, values in context.bundle.st_by_date.items()},
                suspended_by_date={
                    key.isoformat(): sorted(values)
                    for key, values in context.bundle.suspended_by_date.items()
                },
                daily_stock_data=context.daily_stock_data,
                last_closes=context.last_closes,
                subscribe_new_bars=True,
                unsubscribe_removed_bars=True,
            )
            self.log.info(
                f"Refreshed target-model data: instruments={len(context.instrument_ids)} "
                f"signals={context.bundle.selected_rows}",
            )
            details.update(
                instruments=len(context.instrument_ids),
                signals=context.bundle.selected_rows,
            )
        except Exception as exc:
            status = "failed"
            details["error"] = str(exc)
            self.log.warning(f"Target-model data refresh failed, keeping previous data: {exc}")
        self._execution_reconciliation.request_after_target_refresh()
        if fixed_time_event:
            try:
                details["trading_date"] = self._clock_date()
                details["configured_time"] = (
                    f"{self._refresh_time[0]:02d}:{self._refresh_time[1]:02d}"
                )
                details["duration_ms"] = (time.monotonic() - started) * 1000.0
                self._report_event(self._REFRESH_ALERT, status, details)
            except Exception as exc:
                self.log.warning(f"Could not summarize DingTalk event {self._REFRESH_ALERT}: {exc}")

    def _on_process_targets_timer(self, _event: Any) -> None:
        if not self._within_trading_window():
            return
        trading_date = self._clock_date()
        started = time.monotonic()
        before = (len(self.signal_events), len(self.target_events), len(self.order_events))
        try:
            processed = self._process_trading_day_once(trading_date, "timer")
        except Exception as exc:
            self._report_event(
                self._PROCESS_TARGETS_TIMER,
                "failed",
                {
                    "trading_date": trading_date,
                    "duration_ms": (time.monotonic() - started) * 1000.0,
                    "error": str(exc),
                },
            )
            raise
        if not processed:
            return
        after = (len(self.signal_events), len(self.target_events), len(self.order_events))
        self._report_event(
            self._PROCESS_TARGETS_TIMER,
            "success",
            {
                "trading_date": trading_date,
                "duration_ms": (time.monotonic() - started) * 1000.0,
                "new_signal_events": after[0] - before[0],
                "new_target_events": after[1] - before[1],
                "new_order_events": after[2] - before[2],
            },
        )

    def _on_full_tick_prefetch_timer(self, _event: Any) -> None:
        started = time.monotonic()
        if self._full_tick_prefetch_time is not None:
            self._schedule_full_tick_prefetch()

        def _completed(status: str, details: dict[str, Any]) -> None:
            summary = dict(details)
            summary["duration_ms"] = (time.monotonic() - started) * 1000.0
            summary["trading_date"] = self._clock_date()
            if self._full_tick_prefetch_time is not None:
                summary["configured_time"] = (
                    f"{self._full_tick_prefetch_time[0]:02d}:{self._full_tick_prefetch_time[1]:02d}"
                )
            self._report_event(self._FULL_TICK_PREFETCH_ALERT, status, summary)

        self._run_full_tick_fetch(trigger="prefetch", on_complete=_completed)

    def _report_event(self, event_name: str, status: str, details: dict[str, Any]) -> None:
        if self._event_reporter is None:
            return
        try:
            self._event_reporter.report(event_name, status, details)
        except Exception as exc:
            self.log.warning(f"Could not queue DingTalk event {event_name}: {exc}")

    def _active_stock_codes(self) -> set[str]:
        stock_codes = set()
        for instrument_id in self._active_positions:
            stock_code = self._stock_by_instrument.get(instrument_id)
            if stock_code:
                stock_codes.add(stock_code)
        try:
            open_positions = self.cache.positions_open()
        except Exception:
            open_positions = []
        for position in open_positions:
            try:
                if not position.is_long:
                    continue
                instrument_id = str(position.instrument_id)
            except Exception:
                continue
            stock_code = self._stock_by_instrument.get(instrument_id) or legacy.stock_code_from_instrument_id(instrument_id)
            if stock_code:
                stock_codes.add(stock_code)
        return stock_codes


def _held_stock_codes_for_seeding(args: Any) -> set[str]:
    """
    Read currently-held stock codes from the ``live_position_snapshot`` MySQL table
    so they can be folded into the initial universe.

    Prefers today's ``before_trading`` snapshot; falls back to the most recent prior
    trading date's ``after_trading`` snapshot (see
    ``LiveSnapshotWriter.load_held_stock_codes_for_seeding``). Failures are non-fatal:
    seeding is best-effort, so any error is logged and an empty set returned.
    """
    from backtests.result_writers.live_writer import LiveSnapshotWriter

    trade_date = pd.Timestamp.now(tz=args.exchange_timezone).date()
    writer: Any = None
    try:
        writer = LiveSnapshotWriter.from_pymysql_kwargs(
            logger=None,
            create_tables=False,
            host=args.mysql_host,
            port=int(args.mysql_port),
            user=args.mysql_user,
            password=args.mysql_password,
            database=args.mysql_database,
            charset="utf8mb4",
            autocommit=False,
        )
        codes = writer.load_held_stock_codes_for_seeding(
            str(args.account_id),
            str(args.trader_id),
            trade_date,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort seeding, never fatal
        print(
            f"[build_node] held-position seeding skipped (live_position_snapshot read failed): {exc}",
            flush=True,
        )
        codes = []
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
    normalized = legacy.normalized_stock_codes(codes)
    if normalized:
        print(
            f"[build_node] seeding {len(normalized)} held stock code(s) into initial universe "
            f"from live_position_snapshot (trade_date={trade_date})",
            flush=True,
        )
    return normalized


def build_target_model_node(
    args: Any,
    loader: legacy.LivePredictionDataLoader,
    make_venue_clients: Any,
    add_snapshot_recorder: Any,
):
    """
    Build the trading node, strategy, recorder, exporter, status + control servers.

    ``make_venue_clients(args, context) -> VenueClients`` builds the venue-specific
    client configs + factories from the loaded prediction context (the instrument
    provider config depends on ``context.instrument_ids``).
    ``add_snapshot_recorder`` is the entrypoint's ``_add_snapshot_recorder``; it is
    called as ``add_snapshot_recorder(args, node, strategy, ...)``.
    """
    from nautilus_trader.config import LiveExecEngineConfig
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.config import TradingNodeConfig
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.identifiers import TraderId

    params = StrategyParams.from_yaml(args.config)
    extra_stock_codes = legacy.normalized_stock_codes(legacy.env_list_from_value(args.extra_stock_codes))
    # Benchmark index codes recorded in the after-trading live_stock_tick_snapshot.
    # They are reference-only (never traded, never in the universe), so keep them out
    # of extra_stock_codes/loader universe and add them only to the full-tick fetch.
    index_tick_codes = legacy.normalized_stock_codes(
        legacy.env_list_from_value(args.index_tick_codes),
    )
    # Fold currently-held stock codes into the initial universe so their last_close
    # gets seeded from ClickHouse even when they dropped out of today's prediction
    # universe. Without this a held-but-unpredicted name has no price source and gets
    # dropped from the risk-manager holdings / cannot be priced for a forced exit.
    held_stock_codes = _held_stock_codes_for_seeding(args)
    if held_stock_codes:
        extra_stock_codes = extra_stock_codes.union(held_stock_codes)
    history_days = max(int(params.consecutive_up_limit_days) - 1, 0)
    context = loader.load(
        extra_stock_codes=extra_stock_codes,
        trading_history_days=history_days,
    )
    venue_clients: VenueClients = make_venue_clients(args, context)
    print(
        "[build_node] loaded target context: "
        f"stock_codes={len(context.stock_codes)} "
        f"instrument_ids={len(context.instrument_ids)} "
        f"bar_types={len(context.bar_types)} "
        f"signal_dates={len(context.signals_by_date)} "
        f"signals_total={sum(len(v) for v in context.signals_by_date.values())} "
        f"last_closes={len(context.last_closes)} "
        f"daily_stock_data={len(context.daily_stock_data)} "
        f"trading_dates={len(context.bundle.trading_dates)} "
        f"selected_rows={context.bundle.selected_rows} "
        f"universe={len(context.bundle.universe)}",
        flush=True,
    )

    config_node = TradingNodeConfig(
        trader_id=TraderId(args.trader_id),
        cache=legacy.build_cache_config(args),
        logging=LoggingConfig(
            log_level=args.log_level,
            log_level_file=args.log_level,
            log_directory=args.log_directory,
            log_file_name=args.log_file_name,
        ),
        exec_engine=LiveExecEngineConfig(
            load_cache=args.load_cache_on_start,
            reconciliation=True,
            reconciliation_lookback_mins=1440,
            reconciliation_instrument_ids=venue_clients.reconciliation_instrument_ids,
            filter_unclaimed_external_orders=not args.load_all_instruments,
            # QMT 的下单确认链路较慢（Submit→Accepted 观测到 ~6s）。默认的
            # inflight_check_threshold_ms=5000 会在订单正常确认途中就把它判为延迟
            # 单，主动向 venue 发 QueryOrder 核对状态；此时 broker 那边可能已全量成交，
            # 查询返回的 filled_qty 会被合成为一笔 inferred fill，随后 poll 循环的真实
            # fill 又到达，同一笔成交被计两次 → overfill 拒绝。调大阈值到 20s，覆盖慢确认。
            inflight_check_threshold_ms=20_000,
        ),
        data_clients={venue_clients.client_id: venue_clients.data_client_config},
        exec_clients={venue_clients.client_id: venue_clients.exec_client_config},
        timeout_connection=venue_clients.timeout_connection_secs,
        timeout_reconciliation=30.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=5.0,
    )
    node = TradingNode(config=config_node)
    params.log(node.get_logger(), source=args.config)
    event_reporter = FixedTimeEventReporter(
        DingTalkAlerter.from_env(timeout=args.dingtalk_timeout_secs),
        environment=args.environment,
        account_id=str(args.account_id),
        trader_id=str(args.trader_id),
        strategy_name=str(args.strategy_name),
    )
    strategy = LiveTargetModelPredictionsStrategy(
        config=TargetModelPredictionsStrategyConfig(
            instrument_ids=context.instrument_ids,
            external_order_claims=context.instrument_ids,
            bar_types=context.bar_types,
            instrument_stock_codes=context.instrument_stock_codes,
            signals_by_date=context.signals_by_date,
            prediction_ranks_by_date={
                key.isoformat(): values
                for key, values in context.bundle.prediction_ranks_by_date.items()
            },
            trading_dates=[value.isoformat() for value in context.bundle.trading_dates],
            listed_dates={key: value.isoformat() for key, value in context.bundle.listed_dates.items()},
            st_by_date={key.isoformat(): sorted(values) for key, values in context.bundle.st_by_date.items()},
            suspended_by_date={
                key.isoformat(): sorted(values)
                for key, values in context.bundle.suspended_by_date.items()
            },
            daily_stock_data=context.daily_stock_data,
            consecutive_up_limit_days=params.consecutive_up_limit_days,
            max_open_gap_up=params.max_open_gap_up,
            max_positions=args.max_positions,
            max_position_percent=params.max_position_percent,
            stop_loss=params.stop_loss,
            trailing_take_profit=params.trailing_take_profit,
            trailing_take_profit_start=params.trailing_take_profit_start,
            min_listed_days=params.min_listed_days,
            initial_cash=params.initial_cash,
            timezone_name=args.exchange_timezone,
            initial_last_closes=context.last_closes,
            excluded_name_prefixes=params.excluded_name_prefixes,
            target_weight_planner=params.target_weight_planner,
            target_weight_planner_error_policy=params.target_weight_planner_error_policy,
            local_exit_authoritative=params.local_exit_authoritative,
            risk_manager_base_url=params.risk_manager_base_url,
            risk_manager_risk_model_id=params.risk_manager_risk_model_id,
            alpha_model_id=params.alpha_model_id,
            risk_manager_mode=params.risk_manager_mode,
            risk_manager_timeout_secs=params.risk_manager_timeout_secs,
            unfilled_timeout_secs=params.unfilled_timeout_secs,
            resubmit_check_interval_secs=params.resubmit_interval_secs,
            cash_buffer_percent=params.cash_buffer_percent,
            target_cash_buffer_percent=params.target_cash_buffer_percent,
            stop_time=params.stop_time,
            limit_stop_mode=params.limit_stop_mode,
            exit_non_targets=params.exit_non_targets,
            order_slice_notional=params.order_slice_notional,
            trade_tick_log_sample_rate=params.trade_tick_log_sample_rate,
            order_book_depth_log_sample_rate=params.order_book_depth_log_sample_rate,
            trading_windows=params.trading_windows,
            exchange_trading_windows=params.exchange_trading_windows,
            order_id_tag=params.order_id_tag,
            subscribe_bars=False,
            subscribe_quote_ticks=False,
            subscribe_trade_ticks=False,
            quote_tick_window_probe_instrument_ids=tuple(context.instrument_ids[:2]),
            subscribe_order_book_depth=True,
            full_tick_refresh_secs=params.full_tick_refresh_secs,
            full_tick_prefetch_time=params.full_tick_prefetch_time,
            process_targets_on_timer=True,
            process_targets_interval_secs=params.resubmit_interval_secs,
        ),
        refresh_context=lambda active_stock_codes: loader.load(
            extra_stock_codes=extra_stock_codes.union(active_stock_codes),
            trading_history_days=history_days,
        ),
        refresh_interval_secs=args.refresh_interval_secs,
        refresh_time=args.refresh_time,
        event_reporter=event_reporter,
    )
    # Always wire the reconcile callback: the strategy triggers a reconcile on start
    # and on each refresh to republish the execution mass status — which carries the
    # broker sellable (can_use_volume) map — to the strategy's subscription.
    strategy.configure_pre_open_reconciliation(
        reconcile=node.kernel.exec_engine.reconcile_execution_state,
        reconcile_time=_PRE_OPEN_RECONCILE_TIME_SHANGHAI,
        timeout_secs=config_node.timeout_reconciliation,
        loop=node.kernel.exec_engine._loop,
        event_reporter=event_reporter.report,
    )

    def _fetch_full_tick() -> dict[str, dict[str, float]]:
        # Today's authoritative full-tick snapshot per instrument from the broker.
        # Covers the full configured universe (not just held positions) so new buy
        # targets are priced from the real open, plus active positions that dropped
        # out of the universe.
        stock_codes = set(strategy._stock_by_instrument.values())
        stock_codes.update(strategy._active_stock_codes())
        # Current target instruments too: after a mid-session restart the pre-open
        # prefetch is gone, and a target activated later must still get its open so
        # the pricer can anchor. _target_quantities is keyed by instrument-id text.
        for instrument_id in strategy._target_quantities:
            stock_code = legacy.stock_code_from_instrument_id(instrument_id)
            if stock_code:
                stock_codes.add(stock_code)
        # Benchmark indices (e.g. 399852.SZ 中证1000): reference-only, priced from the
        # same broker full-tick so the after-trading live_stock_tick_snapshot carries an
        # index row. They are never traded; applying their tick to the strategy only
        # seeds inert price state for a non-universe instrument id.
        stock_codes.update(index_tick_codes)
        if not stock_codes:
            return {}
        return loader.full_tick_snapshot(sorted(stock_codes))

    def _fetch_positions() -> dict[str, dict[str, Any]]:
        return loader.broker_position_snapshot()

    def _fetch_orders() -> list[dict[str, Any]]:
        return loader.broker_order_snapshot()

    def _fetch_trades() -> list[dict[str, Any]]:
        return loader.broker_trade_snapshot()

    def _fetch_open_prices(stock_codes: list[str]) -> dict[str, dict[str, float]]:
        codes = [code for code in dict.fromkeys(stock_codes) if code]
        if not codes:
            return {}
        return loader.full_tick_snapshot(codes)

    strategy.configure_full_tick_source(fetch_full_tick=_fetch_full_tick)

    _divid_events_cache: dict[tuple[str, str], list[tuple[Any, float]]] = {}

    def _load_divid_events(
        stock_codes: list[str],
    ) -> dict[str, list[tuple[Any, float]]]:
        # Dividend/split ex-events per held stock, from the broker gateway
        # (Big QMT ``get_divid_factors``; empty on the QMT proxy path). Used by the
        # strategy to restate a holding's entry cost onto today's post-ex basis for
        # the stop-loss check. Cached per (trading-date, stock) so repeated stop-loss
        # evaluations within a day do not re-RPC; the daily cadence matches the
        # full-tick refresh. end_time bounds the query to today's ex-events.
        end_time = pd.Timestamp.now(tz=args.exchange_timezone).strftime("%Y%m%d")
        wanted = [str(code).strip() for code in stock_codes if str(code or "").strip()]
        missing = [code for code in wanted if (end_time, code) not in _divid_events_cache]
        if missing:
            fetched = loader.divid_events(missing, end_time)
            for code in missing:
                _divid_events_cache[(end_time, code)] = fetched.get(code, [])
        return {code: _divid_events_cache[(end_time, code)] for code in wanted}

    strategy.configure_divid_events_loader(_load_divid_events)
    node.trader.add_strategy(strategy)
    add_snapshot_recorder(
        args,
        node,
        strategy,
        _fetch_full_tick,
        _fetch_positions,
        _fetch_orders,
        _fetch_trades,
        _fetch_open_prices,
        event_reporter,
    )
    if args.metrics_port and int(args.metrics_port) > 0:
        exporter = PrometheusExporter(
            config=PrometheusExporterConfig(
                port=int(args.metrics_port),
                addr=args.metrics_addr,
                scrape_interval_secs=args.metrics_interval_secs,
                account_label=args.metrics_account_label,
            ),
        )
        exporter.strategy_ref = strategy
        node.trader.add_actor(exporter)
    node.add_data_client_factory(venue_clients.client_id, venue_clients.data_factory)
    node.add_exec_client_factory(venue_clients.client_id, venue_clients.exec_factory)
    node.build()

    status_server: LiveStatusServer | None = None
    if args.status_port and int(args.status_port) > 0:
        status_server = LiveStatusServer(
            node=node,
            strategy_ref=strategy,
            config=StatusServerConfig(
                port=int(args.status_port),
                addr=args.status_addr,
            ),
        )
        status_server.start()
        node.get_logger().info(
            f"Live status server on http://{args.status_addr}:{args.status_port}/health",
            color=LogColor.GREEN,
        )

    # Trading-control state + audit-log writer (best-effort: a MySQL outage must not
    # block trading, so we degrade to in-memory paused=False on any failure).
    control_writer: Any = None
    try:
        from backtests.result_writers.live_control_writer import LiveControlWriter

        control_writer = LiveControlWriter.from_pymysql_kwargs(
            logger=None,
            create_tables=True,
            host=args.mysql_host,
            port=int(args.mysql_port),
            user=args.mysql_user,
            password=args.mysql_password,
            database=args.mysql_database,
            charset="utf8mb4",
            autocommit=False,
        )
        paused = control_writer.load_trading_paused(str(args.account_id), str(args.trader_id))
        # Safe to apply directly here: this runs before node.run(), so it is
        # uncontended by the trading thread.
        strategy.trading_controller.set_paused(bool(paused))
        node.get_logger().info(
            f"Loaded trading_paused={paused} from live_control_state",
            color=LogColor.GREEN,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort, never fatal
        node.get_logger().warning(
            f"Trading-control state load skipped (MySQL unavailable): {exc}",
        )
        if control_writer is not None:
            try:
                control_writer.close()
            except Exception:  # noqa: BLE001
                pass
            control_writer = None

    control_server: LiveControlServer | None = None
    if args.control_port and int(args.control_port) > 0:
        control_server = LiveControlServer(
            node=node,
            strategy_ref=strategy,
            control_writer=control_writer,
            account_id=str(args.account_id),
            trader_id=str(args.trader_id),
            config=ControlServerConfig(
                port=int(args.control_port),
                addr=args.control_addr,
                token=args.control_token,
            ),
        )
        # Best-effort: a taken port (e.g. a stale node still listening) must not kill
        # trading. Warn and continue without the control server; stop() closes the
        # writer it owned so it does not leak.
        try:
            control_server.start()
            node.get_logger().info(
                f"Live control server on http://{args.control_addr}:{args.control_port}/realtime/positions",
                color=LogColor.GREEN,
            )
        except OSError as exc:
            node.get_logger().warning(
                f"Live control server not started (port {args.control_port} unavailable): {exc}",
            )
            control_server.stop()  # closes control_writer; safe on a never-started server
            control_server = None
    elif control_writer is not None:
        # Control server disabled but the writer is open — nothing owns it, so close.
        try:
            control_writer.close()
        except Exception:  # noqa: BLE001
            pass

    return node, status_server, control_server
