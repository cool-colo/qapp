from __future__ import annotations

import argparse
import os
from decimal import Decimal
from typing import Any

from backtests.common import add_benchmark_args
from backtests.common import add_tearsheet_args
from backtests.common import benchmark_config_from_args
from backtests.common import benchmark_run_config
from backtests.common import tearsheet_run_config
from backtests.data_providers import ClickHouseConnectionConfig
from backtests.reporting import BacktestReportProcessor


class BaseBacktest(BacktestReportProcessor):
    """Base helper for shared backtest script behavior."""

    @staticmethod
    def env(name: str, default: str | None = None) -> str | None:
        value = os.environ.get(name)
        return value if value not in (None, "") else default

    @classmethod
    def env_bool(cls, name: str, default: bool = False) -> bool:
        value = cls.env(name)
        if value is None:
            return default
        return value.lower() in {"1", "true", "yes", "on"}

    @classmethod
    def env_list(cls, name: str, default: str = "") -> list[str]:
        return [item.strip() for item in (cls.env(name, default) or "").split(",") if item.strip()]

    @staticmethod
    def parse_decimal(value: str) -> Decimal:
        return Decimal(value.replace(",", ""))

    @staticmethod
    def parse_optional_float(value: str | None) -> float | None:
        if value in (None, ""):
            return None
        return float(value)

    @staticmethod
    def env_list_from_value(value: str) -> list[str]:
        return [item.strip() for item in (value or "").split(",") if item.strip()]

    @staticmethod
    def qmt_symbol(value: str) -> str:
        text = value.strip().upper()
        if text.startswith("STOCK:") or text.startswith("STOCK."):
            text = text[6:]
        if text.endswith(".XSHE"):
            return f"{text[:-5]}.SZ"
        if text.endswith(".XSHG"):
            return f"{text[:-5]}.SH"
        if text.endswith(".BJSE"):
            return f"{text[:-5]}.BJ"
        if len(text) >= 3 and text[:2] in {"SZ", "SH", "BJ"} and "." not in text:
            return f"{text[2:]}.{text[:2]}"
        return text

    @classmethod
    def data_symbol(cls, value: str) -> str:
        return f"stock:{cls.qmt_symbol(value)}"

    @staticmethod
    def build_clickhouse_connection(args: argparse.Namespace) -> ClickHouseConnectionConfig:
        return ClickHouseConnectionConfig(
            url=args.clickhouse_url,
            database=args.clickhouse_database,
            user=args.clickhouse_user,
            password=args.clickhouse_password,
            timeout_secs=args.clickhouse_timeout_secs,
        )

    @staticmethod
    def add_benchmark_args(parser: argparse.ArgumentParser) -> None:
        add_benchmark_args(parser)

    @staticmethod
    def add_tearsheet_args(parser: argparse.ArgumentParser) -> None:
        add_tearsheet_args(parser)

    @staticmethod
    def benchmark_run_config(args: argparse.Namespace) -> dict[str, Any]:
        return benchmark_run_config(args)

    @staticmethod
    def tearsheet_run_config(args: argparse.Namespace) -> dict[str, Any]:
        return tearsheet_run_config(args)

    @staticmethod
    def benchmark_config_from_args(args: argparse.Namespace) -> Any:
        return benchmark_config_from_args(args)
