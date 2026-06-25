from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from datetime import datetime
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


class ResultWriter(ABC):
    @abstractmethod
    def create_experiment(self, experiment: ExperimentRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def update_experiment_status(
        self,
        experiment_id: str,
        status: str,
        error_message: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def write_experiment_params(self, records: Sequence[ExperimentParamRecord]) -> None:
        raise NotImplementedError

    @abstractmethod
    def write_signals(self, records: Sequence[SignalRecord]) -> None:
        raise NotImplementedError

    @abstractmethod
    def write_target_portfolios(self, records: Sequence[TargetPortfolioRecord]) -> None:
        raise NotImplementedError

    @abstractmethod
    def write_orders(self, records: Sequence[OrderRecord]) -> None:
        raise NotImplementedError

    @abstractmethod
    def write_trades(self, records: Sequence[TradeRecord]) -> None:
        raise NotImplementedError

    @abstractmethod
    def write_daily_positions(self, records: Sequence[DailyPositionRecord]) -> None:
        raise NotImplementedError

    @abstractmethod
    def write_daily_accounts(self, records: Sequence[DailyAccountRecord]) -> None:
        raise NotImplementedError

    @abstractmethod
    def write_daily_performance(self, records: Sequence[DailyPerformanceRecord]) -> None:
        raise NotImplementedError

    @abstractmethod
    def write_summary_metrics(self, records: Sequence[SummaryMetricRecord]) -> None:
        raise NotImplementedError

    def finalize_experiment(self, experiment_id: str, finished_at: datetime | None = None) -> None:
        self.update_experiment_status(experiment_id, "succeeded", finished_at=finished_at)

    def fail_experiment(
        self,
        experiment_id: str,
        error_message: str,
        finished_at: datetime | None = None,
    ) -> None:
        self.update_experiment_status(
            experiment_id,
            "failed",
            error_message=error_message,
            finished_at=finished_at,
        )


class NullResultWriter(ResultWriter):
    def create_experiment(self, experiment: ExperimentRecord) -> None:
        return None

    def update_experiment_status(
        self,
        experiment_id: str,
        status: str,
        error_message: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        return None

    def write_experiment_params(self, records: Sequence[ExperimentParamRecord]) -> None:
        return None

    def write_signals(self, records: Sequence[SignalRecord]) -> None:
        return None

    def write_target_portfolios(self, records: Sequence[TargetPortfolioRecord]) -> None:
        return None

    def write_orders(self, records: Sequence[OrderRecord]) -> None:
        return None

    def write_trades(self, records: Sequence[TradeRecord]) -> None:
        return None

    def write_daily_positions(self, records: Sequence[DailyPositionRecord]) -> None:
        return None

    def write_daily_accounts(self, records: Sequence[DailyAccountRecord]) -> None:
        return None

    def write_daily_performance(self, records: Sequence[DailyPerformanceRecord]) -> None:
        return None

    def write_summary_metrics(self, records: Sequence[SummaryMetricRecord]) -> None:
        return None
