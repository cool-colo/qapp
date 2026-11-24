from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class FormalTarget:
    version: str
    trading_date: str
    quantities: Mapping[str, Decimal]
    weights: Mapping[str, Decimal]
    buffer_amount: Decimal
    planning_assets: Decimal
    ranks: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "quantities", MappingProxyType(dict(self.quantities)))
        object.__setattr__(self, "weights", MappingProxyType(dict(self.weights)))
        object.__setattr__(self, "ranks", MappingProxyType(dict(self.ranks)))
        if not self.version or not self.buffer_amount.is_finite() or self.buffer_amount < 0:
            raise ValueError("formal target needs a version and non-negative fixed buffer")
        if not self.planning_assets.is_finite() or self.planning_assets <= 0:
            raise ValueError("formal target needs positive planning assets")
        if any(not q.is_finite() or q < 0 for q in self.quantities.values()):
            raise ValueError("invalid target quantity")
        if any(not w.is_finite() or w < 0 for w in self.weights.values()):
            raise ValueError("invalid target weight")
        if any(q > 0 and key not in self.weights for key, q in self.quantities.items()):
            raise ValueError("positive target missing weight")

    @property
    def ratio(self) -> Decimal:
        return sum(self.weights.values(), Decimal(0))


@dataclass(frozen=True)
class InstrumentSnapshot:
    quantity: Decimal
    price: Decimal
    sellable: Decimal
    increment: Decimal = Decimal(100)
    minimum: Decimal = Decimal(100)
    can_buy: bool = True
    can_sell: bool = True
    block_reason: str = ""
    minimum_buy_notional: Decimal = Decimal(0)
    allow_odd_lot_sell: bool = False
    quality_error: str = ""


@dataclass(frozen=True)
class PendingExposureOrder:
    instrument_id: str
    direction: str
    quantity: Decimal


@dataclass(frozen=True)
class HoldingSnapshot:
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True)
class ExposurePositionContext:
    """Facts needed to assess total exposure, without order-planning inputs."""

    now_ns: int
    trading_date: str
    execution_revision: int
    total_assets: Decimal
    positions: Mapping[str, HoldingSnapshot]
    target: FormalTarget | None
    pending_orders: int = 0
    quality_error: str = ""
    pending: tuple[PendingExposureOrder, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "positions", MappingProxyType(dict(self.positions)))


@dataclass(frozen=True)
class ExposureContext:
    """Fresh valuation and candidate inputs for planning or physical admission."""
    now_ns: int
    trading_date: str
    execution_revision: int
    total_assets: Decimal
    free_cash: Decimal
    instruments: Mapping[str, InstrumentSnapshot]
    target: FormalTarget | None
    pending_orders: int = 0
    quality_error: str = ""
    pending: tuple[PendingExposureOrder, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "instruments", MappingProxyType(dict(self.instruments)))

    def position_snapshot(self) -> ExposurePositionContext:
        return ExposurePositionContext(
            self.now_ns, self.trading_date, self.execution_revision, self.total_assets,
            {iid: HoldingSnapshot(item.quantity, item.price)
             for iid, item in self.instruments.items() if item.quantity != 0},
            self.target, self.pending_orders, self.quality_error, self.pending)


@dataclass(frozen=True)
class ExposureAssessment:
    status: str
    reason: str
    target_ratio: Decimal | None = None
    actual_ratio: Decimal | None = None
    deviation: Decimal | None = None
    denominator: Decimal | None = None
    market_value: Decimal | None = None
    direction: str | None = None
    minimum_notional: Decimal = Decimal(0)
    center_notional: Decimal = Decimal(0)
    maximum_notional: Decimal = Decimal(0)

    def evidence(self) -> dict:
        return {name: str(value) for name, value in vars(self).items() if value is not None}


@dataclass(frozen=True)
class ExposurePlan:
    status: str
    reason: str
    parent_version: str
    execution_revision: int
    direction: str | None = None
    quantities: Mapping[str, Decimal] = field(default_factory=dict)
    notional: Decimal = Decimal(0)
    required_notional: Decimal = Decimal(0)
    available_notional: Decimal = Decimal(0)
    excluded: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "quantities", MappingProxyType(dict(self.quantities)))
        object.__setattr__(self, "excluded", MappingProxyType(dict(self.excluded)))

