from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re

import pandas as pd


@dataclass(frozen=True)
class BacktestTimeRange:
    """Receive-time interval [start, end), in the exchange timezone.

    Date-only end arguments include that entire date. Datetime end arguments are
    exclusive, including when their precision is only a minute.
    """

    start: pd.Timestamp
    end: pd.Timestamp
    has_time: bool

    @classmethod
    def parse(cls, start: str, end: str, timezone: str = "Asia/Shanghai") -> BacktestTimeRange:
        def timestamp(value: str) -> tuple[pd.Timestamp, bool]:
            text = value.strip()
            date_only = re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) is not None
            if not date_only and re.match(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", text) is None:
                raise ValueError("use YYYY-MM-DD or YYYY-MM-DD HH:MM for backtest boundaries")
            result = pd.Timestamp(text)
            if pd.isna(result):
                raise ValueError("backtest boundaries must be valid timestamps")
            result = result.tz_localize(timezone) if result.tzinfo is None else result.tz_convert(timezone)
            return result, date_only

        first, start_is_date = timestamp(start)
        last, end_is_date = timestamp(end)
        if end_is_date:
            last += pd.DateOffset(days=1)
        if first >= last:
            raise ValueError("backtest start must precede end (datetime end is exclusive)")
        return cls(first, last, not (start_is_date and end_is_date))

    @property
    def start_date(self) -> date:
        return self.start.date()

    @property
    def end_date(self) -> date:
        return (self.end - pd.Timedelta(nanoseconds=1)).date()

    @property
    def start_ns(self) -> int:
        return self.start.value

    @property
    def end_ns(self) -> int:
        return self.end.value

    def contains(self, timestamp_ns: int) -> bool:
        return self.start_ns <= timestamp_ns < self.end_ns
