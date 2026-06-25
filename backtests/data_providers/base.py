from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass

from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType


@dataclass(frozen=True)
class PreparedBarData:
    bar_type: BarType
    bars: list[Bar]
    skipped_rows: int


class BarDataProvider(ABC):
    """
    Base class for reusable Nautilus bar data providers.

    Concrete providers own their storage/query details and return prepared
    Nautilus bar objects that backtests can add directly to a BacktestEngine.
    """

    @abstractmethod
    def prepare_bars(
        self,
        symbol: str,
        bar_type: BarType,
        start: str,
        end: str,
        timezone_name: str = "UTC",
        price_precision: int = 2,
        strict_data: bool = False,
        limit: int = 0,
    ) -> PreparedBarData:
        raise NotImplementedError

    def preview_request(
        self,
        symbol: str,
        start: str,
        end: str,
        limit: int = 0,
    ) -> str | None:
        return None
