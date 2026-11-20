from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ExposureConfig:
    enabled: bool = False
    tolerance: Decimal = Decimal("0.05")
    windows: str = "09:30-11:30,13:00-14:56"
    interval_secs: float = 1.0
    priority: int = 300
    commission_rate: Decimal = Decimal("0.000085")
    minimum_fee: Decimal = Decimal("5")
    quote_max_age_secs: float = 4.0

    def __post_init__(self):
        import math
        if not isinstance(self.enabled, bool):
            raise ValueError("exposure enabled must be boolean")
        if not self.tolerance.is_finite() or not 0 < self.tolerance < 1:
            raise ValueError("exposure tolerance must be in (0, 1)")
        if not math.isfinite(self.interval_secs) or self.interval_secs <= 0:
            raise ValueError("exposure interval must be finite and positive")
        if not math.isfinite(self.quote_max_age_secs) or self.quote_max_age_secs <= 0:
            raise ValueError("quote max age must be finite and positive")
        if not self.commission_rate.is_finite() or not 0 <= self.commission_rate < 1:
            raise ValueError("invalid commission rate")
        if not self.minimum_fee.is_finite() or self.minimum_fee < 0:
            raise ValueError("invalid minimum fee")

