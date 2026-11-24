from typing import Protocol
from decimal import Decimal
from trading_orchestration.contexts.exposure_context import ExposureContext


class OrderAdmissionPolicy(Protocol):
    def quantity(self, context: ExposureContext, instrument_id: str,
                 direction: str, requested: Decimal, *, order_price: Decimal) -> Decimal: ...

