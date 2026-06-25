from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from decimal import ROUND_HALF_UP
from pathlib import Path
from typing import Any

import pandas as pd

from backtests.common import apply_benchmark_to_reports
from backtests.common import benchmark_config_from_args
from backtests.common import final_benchmark_metrics
from backtests.result_writers import DailyAccountRecord
from backtests.result_writers import DailyPerformanceRecord
from backtests.result_writers import DailyPositionRecord
from backtests.result_writers import SummaryMetricRecord
from backtests.result_writers import TradeRecord


class BacktestReportProcessor:
    """
    Base report processor shared by backtest runners.

    Strategy scripts still own strategy-specific portfolio reconstruction and
    signal/target/order frame conversion. Generic report formatting, raw
    Nautilus report collection, benchmark enrichment, CSV export, and common
    result-writer records live here.
    """

    def __init__(
        self,
        report_decimal_quantum: Decimal = Decimal("0.001"),
        csv_index: bool = False,
    ) -> None:
        self.report_decimal_quantum = report_decimal_quantum
        self.csv_index = csv_index

    def raw_engine_reports(self, engine: Any) -> dict[str, pd.DataFrame]:
        from nautilus_trader.adapters.qmt.constants import QMT_VENUE

        return {
            "engine_cash_ledger": self.safe_report(lambda: engine.trader.generate_account_report(QMT_VENUE)),
            "fills": self.safe_report(lambda: engine.trader.generate_order_fills_report()),
            "positions": self.safe_report(lambda: engine.trader.generate_positions_report()),
        }

    def complete_report(
        self,
        engine: Any,
        daily_portfolio: pd.DataFrame,
        daily_positions: pd.DataFrame,
        extra_reports: dict[str, pd.DataFrame] | None = None,
        raw_reports: dict[str, pd.DataFrame] | None = None,
    ) -> dict[str, pd.DataFrame]:
        raw_reports = raw_reports or self.raw_engine_reports(engine)
        reports = {
            "daily_portfolio": daily_portfolio,
            "daily_positions": daily_positions,
            "cash_ledger": self.daily_cash_ledger(daily_portfolio),
            **raw_reports,
        }
        reports.update(extra_reports or {})
        return reports

    def enrich_with_benchmark(
        self,
        args: Any,
        connection: Any,
        reports: dict[str, pd.DataFrame],
    ) -> dict[str, pd.DataFrame]:
        return apply_benchmark_to_reports(args, connection, reports)

    def daily_cash_ledger(self, daily_portfolio: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(daily_portfolio, pd.DataFrame) or daily_portfolio.empty:
            return pd.DataFrame()
        columns = [
            "date",
            "cash",
            "market_value",
            "total_equity",
            "net_value",
            "active_positions",
        ]
        available = [column for column in columns if column in daily_portfolio]
        return daily_portfolio.loc[:, available].copy()

    def write_report_dir(self, report_dir: str, reports: dict[str, pd.DataFrame]) -> None:
        path = Path(report_dir)
        path.mkdir(parents=True, exist_ok=True)
        for name, frame in reports.items():
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                self.format_report_frame(frame).to_csv(path / f"{name}.csv", index=self.csv_index)

    def write_tearsheet(
        self,
        args: Any,
        engine: Any,
        reports: dict[str, pd.DataFrame] | None = None,
    ) -> str | None:
        output_path = str(getattr(args, "tearsheet_path", "") or "").strip()
        if not output_path:
            return None

        from nautilus_trader.analysis import TearsheetConfig
        from nautilus_trader.analysis import create_tearsheet

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        benchmark_config = benchmark_config_from_args(args)
        benchmark_returns = self.tearsheet_benchmark_returns(reports or {})
        benchmark_name = benchmark_config.display_name if benchmark_config.enabled else "Benchmark"
        title = str(getattr(args, "tearsheet_title", "") or "").strip() or "NautilusTrader Backtest Results"
        theme = str(getattr(args, "tearsheet_theme", "plotly_white") or "plotly_white")
        height = int(getattr(args, "tearsheet_height", 1500) or 1500)

        create_tearsheet(
            engine=engine,
            output_path=str(path),
            title=title,
            config=TearsheetConfig(
                title=title,
                theme=theme,
                height=height,
                benchmark_name=benchmark_name,
                include_benchmark=benchmark_returns is not None,
            ),
            benchmark_returns=benchmark_returns,
            benchmark_name=benchmark_name,
        )
        return str(path)

    def tearsheet_benchmark_returns(self, reports: dict[str, pd.DataFrame]) -> pd.Series | None:
        for report_name in ("benchmark", "daily_portfolio"):
            frame = reports.get(report_name, pd.DataFrame())
            series = self.benchmark_returns_from_frame(frame)
            if series is not None:
                return series
        return None

    @staticmethod
    def benchmark_returns_from_frame(frame: Any) -> pd.Series | None:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return None
        if "benchmark_return" not in frame:
            return None

        if "date" in frame:
            index = pd.to_datetime(frame["date"], errors="coerce")
        else:
            index = pd.to_datetime(frame.index, errors="coerce")

        values = pd.to_numeric(frame["benchmark_return"], errors="coerce")
        series = pd.Series(values.to_numpy(dtype=float), index=index).dropna().sort_index()
        if series.empty:
            return None
        return series

    def print_complete_report(self, engine: Any, complete_report: dict[str, pd.DataFrame]) -> None:
        from nautilus_trader.adapters.qmt.constants import QMT_VENUE

        with pd.option_context("display.max_rows", 100, "display.max_columns", None, "display.width", 300):
            portfolio = complete_report.get("daily_portfolio", pd.DataFrame())
            daily_positions = complete_report.get("daily_positions", pd.DataFrame())
            benchmark = complete_report.get("benchmark", pd.DataFrame())
            if not portfolio.empty:
                print("\nPortfolio equity report")
                print(self.format_report_frame(portfolio.tail(20)).to_string(index=False))
                print("\nFinal portfolio summary")
                print(self.format_report_frame(portfolio.tail(1)).to_string(index=False))
            if not daily_positions.empty:
                final_date = daily_positions["date"].max()
                print("\nFinal open positions")
                print(
                    self.format_report_frame(
                        daily_positions.loc[daily_positions["date"] == final_date],
                    ).to_string(index=False),
                )
            if isinstance(benchmark, pd.DataFrame) and not benchmark.empty:
                print("\nBenchmark report")
                print(self.format_report_frame(benchmark.tail(20)).to_string(index=False))
            print("\nRaw Nautilus account event ledger")
            print(engine.trader.generate_account_report(QMT_VENUE))
            print("\nOrder fills report")
            print(engine.trader.generate_order_fills_report())
            print("\nPositions report")
            print(engine.trader.generate_positions_report())

    def print_raw_engine_reports(self, engine: Any) -> None:
        from nautilus_trader.adapters.qmt.constants import QMT_VENUE

        with pd.option_context("display.max_rows", 100, "display.max_columns", None, "display.width", 300):
            print("\nAccount report")
            print(engine.trader.generate_account_report(QMT_VENUE))
            print("\nOrder fills report")
            print(engine.trader.generate_order_fills_report())
            print("\nPositions report")
            print(engine.trader.generate_positions_report())

    def format_report_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        return frame.map(self.format_report_value)

    def format_report_value(self, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, (bool, int)):
            return value
        if isinstance(value, Decimal):
            return self.quantize_report_decimal(value)
        if isinstance(value, float):
            if not pd.notna(value):
                return value
            return self.quantize_report_decimal(Decimal(str(value)))
        return value

    def quantize_report_decimal(self, value: Decimal) -> Decimal:
        rounded = value.quantize(self.report_decimal_quantum, rounding=ROUND_HALF_UP)
        return abs(rounded) if rounded == 0 else rounded

    def safe_report(self, callback: Any) -> pd.DataFrame:
        try:
            return callback()
        except TypeError:
            return callback()
        except Exception:
            return pd.DataFrame()

    def trade_records_from_report(self, experiment_id_value: str, frame: Any) -> list[TradeRecord]:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return []
        records = []
        for index, row in frame.reset_index().iterrows():
            price = self.decimal_or_zero(self.first_value(row, "last_px", "last_price", "price", "avg_px", default=0))
            quantity = self.int_or_zero(self.first_value(row, "last_qty", "quantity", "filled_qty", "qty", default=0))
            amount = price * Decimal(quantity)
            trade_time = self.timestamp_value(
                self.first_value(row, "ts_event", "ts_init", "time", "datetime", default=None),
            )
            if trade_time is None:
                trade_time = datetime.now()
            records.append(
                TradeRecord(
                    experiment_id=experiment_id_value,
                    trade_id=str(
                        self.first_value(row, "trade_id", "venue_order_id", "client_order_id", default=f"trade-{index}"),
                    ),
                    order_id=str(
                        self.first_value(row, "client_order_id", "order_id", "venue_order_id", default=f"order-{index}"),
                    ),
                    trading_date=trade_time.date(),
                    trade_time=trade_time,
                    instrument_id=str(self.first_value(row, "instrument_id", default="")),
                    side=self.side_text(self.first_value(row, "order_side", "side", default="")),
                    price=price,
                    quantity=quantity,
                    amount=amount,
                    commission=self.decimal_or_zero(self.first_value(row, "commission", default=0)),
                    total_cost=self.decimal_or_zero(self.first_value(row, "commission", default=0)),
                ),
            )
        return records

    def daily_account_records(self, experiment_id_value: str, frame: Any) -> list[DailyAccountRecord]:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return []
        records = []
        for _, row in frame.reset_index().iterrows():
            trading_date = pd.Timestamp(self.first_value(row, "date", "trading_date", default=datetime.now())).date()
            cash = self.decimal_or_zero(self.first_value(row, "cash", default=0))
            market_value = self.decimal_or_zero(self.first_value(row, "market_value", default=0))
            total_value = self.decimal_or_zero(
                self.first_value(row, "total_equity", "total_value", default=cash + market_value),
            )
            records.append(
                DailyAccountRecord(
                    experiment_id=experiment_id_value,
                    trading_date=trading_date,
                    cash=cash,
                    frozen_cash=self.decimal_or_zero(self.first_value(row, "frozen_cash", default=0)),
                    market_value=market_value,
                    total_value=total_value,
                    net_value=self.decimal_or_zero(self.first_value(row, "net_value", default=0)),
                ),
            )
        return records

    def daily_performance_records(self, experiment_id_value: str, frame: Any) -> list[DailyPerformanceRecord]:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return []
        records = []
        max_net = Decimal("0")
        previous_net = None
        for _, row in frame.reset_index().iterrows():
            trading_date = pd.Timestamp(self.first_value(row, "date", "trading_date", default=datetime.now())).date()
            net = self.decimal_or_zero(self.first_value(row, "net_value", default=0))
            max_net = max(max_net, net)
            daily_return = self.decimal_or_none(self.first_value(row, "daily_return", default=None))
            if daily_return is None:
                daily_return = Decimal("0") if previous_net in (None, Decimal("0")) else net / previous_net - Decimal("1")
            cum_return = self.decimal_or_none(self.first_value(row, "cum_return", default=None))
            if cum_return is None:
                cum_return = net - Decimal("1")
            drawdown = self.decimal_or_none(self.first_value(row, "drawdown", default=None))
            if drawdown is None:
                drawdown = Decimal("0") if max_net == 0 else net / max_net - Decimal("1")
            records.append(
                DailyPerformanceRecord(
                    experiment_id=experiment_id_value,
                    trading_date=trading_date,
                    net_value=net,
                    daily_return=daily_return,
                    cum_return=cum_return,
                    drawdown=drawdown,
                    benchmark_net_value=self.decimal_or_none(self.first_value(row, "benchmark_net_value", default=None)),
                    benchmark_daily_return=self.decimal_or_none(self.first_value(row, "benchmark_return", default=None)),
                    benchmark_cum_return=self.decimal_or_none(self.first_value(row, "benchmark_cum_return", default=None)),
                    daily_excess_return=self.decimal_or_none(self.first_value(row, "excess_return", default=None)),
                    cum_excess_return=self.decimal_or_none(self.first_value(row, "excess_cum_return", default=None)),
                ),
            )
            previous_net = net
        return records

    def daily_position_records(self, experiment_id_value: str, frame: Any) -> list[DailyPositionRecord]:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return []
        records = []
        for _, row in frame.reset_index().iterrows():
            quantity = self.int_or_zero(self.first_value(row, "quantity", "signed_qty", "qty", default=0))
            avg_cost = self.decimal_or_zero(self.first_value(row, "avg_cost", "avg_px_open", "avg_price", default=0))
            last_price = self.decimal_or_zero(self.first_value(row, "last_price", "last_px", default=avg_cost))
            market_value = self.decimal_or_zero(
                self.first_value(row, "market_value", default=Decimal(quantity) * last_price),
            )
            records.append(
                DailyPositionRecord(
                    experiment_id=experiment_id_value,
                    trading_date=pd.Timestamp(self.first_value(row, "date", "trading_date", default=datetime.now())).date(),
                    instrument_id=str(self.first_value(row, "instrument_id", default="")),
                    quantity=quantity,
                    sellable_quantity=quantity,
                    avg_cost=avg_cost,
                    last_price=last_price,
                    market_value=market_value,
                    weight=self.decimal_or_zero(self.first_value(row, "weight", default=0)),
                    unrealized_pnl=self.decimal_or_zero(
                        self.first_value(row, "unrealized_pnl", default=Decimal(quantity) * (last_price - avg_cost)),
                    ),
                ),
            )
        return records

    def summary_metric_records(
        self,
        experiment_id_value: str,
        count_metrics: dict[str, Any],
        portfolio_report: Any,
        account_report: Any | None = None,
    ) -> list[SummaryMetricRecord]:
        metrics: dict[str, Any] = dict(count_metrics)
        if isinstance(account_report, pd.DataFrame) and not account_report.empty:
            final = account_report.reset_index().iloc[-1]
            metrics["final_cash"] = self.first_value(final, "total", "total_value", "balance_total", "equity", default=None)
        if isinstance(portfolio_report, pd.DataFrame) and not portfolio_report.empty:
            final_portfolio = portfolio_report.reset_index().iloc[-1]
            metrics["final_total_equity"] = self.first_value(final_portfolio, "total_equity", default=None)
            metrics["final_market_value"] = self.first_value(final_portfolio, "market_value", default=None)
            metrics["final_return"] = self.first_value(final_portfolio, "cum_return", default=None)
            metrics.update(final_benchmark_metrics(portfolio_report))

        records = []
        for name, value in metrics.items():
            numeric = self.decimal_or_none(value)
            records.append(
                SummaryMetricRecord(
                    experiment_id=experiment_id_value,
                    metric_group="trade" if name.endswith("_count") else "return",
                    metric_name=name,
                    metric_value=numeric,
                    metric_text_value=None if numeric is not None else str(value),
                    metric_value_type="float" if numeric is not None else "string",
                ),
            )
        return records

    def decimal_or_none(self, value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not pd.notna(numeric):
            return None
        return Decimal(str(numeric))

    def decimal_or_zero(self, value: Any) -> Decimal:
        return self.decimal_or_none(value) or Decimal("0")

    def int_or_zero(self, value: Any) -> int:
        try:
            return int(Decimal(str(value)))
        except Exception:
            return 0

    def first_value(self, row: pd.Series, *names: str, default: Any = None) -> Any:
        for name in names:
            if name in row and pd.notna(row[name]):
                return row[name]
            lower = name.lower()
            for column in row.index:
                if str(column).lower() == lower and pd.notna(row[column]):
                    return row[column]
        return default

    def timestamp_value(self, value: Any, fallback_date: pd.Timestamp | None = None) -> datetime | None:
        if value in (None, ""):
            return None if fallback_date is None else pd.Timestamp(fallback_date).to_pydatetime()
        try:
            if isinstance(value, (int, float)) or str(value).isdigit():
                number = int(value)
                if number > 10_000_000_000_000:
                    return pd.Timestamp(number, unit="ns", tz="UTC").to_pydatetime()
                if number > 10_000_000_000:
                    return pd.Timestamp(number, unit="ms", tz="UTC").to_pydatetime()
                return pd.Timestamp(number, unit="s", tz="UTC").to_pydatetime()
            return pd.Timestamp(value).to_pydatetime()
        except Exception:
            return None if fallback_date is None else pd.Timestamp(fallback_date).to_pydatetime()

    def side_text(self, value: Any) -> str:
        text = str(value or "").lower()
        if "buy" in text:
            return "buy"
        if "sell" in text:
            return "sell"
        return text
