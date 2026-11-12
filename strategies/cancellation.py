from __future__ import annotations

from datetime import time
from functools import lru_cache


# The opening auction cannot be canceled by this strategy before 09:30;
# the closing auction becomes non-cancelable at 14:57:00.
A_SHARE_CANCEL_WINDOWS = "09:30-11:30,13:00-14:57"


@lru_cache(maxsize=128)
def parse_cancel_windows(windows: str) -> tuple[tuple[time, time], ...]:
    """Parse ordered, nonoverlapping [start, end) windows with second precision."""
    if not isinstance(windows, str):
        raise ValueError("cancel windows must be a string")
    if not windows.strip():
        return ()
    result = []
    for session in windows.split(","):
        try:
            start_text, end_text = session.strip().split("-")
            start, end = time.fromisoformat(start_text.strip()), time.fromisoformat(end_text.strip())
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid cancel window: {session!r}") from exc
        if (start.tzinfo is not None or end.tzinfo is not None or start >= end
                or (result and start < result[-1][1])):
            raise ValueError(f"Invalid or overlapping cancel window: {session!r}")
        result.append((start, end))
    return tuple(result)


def cancel_window_index(current: time, windows: str) -> int | None:
    for index, (start, end) in enumerate(parse_cancel_windows(windows)):
        if start <= current < end:
            return index
    return None


def validate_cancel_config(config) -> None:
    allowed = parse_cancel_windows(A_SHARE_CANCEL_WINDOWS)
    for name, value in (("cancel_windows", config.cancel_windows),
                        ("exchange_cancel_windows", config.exchange_cancel_windows)):
        try:
            if not isinstance(value, str):
                raise ValueError("must be a string")
            windows = parse_cancel_windows(value)
            if any(not any(lower <= start < end <= upper for lower, upper in allowed)
                   for start, end in windows):
                raise ValueError(f"must stay within {A_SHARE_CANCEL_WINDOWS}")
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from exc
