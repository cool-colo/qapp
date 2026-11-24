from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
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
    daily_window: str | None = None

    @classmethod
    def parse(cls, start: str, end: str, timezone: str = "Asia/Shanghai", *,
              daily_window: str | None = None) -> BacktestTimeRange:
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
        if daily_window is not None:
            if re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?-\d{2}:\d{2}(?::\d{2})?", daily_window) is None:
                raise ValueError("daily window must be HH:MM-HH:MM or HH:MM:SS-HH:MM:SS")
            lower, upper = map(time.fromisoformat, daily_window.split('-'))
            if lower >= upper:
                raise ValueError("daily window start must precede end; overnight windows are not supported")
        result = cls(first, last, not (start_is_date and end_is_date), daily_window)
        if daily_window is not None and not result.sessions:
            raise ValueError("daily window does not overlap the backtest date/time range")
        return result

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
        if not self.start_ns <= timestamp_ns < self.end_ns:
            return False
        if self.daily_window is None:
            return True
        local = pd.Timestamp(timestamp_ns, unit="ns", tz="UTC").tz_convert(self.start.tz)
        lower, upper = map(time.fromisoformat, self.daily_window.split('-'))
        return lower <= local.time() < upper

    def bounds_for_date(self, day: date) -> tuple[pd.Timestamp, pd.Timestamp]:
        midnight = pd.Timestamp(day).tz_localize(self.start.tz)
        if self.daily_window is None:
            lower, upper = midnight, midnight + pd.DateOffset(days=1)
        else:
            first, last = self.daily_window.split('-')
            lower = pd.Timestamp(f"{day.isoformat()} {first}").tz_localize(self.start.tz)
            upper = pd.Timestamp(f"{day.isoformat()} {last}").tz_localize(self.start.tz)
        return max(lower, self.start), min(upper, self.end)

    @property
    def sessions(self) -> tuple[tuple[pd.Timestamp, pd.Timestamp], ...]:
        return tuple((lower, upper) for day in pd.date_range(self.start_date, self.end_date)
                     for lower, upper in (self.bounds_for_date(day.date()),) if lower < upper)
