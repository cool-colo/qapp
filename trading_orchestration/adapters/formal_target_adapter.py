from __future__ import annotations

from decimal import Decimal
from trading_orchestration.contexts.exposure_context import FormalTarget


def formal_target_from_plan(plan, version: str) -> FormalTarget:
    if plan.total_asset is None or plan.investable_asset is None:
        raise ValueError("formal target has no planning asset basis")
    total, basis = Decimal(str(plan.total_asset)), Decimal(str(plan.investable_asset))
    buffer = max(Decimal(0), total - basis)
    denominator = total - buffer
    if denominator <= 0 or basis <= 0:
        raise ValueError("invalid planning asset basis")
    # Some existing account policies deliberately size above raw assets. Convert
    # their weights explicitly to the same denominator used by the exposure task.
    factor = basis / denominator
    quantities, weights, ranks = {}, {}, {}
    for row in plan.targets:
        if row.instrument_id in quantities or row.quantity is None:
            raise ValueError("duplicate or missing target quantity")
        quantities[row.instrument_id] = Decimal(str(row.quantity))
        if row.weight is None:
            if row.quantity != 0:
                raise ValueError(f"missing target weight: {row.instrument_id}")
            weights[row.instrument_id] = Decimal(0)
        else:
            weights[row.instrument_id] = Decimal(str(row.weight)) * factor
        if row.target_context.prediction_rank is not None:
            ranks[row.instrument_id] = row.target_context.prediction_rank
    return FormalTarget(version, plan.trading_date.isoformat(), quantities, weights,
                        buffer, denominator, ranks)

