#!/usr/bin/env python3
from __future__ import annotations

import os
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
    args = legacy.parse_args()
    # `--pre-open-reconcile-time` is target-specific and is not defined by the shared
    # `live_common.parse_args()`, so fall back to its env default when the shared parser
    # did not populate it. Configuring it triggers a pre-open execution-state reconcile,
    # which also refreshes the broker sellable (can_use_volume) map before trading.
    if not hasattr(args, "pre_open_reconcile_time"):
        args.pre_open_reconcile_time = os.environ.get("MODEL_PRE_OPEN_RECONCILE_TIME") or None
    try:
        args.refresh_time = normalize_refresh_time(args.refresh_time)
        args.pre_open_reconcile_time = normalize_refresh_time(args.pre_open_reconcile_time)
    except ValueError as exc:
        raise SystemExit(f"invalid configured HH:MM time: {exc}") from exc
    return args


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
            price_offset_ticks=args.price_offset_ticks,
            quote_tick_log_sample_rate=args.quote_tick_log_sample_rate,
            trade_tick_log_sample_rate=args.trade_tick_log_sample_rate,
            order_book_depth_log_sample_rate=args.order_book_depth_log_sample_rate,
            trading_windows=args.trading_windows,
            order_id_tag=args.order_id_tag,
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
        )
    node.trader.add_strategy(strategy)
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
