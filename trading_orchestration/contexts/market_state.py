from dataclasses import dataclass, field
from typing import Any


@dataclass
class MarketState:
    """Single-writer market projections; consumers receive immutable snapshots."""

    market_status: dict[str, Any] = field(default_factory=dict)
    whole_market_status: dict[str, Any] = field(default_factory=dict)
    depth_books: dict[str, Any] = field(default_factory=dict)
    today_open: dict[str, float] = field(default_factory=dict)
    authoritative_open: set[str] = field(default_factory=set)
    last_close: dict[str, float] = field(default_factory=dict)

