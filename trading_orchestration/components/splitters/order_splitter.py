from decimal import Decimal
from typing import Protocol, TypeVar

InstrumentT = TypeVar("InstrumentT", contravariant=True)


class OrderSplitter(Protocol[InstrumentT]):
    def split(self, instrument: InstrumentT, quantity: Decimal, price: Decimal,
              *, allow_odd_lot: bool = False) -> list[Decimal]: ...
