from dataclasses import dataclass
from trading_orchestration.components.splitters.order_splitter import OrderSplitter
from trading_orchestration.components.cancellations.cancellation_policy import CancellationPolicy


@dataclass(frozen=True)
class ExecutionProfile:
    splitter: OrderSplitter
    cancellation: CancellationPolicy
