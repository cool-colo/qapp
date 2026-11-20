from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class ExecutionState:
    """Target-local progress; order facts and daily budgets have separate lifetimes."""

    frozen_instruments: dict[str, str] = field(default_factory=dict)
    deferred_buys: dict[str, Decimal] = field(default_factory=dict)
    insufficient_funds: set[str] = field(default_factory=set)
    pending_replacements: dict[str, Any] = field(default_factory=dict)
    order_quote_states: dict[str, Any] = field(default_factory=dict)
    target_quantities: dict[str, Decimal] = field(default_factory=dict)

