from dataclasses import dataclass, field
from datetime import date


@dataclass
class AuctionSessionState:
    frozen_dates: set[date] = field(default_factory=set)
    failed_dates: dict[date, str] = field(default_factory=dict)
    execution_ready_dates: set[date] = field(default_factory=set)
    ranks: dict[str, int] = field(default_factory=dict)

    def fail(self, day, reason):
        self.failed_dates[day] = reason
