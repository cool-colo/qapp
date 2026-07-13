#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NAUTILUS_TRADER_PATH = Path(
    os.environ.get("NAUTILUS_TRADER_PATH", "/data/flc/code/quant/nautilus_trader"),
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if NAUTILUS_TRADER_PATH.exists() and str(NAUTILUS_TRADER_PATH) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_TRADER_PATH))

from lives import live_common as legacy
from lives.monitoring import PrometheusExporter
from lives.monitoring import PrometheusExporterConfig
from strategies.model_prediction_targets import TargetModelPredictionsStrategy
from strategies.model_prediction_targets import TargetModelPredictionsStrategyConfig
from nautilus_trader.common.enums import LogColor


QMT_CLIENT = legacy.QMT_CLIENT
_MISSING = object()
_DATE_TOKEN_RE = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{8})")


class LiveTargetModelPredictionsStrategy(TargetModelPredictionsStrategy):
    _REFRESH_ALERT = "TARGET-MODEL-DATA-REFRESH"

    def __init__(
        self,
        config: TargetModelPredictionsStrategyConfig,
        refresh_context: Any,
        refresh_interval_secs: float = 0.0,
        refresh_time: str | None = "09:00",
    ) -> None:
        super().__init__(config)
        self._refresh_context = refresh_context
        self._refresh_interval_secs = float(refresh_interval_secs)
        self._refresh_time = self._parse_hh_mm(refresh_time)

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
        # Startup reconciliation runs before the trader (and thus this on_start) starts, so the
        # mass status it publishes is emitted before our subscription exists and is missed.
        # Trigger a fresh reconcile now that the subscription is active, to republish the mass
        # status and populate the broker sellable (can_use_volume) map before the first sell.
        try:
            self.request_execution_reconcile()
        except Exception as exc:
            self.log.warning(f"Execution reconcile request on start failed: {exc}")
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
        if self._refresh_time is not None:
            self._schedule_daily_refresh()
        try:
            context = self._refresh_context(self._active_stock_codes())
            self.refresh_reference_data(
                instrument_ids=context.instrument_ids,
                bar_types=context.bar_types,
                instrument_stock_codes=context.instrument_stock_codes,
                signals_by_date=context.signals_by_date,
                trading_dates=[value.isoformat() for value in context.bundle.trading_dates],
                listed_dates={key: value.isoformat() for key, value in context.bundle.listed_dates.items()},
                st_by_date={key.isoformat(): sorted(values) for key, values in context.bundle.st_by_date.items()},
                suspended_by_date={
                    key.isoformat(): sorted(values)
                    for key, values in context.bundle.suspended_by_date.items()
                },
                last_closes=context.last_closes,
                subscribe_new_bars=True,
                unsubscribe_removed_bars=True,
            )
            self.log.info(
                f"Refreshed target-model data: instruments={len(context.instrument_ids)} "
                f"signals={context.bundle.selected_rows}",
            )
        except Exception as exc:
            self.log.warning(f"Target-model data refresh failed, keeping previous data: {exc}")
        # Also refresh the broker sellable map (can_use_volume) by requesting an execution
        # reconcile; the resulting mass status repopulates _venue_sellable. Inert if the
        # reconcile callback is not configured.
        try:
            self.request_execution_reconcile()
        except Exception as exc:
            self.log.warning(f"Execution reconcile request on refresh failed: {exc}")

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


def parse_args():
    original_argv = sys.argv[:]
    cleaned_argv, pre_open_reconcile_time = _extract_pre_open_reconcile_time_arg(original_argv)
    try:
        sys.argv = cleaned_argv
        args = legacy.parse_args()
    finally:
        sys.argv = original_argv
    # `--pre-open-reconcile-time` is target-specific and is not defined by the shared
    # `live_common.parse_args()`, so strip it before delegating and then apply the
    # target-model env/default here.
    if pre_open_reconcile_time is _MISSING:
        pre_open_reconcile_time = os.environ.get(
            "MODEL_LIVE_PRE_OPEN_RECONCILE_TIME",
            os.environ.get("MODEL_PRE_OPEN_RECONCILE_TIME", "09:15"),
        )
    args.pre_open_reconcile_time = pre_open_reconcile_time
    args.log_file_name = _resolve_daily_log_file_name(args.log_file_name, args.exchange_timezone)
    try:
        args.refresh_time = normalize_refresh_time(args.refresh_time)
        args.pre_open_reconcile_time = normalize_refresh_time(args.pre_open_reconcile_time)
    except ValueError as exc:
        raise SystemExit(f"invalid configured HH:MM time: {exc}") from exc
    _apply_snapshot_args(args)
    return args


def _apply_snapshot_args(args: Any) -> None:
    """
    Attach daily-snapshot / MySQL settings to args from the environment.

    These are target-model-specific and not defined by the shared
    ``live_common.parse_args()``, so — like ``--pre-open-reconcile-time`` — they are
    resolved here rather than added to the shared parser. Snapshots are opt-in via
    ``MODEL_SNAPSHOTS_ENABLED``; when disabled no MySQL connection is made.
    """
    env = os.environ.get
    args.snapshots_enabled = str(env("MODEL_SNAPSHOTS_ENABLED", "") or "").lower() in {
        "1", "true", "yes", "on",
    }
    args.snapshot_before_time = env("MODEL_SNAPSHOT_BEFORE_TIME", "09:27")
    args.snapshot_after_time = env("MODEL_SNAPSHOT_AFTER_TIME", "15:40")
    args.mysql_host = env("MYSQL_HOST", "127.0.0.1")
    args.mysql_port = int(env("MYSQL_PORT", "3306"))
    args.mysql_user = env("MYSQL_USER", "root")
    args.mysql_password = env("MYSQL_PASSWORD", "")
    args.mysql_database = env("MYSQL_DATABASE", "")


def _resolve_daily_log_file_name(log_file_name: str | None, timezone_name: str) -> str:
    base_name = (log_file_name or "model_preds").strip() or "model_preds"
    if "{date}" in base_name:
        date_text = pd.Timestamp.now(tz=timezone_name).strftime("%Y-%m-%d")
        return base_name.replace("{date}", date_text)
    if _DATE_TOKEN_RE.search(base_name):
        return base_name
    date_text = pd.Timestamp.now(tz=timezone_name).strftime("%Y-%m-%d")
    return f"{base_name}-{date_text}"


def _extract_pre_open_reconcile_time_arg(argv: list[str]) -> tuple[list[str], str | object]:
    cleaned = [argv[0]]
    value: str | object = _MISSING
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--pre-open-reconcile-time":
            if index + 1 >= len(argv):
                raise SystemExit("--pre-open-reconcile-time requires a value")
            value = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--pre-open-reconcile-time="):
            value = arg.split("=", 1)[1]
            index += 1
            continue
        cleaned.append(arg)
        index += 1
    return cleaned, value


def normalize_refresh_time(value: str | None) -> str | None:
    if not value or not str(value).strip():
        return None
    parts = str(value).strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError("expected HH:MM or HH:MM:SS")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise ValueError("hour, minute, and second must be integers") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError("time must be within 00:00:00 and 23:59:59")
    return f"{hour:02d}:{minute:02d}"


def _emit_snapshot_status(
    node: Any,
    message: str,
    *,
    is_warning: bool = False,
    color: LogColor = LogColor.BLUE,
) -> None:
    """Mirror snapshot startup status to stdout and the Nautilus file logger."""
    print(message, flush=True)
    get_logger = getattr(node, "get_logger", None)
    if get_logger is None:
        return
    try:
        logger = get_logger()
    except Exception:
        return
    if logger is None:
        return
    log_method = getattr(logger, "warning" if is_warning else "info", None)
    if log_method is None:
        return
    kwargs = {"color": LogColor.YELLOW if is_warning else color}
    try:
        log_method(message, **kwargs)
    except TypeError:
        log_method(message)


def _maybe_add_snapshot_recorder(
    args: Any,
    node: Any,
    strategy: Any,
    fetch_full_tick: Any,
    fetch_positions: Any | None = None,
) -> None:
    """
    Build the MySQL-backed daily-snapshot recorder actor and add it to the node when
    snapshots are enabled. No-op (and no MySQL connection) when disabled, so the node
    runs without pymysql installed.
    """
    if not getattr(args, "snapshots_enabled", False):
        return
    from backtests.result_writers.live_writer import LiveSnapshotWriter
    from lives.snapshot_recorder import SnapshotRecorder
    from lives.snapshot_recorder import SnapshotRecorderConfig

    try:
        writer = LiveSnapshotWriter.from_pymysql_kwargs(
            host=args.mysql_host,
            port=int(args.mysql_port),
            user=args.mysql_user,
            password=args.mysql_password,
            database=args.mysql_database,
            charset="utf8mb4",
            autocommit=False,
        )
    except Exception as exc:
        _emit_snapshot_status(
            node,
            f"[snapshot] MySQL writer init failed, snapshots disabled: {exc}",
            is_warning=True,
        )
        return
    recorder = SnapshotRecorder(
        config=SnapshotRecorderConfig(
            account_id=str(args.account_id),
            trader_id=str(args.trader_id),
            timezone_name=args.exchange_timezone,
            before_time=args.snapshot_before_time,
            after_time=args.snapshot_after_time,
            trading_windows=args.trading_windows,
        ),
        writer=writer,
        strategy_ref=strategy,
        fetch_full_tick=fetch_full_tick,
        fetch_positions=fetch_positions,
    )
    node.trader.add_actor(recorder)
    _emit_snapshot_status(
        node,
        f"[snapshot] recorder enabled: account={args.account_id} "
        f"before={args.snapshot_before_time} after={args.snapshot_after_time}",
    )


def build_node(args: Any, loader: legacy.LivePredictionDataLoader):
    from nautilus_trader.adapters.qmt import QMTDataClientConfig
    from nautilus_trader.adapters.qmt import QMTExecClientConfig
    from nautilus_trader.adapters.qmt import QMTInstrumentProviderConfig
    from nautilus_trader.adapters.qmt import QMTLiveDataClientFactory
    from nautilus_trader.adapters.qmt import QMTLiveExecClientFactory
    from nautilus_trader.config import LiveExecEngineConfig
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.config import TradingNodeConfig
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.identifiers import TraderId

    extra_stock_codes = legacy.normalized_stock_codes(legacy.env_list_from_value(args.extra_stock_codes))
    context = loader.load(extra_stock_codes=extra_stock_codes)
    print(
        "[build_node] loaded target context: "
        f"stock_codes={len(context.stock_codes)} "
        f"instrument_ids={len(context.instrument_ids)} "
        f"bar_types={len(context.bar_types)} "
        f"signal_dates={len(context.signals_by_date)} "
        f"signals_total={sum(len(v) for v in context.signals_by_date.values())} "
        f"last_closes={len(context.last_closes)} "
        f"trading_dates={len(context.bundle.trading_dates)} "
        f"selected_rows={context.bundle.selected_rows} "
        f"universe={len(context.bundle.universe)}",
        flush=True,
    )
    if args.load_all_instruments:
        instrument_provider = QMTInstrumentProviderConfig(
            load_all=True,
            complete_details=args.complete_instrument_details,
        )
    else:
        instrument_provider = QMTInstrumentProviderConfig(
            load_ids=frozenset(context.instrument_ids),
            complete_details=args.complete_instrument_details,
        )
    if args.restrict_reconciliation and not args.load_all_instruments:
        reconciliation_ids = context.instrument_ids
    else:
        reconciliation_ids = None

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
            reconciliation=not args.no_reconciliation,
            reconciliation_lookback_mins=1440,
            reconciliation_instrument_ids=reconciliation_ids,
            filter_unclaimed_external_orders=not args.load_all_instruments,
        ),
        data_clients={
            QMT_CLIENT: QMTDataClientConfig(
                base_url_http=args.base_url_http,
                base_url_ws=args.base_url_ws,
                api_key=args.api_key,
                instrument_provider=instrument_provider,
                adjust_type=args.adjust_type,
            ),
        },
        exec_clients={
            QMT_CLIENT: QMTExecClientConfig(
                account_id=args.account_id,
                account_type=args.account_type,
                base_url_http=args.base_url_http,
                api_key=args.api_key,
                instrument_provider=instrument_provider,
                poll_interval_secs=args.poll_interval_secs,
                strategy_name=args.strategy_name,
                enforce_sellable_position=not args.no_sellable_check,
            ),
        },
        timeout_connection=90.0,
        timeout_reconciliation=30.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=5.0,
    )
    node = TradingNode(config=config_node)
    strategy = LiveTargetModelPredictionsStrategy(
        config=TargetModelPredictionsStrategyConfig(
            instrument_ids=context.instrument_ids,
            external_order_claims=context.instrument_ids,
            bar_types=context.bar_types,
            instrument_stock_codes=context.instrument_stock_codes,
            signals_by_date=context.signals_by_date,
            trading_dates=[value.isoformat() for value in context.bundle.trading_dates],
            listed_dates={key: value.isoformat() for key, value in context.bundle.listed_dates.items()},
            st_by_date={key.isoformat(): sorted(values) for key, values in context.bundle.st_by_date.items()},
            suspended_by_date={
                key.isoformat(): sorted(values)
                for key, values in context.bundle.suspended_by_date.items()
            },
            max_positions=args.max_positions,
            max_position_percent=args.max_position_percent,
            holding_days=args.holding_days,
            stop_loss=args.stop_loss,
            trailing_take_profit=args.trailing_take_profit,
            trailing_take_profit_start=args.trailing_take_profit_start,
            min_listed_days=args.min_listed_days,
            initial_cash=args.initial_cash,
            timezone_name=args.exchange_timezone,
            initial_last_closes=context.last_closes,
            excluded_name_prefixes=tuple(legacy.env_list_from_value(args.excluded_name_prefixes)),
            target_weight_planner=args.target_weight_planner,
            target_weight_planner_error_policy=args.target_weight_planner_error_policy,
            risk_manager_base_url=args.risk_manager_base_url,
            risk_manager_risk_model_id=args.risk_manager_risk_model_id,
            risk_manager_mode=args.risk_manager_mode,
            risk_manager_timeout_secs=args.risk_manager_timeout_secs,
            unfilled_timeout_secs=args.unfilled_timeout_secs,
            resubmit_check_interval_secs=args.resubmit_interval_secs,
            cash_buffer_percent=args.cash_buffer_percent,
            target_cash_buffer_percent=args.target_cash_buffer_percent,
            weight_tolerance_percent=args.weight_tolerance_percent,
            cash_tolerance_percent=args.cash_tolerance_percent,
            stop_time=args.stop_time,
            limit_stop_mode=args.limit_stop_mode,
            exit_non_targets=not args.leave_non_targets,
            order_slice_notional=args.order_slice_notional,
            trade_tick_log_sample_rate=args.trade_tick_log_sample_rate,
            order_book_depth_log_sample_rate=args.order_book_depth_log_sample_rate,
            trading_windows=args.trading_windows,
            order_id_tag=args.order_id_tag,
            subscribe_bars=False,
            subscribe_quote_ticks=False,
            subscribe_trade_ticks=False,
            quote_tick_window_probe_instrument_ids=tuple(context.instrument_ids[:2]),
            subscribe_order_book_depth=True,
            seed_open_from_last_close=True,
            full_tick_refresh_secs=args.full_tick_refresh_secs,
            full_tick_prefetch_time=args.full_tick_prefetch_time,
            process_targets_on_timer=True,
        ),
        refresh_context=lambda active_stock_codes: loader.load(
            extra_stock_codes=extra_stock_codes.union(active_stock_codes),
        ),
        refresh_interval_secs=args.refresh_interval_secs,
        refresh_time=args.refresh_time,
    )
    if not args.no_reconciliation:
        # Always wire the reconcile callback (not only when a pre-open time is configured):
        # the strategy triggers a reconcile on start and on each refresh to republish the
        # execution mass status — which carries the broker sellable (can_use_volume) map —
        # to the strategy's subscription. reconcile_time may be None (no scheduled pre-open
        # run); on-demand triggering via request_execution_reconcile() still works.
        strategy.configure_pre_open_reconciliation(
            reconcile=node.kernel.exec_engine.reconcile_execution_state,
            reconcile_time=args.pre_open_reconcile_time,
            timeout_secs=config_node.timeout_reconciliation,
            loop=node.kernel.exec_engine._loop,
        )

    def _fetch_full_tick() -> dict[str, dict[str, float]]:
        # Today's authoritative full-tick snapshot per instrument from the QMT
        # proxy. Nautilus has no full-tick type, so this reaches the proxy directly
        # (infrastructure plumbing). Covers the full configured universe (not just
        # held positions) so new buy targets are priced from the real open, plus
        # any active positions that dropped out of the universe.
        stock_codes = set(strategy._stock_by_instrument.values())
        stock_codes.update(strategy._active_stock_codes())
        if not stock_codes:
            return {}
        return loader.full_tick_snapshot(sorted(stock_codes))

    def _fetch_positions() -> dict[str, dict[str, Any]]:
        return loader.broker_position_snapshot()

    strategy.configure_full_tick_source(fetch_full_tick=_fetch_full_tick)
    node.trader.add_strategy(strategy)
    _maybe_add_snapshot_recorder(args, node, strategy, _fetch_full_tick, _fetch_positions)
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
    node.add_data_client_factory(QMT_CLIENT, QMTLiveDataClientFactory)
    node.add_exec_client_factory(QMT_CLIENT, QMTLiveExecClientFactory)
    node.build()
    return node


def main() -> None:
    args = parse_args()
    connection = legacy.build_connection(args)
    loader = legacy.LivePredictionDataLoader(args, connection)
    node = build_node(args, loader)
    if args.build_only:
        node.dispose()
        return
    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
