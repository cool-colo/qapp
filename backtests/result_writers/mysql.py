from __future__ import annotations

import json
from datetime import date
from datetime import datetime
from decimal import Decimal
from typing import Any
from typing import Iterable
from typing import Mapping
from typing import Sequence

from backtests.result_writers.records import DailyAccountRecord
from backtests.result_writers.records import DailyPerformanceRecord
from backtests.result_writers.records import DailyPositionRecord
from backtests.result_writers.records import ExperimentParamRecord
from backtests.result_writers.records import ExperimentRecord
from backtests.result_writers.records import OrderRecord
from backtests.result_writers.records import SignalRecord
from backtests.result_writers.records import SummaryMetricRecord
from backtests.result_writers.records import TargetPortfolioRecord
from backtests.result_writers.records import TradeRecord
from backtests.result_writers.writer import ResultWriter


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)


def _timestamp(value: datetime | None) -> datetime:
    return value or datetime.now()


class MySQLResultWriter(ResultWriter):
    """Persist migrated Nautilus backtest records into the existing bt_* schema."""

    def __init__(self, connection=None, connect_kwargs: Mapping[str, Any] | None = None, commit: bool = True) -> None:
        self._connection = connection or self._create_connection(connect_kwargs or {})
        self._commit = commit

    @classmethod
    def from_pymysql_kwargs(cls, **connect_kwargs: Any) -> "MySQLResultWriter":
        return cls(connect_kwargs=connect_kwargs)

    @staticmethod
    def _create_connection(connect_kwargs: Mapping[str, Any]):
        try:
            import pymysql
        except ImportError as exc:
            raise ImportError("pymysql is required to write backtest results to MySQL") from exc
        return pymysql.connect(**dict(connect_kwargs))

    def close(self) -> None:
        close = getattr(self._connection, "close", None)
        if close is not None:
            close()

    def create_experiment(self, experiment: ExperimentRecord) -> None:
        self._upsert_one(
            "bt_experiment",
            {
                "experiment_id": experiment.experiment_id,
                "experiment_name": experiment.experiment_name,
                "strategy_id": experiment.strategy_id,
                "strategy_version_id": experiment.strategy_version_id,
                "model_id": experiment.model_id,
                "data_snapshot_id": experiment.data_snapshot_id,
                "start_date": experiment.start_date,
                "end_date": experiment.end_date,
                "frequency": experiment.frequency,
                "benchmark": experiment.benchmark,
                "initial_cash": experiment.initial_cash,
                "currency": experiment.currency,
                "universe_id": experiment.universe_id,
                "engine_name": experiment.engine_name,
                "engine_version": experiment.engine_version,
                "cost_config": _json_dumps(experiment.cost_config) or "{}",
                "slippage_config": _json_dumps(experiment.slippage_config) or "{}",
                "risk_config": _json_dumps(experiment.risk_config) or "{}",
                "run_config": _json_dumps(experiment.run_config) or "{}",
                "status": experiment.status,
                "error_message": experiment.error_message,
                "started_at": experiment.started_at,
                "finished_at": experiment.finished_at,
                "created_at": _timestamp(experiment.created_at),
                "schema_version": experiment.schema_version,
            },
            key_columns=("experiment_id",),
            preserve_columns=("created_at",),
        )

    def update_experiment_status(
        self,
        experiment_id: str,
        status: str,
        error_message: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        assignments = ["status = %s", "error_message = %s"]
        params: list[Any] = [status, error_message]
        if started_at is not None:
            assignments.append("started_at = %s")
            params.append(started_at)
        if finished_at is not None:
            assignments.append("finished_at = %s")
            params.append(finished_at)
        params.append(experiment_id)
        self._execute(
            f"UPDATE bt_experiment SET {', '.join(assignments)} WHERE experiment_id = %s",
            tuple(params),
        )

    def write_experiment_params(self, records: Sequence[ExperimentParamRecord]) -> None:
        self._upsert_many(
            "bt_experiment_param",
            [
                {
                    "experiment_id": record.experiment_id,
                    "param_group": record.param_group,
                    "param_name": record.param_name,
                    "param_value": record.param_value,
                    "param_type": record.param_type,
                    "created_at": _timestamp(record.created_at),
                    "schema_version": record.schema_version,
                }
                for record in records
            ],
            key_columns=("experiment_id", "param_group", "param_name"),
            preserve_columns=("created_at",),
        )

    def write_signals(self, records: Sequence[SignalRecord]) -> None:
        self._upsert_many(
            "bt_signal",
            [
                {
                    "experiment_id": record.experiment_id,
                    "signal_date": record.signal_date,
                    "instrument_id": record.instrument_id,
                    "signal_name": record.signal_name,
                    "model_id": record.model_id,
                    "signal_value": record.signal_value,
                    "score": record.score,
                    "signal_rank": record.signal_rank,
                    "selected": 1 if record.selected else 0,
                    "reason": record.reason,
                    "extra": _json_dumps(record.extra),
                    "created_at": _timestamp(record.created_at),
                    "schema_version": record.schema_version,
                }
                for record in records
            ],
            key_columns=("experiment_id", "signal_date", "instrument_id", "signal_name", "model_id"),
            preserve_columns=("created_at",),
        )

    def write_target_portfolios(self, records: Sequence[TargetPortfolioRecord]) -> None:
        self._upsert_many(
            "bt_target_portfolio",
            [
                {
                    "experiment_id": record.experiment_id,
                    "target_id": record.target_id,
                    "target_date": record.target_date,
                    "execute_date": record.execute_date,
                    "instrument_id": record.instrument_id,
                    "target_weight": record.target_weight,
                    "current_weight": record.current_weight,
                    "delta_weight": record.delta_weight,
                    "source_signal_name": record.source_signal_name,
                    "source_model_id": record.source_model_id,
                    "reason": record.reason,
                    "extra": _json_dumps(record.extra),
                    "created_at": _timestamp(record.created_at),
                    "schema_version": record.schema_version,
                }
                for record in records
            ],
            key_columns=("experiment_id", "target_id"),
            preserve_columns=("created_at",),
        )

    def write_orders(self, records: Sequence[OrderRecord]) -> None:
        self._upsert_many(
            "bt_order",
            [
                {
                    "experiment_id": record.experiment_id,
                    "order_id": record.order_id,
                    "source_target_id": record.source_target_id,
                    "trading_date": record.trading_date,
                    "submit_time": record.submit_time,
                    "instrument_id": record.instrument_id,
                    "side": record.side,
                    "order_type": record.order_type,
                    "price_type": record.price_type,
                    "limit_price": record.limit_price,
                    "quantity": record.quantity,
                    "amount": record.amount,
                    "target_weight": record.target_weight,
                    "status": record.status,
                    "filled_quantity": record.filled_quantity,
                    "avg_fill_price": record.avg_fill_price,
                    "filled_amount": record.filled_amount,
                    "rejected_reason": record.rejected_reason,
                    "cancelled_reason": record.cancelled_reason,
                    "extra": _json_dumps(record.extra),
                    "created_at": _timestamp(record.created_at),
                    "updated_at": _timestamp(record.updated_at),
                    "schema_version": record.schema_version,
                }
                for record in records
            ],
            key_columns=("experiment_id", "order_id"),
            preserve_columns=("created_at",),
        )

    def write_trades(self, records: Sequence[TradeRecord]) -> None:
        self._upsert_many(
            "bt_trade",
            [
                {
                    "experiment_id": record.experiment_id,
                    "trade_id": record.trade_id,
                    "order_id": record.order_id,
                    "trading_date": record.trading_date,
                    "trade_time": record.trade_time,
                    "instrument_id": record.instrument_id,
                    "side": record.side,
                    "price": record.price,
                    "quantity": record.quantity,
                    "amount": record.amount,
                    "commission": record.commission,
                    "tax": record.tax,
                    "slippage_cost": record.slippage_cost,
                    "total_cost": record.total_cost,
                    "created_at": _timestamp(record.created_at),
                    "schema_version": record.schema_version,
                }
                for record in records
            ],
            key_columns=("experiment_id", "trade_id"),
            preserve_columns=("created_at",),
        )

    def write_daily_positions(self, records: Sequence[DailyPositionRecord]) -> None:
        self._upsert_many(
            "bt_daily_position",
            [
                {
                    "experiment_id": record.experiment_id,
                    "trading_date": record.trading_date,
                    "instrument_id": record.instrument_id,
                    "quantity": record.quantity,
                    "sellable_quantity": record.sellable_quantity,
                    "avg_cost": record.avg_cost,
                    "last_price": record.last_price,
                    "market_value": record.market_value,
                    "weight": record.weight,
                    "unrealized_pnl": record.unrealized_pnl,
                    "realized_pnl": record.realized_pnl,
                    "holding_days": record.holding_days,
                    "created_at": _timestamp(record.created_at),
                    "schema_version": record.schema_version,
                }
                for record in records
            ],
            key_columns=("experiment_id", "trading_date", "instrument_id"),
            preserve_columns=("created_at",),
        )

    def write_daily_accounts(self, records: Sequence[DailyAccountRecord]) -> None:
        self._upsert_many(
            "bt_daily_account",
            [
                {
                    "experiment_id": record.experiment_id,
                    "trading_date": record.trading_date,
                    "cash": record.cash,
                    "frozen_cash": record.frozen_cash,
                    "market_value": record.market_value,
                    "total_value": record.total_value,
                    "net_value": record.net_value,
                    "daily_deposit": record.daily_deposit,
                    "daily_withdraw": record.daily_withdraw,
                    "cash_flow": record.cash_flow,
                    "commission": record.commission,
                    "tax": record.tax,
                    "slippage_cost": record.slippage_cost,
                    "total_cost": record.total_cost,
                    "created_at": _timestamp(record.created_at),
                    "schema_version": record.schema_version,
                }
                for record in records
            ],
            key_columns=("experiment_id", "trading_date"),
            preserve_columns=("created_at",),
        )

    def write_daily_performance(self, records: Sequence[DailyPerformanceRecord]) -> None:
        self._upsert_many(
            "bt_daily_performance",
            [
                {
                    "experiment_id": record.experiment_id,
                    "trading_date": record.trading_date,
                    "net_value": record.net_value,
                    "daily_return": record.daily_return,
                    "cum_return": record.cum_return,
                    "benchmark_net_value": record.benchmark_net_value,
                    "benchmark_daily_return": record.benchmark_daily_return,
                    "benchmark_cum_return": record.benchmark_cum_return,
                    "daily_excess_return": record.daily_excess_return,
                    "cum_excess_return": record.cum_excess_return,
                    "drawdown": record.drawdown,
                    "turnover": record.turnover,
                    "commission": record.commission,
                    "tax": record.tax,
                    "slippage_cost": record.slippage_cost,
                    "total_cost": record.total_cost,
                    "created_at": _timestamp(record.created_at),
                    "schema_version": record.schema_version,
                }
                for record in records
            ],
            key_columns=("experiment_id", "trading_date"),
            preserve_columns=("created_at",),
        )

    def write_summary_metrics(self, records: Sequence[SummaryMetricRecord]) -> None:
        self._upsert_many(
            "bt_summary_metric",
            [
                {
                    "experiment_id": record.experiment_id,
                    "metric_group": record.metric_group,
                    "metric_name": record.metric_name,
                    "metric_value": record.metric_value,
                    "metric_text_value": record.metric_text_value,
                    "metric_unit": record.metric_unit,
                    "metric_value_type": record.metric_value_type,
                    "created_at": _timestamp(record.created_at),
                    "schema_version": record.schema_version,
                }
                for record in records
            ],
            key_columns=("experiment_id", "metric_group", "metric_name"),
            preserve_columns=("created_at",),
        )

    def _upsert_one(
        self,
        table: str,
        row: Mapping[str, Any],
        key_columns: Sequence[str],
        preserve_columns: Sequence[str] = (),
    ) -> None:
        self._upsert_many(table, [row], key_columns, preserve_columns)

    def _upsert_many(
        self,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        key_columns: Sequence[str],
        preserve_columns: Sequence[str] = (),
    ) -> None:
        if not rows:
            return
        columns = list(rows[0].keys())
        updates = [
            f"{self._quote_identifier(column)} = VALUES({self._quote_identifier(column)})"
            for column in columns
            if column not in set(key_columns).union(preserve_columns)
        ]
        sql = (
            f"INSERT INTO {self._quote_identifier(table)} "
            f"({', '.join(self._quote_identifier(column) for column in columns)}) "
            f"VALUES ({', '.join(['%s'] * len(columns))}) "
            f"ON DUPLICATE KEY UPDATE {', '.join(updates)}"
        )
        params = [tuple(row[column] for column in columns) for row in rows]
        self._executemany(sql, params)

    def _execute(self, sql: str, params: Sequence[Any]) -> None:
        cursor = self._connection.cursor()
        try:
            cursor.execute(sql, params)
            self._commit_if_needed()
        except Exception:
            self._rollback_if_needed()
            raise
        finally:
            self._close_cursor(cursor)

    def _executemany(self, sql: str, params: Iterable[Sequence[Any]]) -> None:
        cursor = self._connection.cursor()
        try:
            cursor.executemany(sql, list(params))
            self._commit_if_needed()
        except Exception:
            self._rollback_if_needed()
            raise
        finally:
            self._close_cursor(cursor)

    def _commit_if_needed(self) -> None:
        if self._commit:
            self._connection.commit()

    def _rollback_if_needed(self) -> None:
        if not self._commit:
            return
        rollback = getattr(self._connection, "rollback", None)
        if rollback is not None:
            rollback()

    @staticmethod
    def _close_cursor(cursor) -> None:
        close = getattr(cursor, "close", None)
        if close is not None:
            close()

    @staticmethod
    def _quote_identifier(value: str) -> str:
        if not value.replace("_", "").isalnum():
            raise ValueError(f"Unsafe MySQL identifier: {value}")
        return f"`{value}`"
