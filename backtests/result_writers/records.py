from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import datetime
from decimal import Decimal
from typing import Any
from typing import Mapping


JsonMapping = Mapping[str, Any]


@dataclass(frozen=True)
class ExperimentRecord:
    experiment_id: str
    experiment_name: str
    strategy_id: str
    strategy_version_id: str
    start_date: date
    end_date: date
    frequency: str
    initial_cash: Decimal
    engine_name: str
    status: str
    model_id: str | None = None
    data_snapshot_id: str | None = None
    benchmark: str | None = None
    currency: str = "CNY"
    universe_id: str | None = None
    engine_version: str | None = None
    cost_config: JsonMapping = field(default_factory=dict)
    slippage_config: JsonMapping = field(default_factory=dict)
    risk_config: JsonMapping = field(default_factory=dict)
    run_config: JsonMapping = field(default_factory=dict)
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class ExperimentParamRecord:
    experiment_id: str
    param_name: str
    param_value: str
    param_type: str
    param_group: str = ""
    created_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class SignalRecord:
    experiment_id: str
    signal_date: date
    instrument_id: str
    signal_name: str
    model_id: str = ""
    signal_value: Decimal | None = None
    score: Decimal | None = None
    signal_rank: int | None = None
    selected: bool = False
    reason: str | None = None
    extra: JsonMapping | None = None
    created_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class TargetPortfolioRecord:
    experiment_id: str
    target_id: str
    target_date: date
    execute_date: date
    instrument_id: str
    target_weight: Decimal | None = None
    current_weight: Decimal | None = None
    delta_weight: Decimal | None = None
    source_signal_name: str | None = None
    source_model_id: str | None = None
    reason: str | None = None
    extra: JsonMapping | None = None
    created_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class OrderRecord:
    experiment_id: str
    order_id: str
    trading_date: date
    submit_time: datetime
    instrument_id: str
    side: str
    order_type: str
    price_type: str
    status: str
    source_target_id: str | None = None
    limit_price: Decimal | None = None
    quantity: int | None = None
    amount: Decimal | None = None
    target_weight: Decimal | None = None
    filled_quantity: int = 0
    avg_fill_price: Decimal | None = None
    filled_amount: Decimal = Decimal("0")
    rejected_reason: str | None = None
    cancelled_reason: str | None = None
    extra: JsonMapping | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class TradeRecord:
    experiment_id: str
    trade_id: str
    order_id: str
    trading_date: date
    trade_time: datetime
    instrument_id: str
    side: str
    price: Decimal
    quantity: int
    amount: Decimal
    commission: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    slippage_cost: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    created_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class DailyPositionRecord:
    experiment_id: str
    trading_date: date
    instrument_id: str
    quantity: int
    sellable_quantity: int
    avg_cost: Decimal
    last_price: Decimal
    market_value: Decimal
    weight: Decimal
    unrealized_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    holding_days: int = 0
    created_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class DailyAccountRecord:
    experiment_id: str
    trading_date: date
    cash: Decimal
    market_value: Decimal
    total_value: Decimal
    net_value: Decimal
    frozen_cash: Decimal = Decimal("0")
    daily_deposit: Decimal = Decimal("0")
    daily_withdraw: Decimal = Decimal("0")
    cash_flow: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    slippage_cost: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    created_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class DailyPerformanceRecord:
    experiment_id: str
    trading_date: date
    net_value: Decimal
    daily_return: Decimal
    cum_return: Decimal
    drawdown: Decimal
    benchmark_net_value: Decimal | None = None
    benchmark_daily_return: Decimal | None = None
    benchmark_cum_return: Decimal | None = None
    daily_excess_return: Decimal | None = None
    cum_excess_return: Decimal | None = None
    turnover: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    slippage_cost: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    created_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class SummaryMetricRecord:
    experiment_id: str
    metric_group: str
    metric_name: str
    metric_value_type: str
    metric_value: Decimal | None = None
    metric_text_value: str | None = None
    metric_unit: str | None = None
    created_at: datetime | None = None
    schema_version: int = 1
