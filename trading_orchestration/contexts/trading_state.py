from dataclasses import dataclass, field
from typing import Any
from trading_orchestration.contexts.market_state import MarketState
from trading_orchestration.contexts.execution_state import ExecutionState


@dataclass
class TradingState:
    market: MarketState = field(default_factory=MarketState)
    execution: ExecutionState = field(default_factory=ExecutionState)
    planned_models: dict[str, Any] = field(default_factory=dict)
    formal_model: Any = None

