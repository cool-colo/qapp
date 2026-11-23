from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from strategies.convergence import ConvergenceTrigger


@dataclass(frozen=True)
class ManualTarget:
    quantities: tuple[tuple[str, Decimal], ...]
    trading_date: date
    reason: str
    version: str
    trigger: ConvergenceTrigger
