from __future__ import annotations

from decimal import Decimal

import pytest

from nautilus_trader.adapters.bigqmt.common import parse_equity

from strategies.order_splitter import LimitOrderQuantityRules
from strategies.order_splitter import NotionalOrderSplitter


def instrument(symbol: str = "688802.SH"):
    return parse_equity(symbol=symbol, fields={}, ts_event=1, ts_init=1)


@pytest.mark.parametrize(
    ("symbol", "quantity", "price", "cap", "expected"),
    [
        ("688802.SH", "2200", "405.19", "300000", [740, 740, 720]),
        ("688802.SH", "2200", "400", "280000", [700, 700, 600, 200]),
        ("688802.SH", "800", "400", "300000", [600, 200]),
        ("688802.SH", "201", "400", "300000", [201]),
        ("688802.SH", "100", "400", "300000", []),
        ("688802.SH", "100", "400", "0", []),
        ("688802.SH", "200100", "1", "0", [100000, 99900, 200]),
        ("688802.SH", "100001", "1", "10000000", [99801, 200]),
        ("688802.SH", "300", "1", "250", [250]),
        ("688802.SH", "600", "1", "250", [200, 200, 200]),
        ("688802.SH", "2200", "400", "50000", []),
        ("000001.SZ", "95123", "10.01", "300000", [29900, 29900, 29900, 5400]),
        ("000001.SZ", "50", "1", "300000", []),
        ("300001.SZ", "600100", "1", "0", [300000, 300000, 100]),
        ("920001.BJ", "101", "1", "300000", [101]),
    ],
)
def test_splits_respect_reference_limits(symbol, quantity, price, cap, expected):
    stock = instrument(symbol)
    slices = NotionalOrderSplitter(Decimal(cap)).split(stock, Decimal(quantity), Decimal(price))

    assert slices == [Decimal(value) for value in expected]
    assert sum(slices) <= Decimal(quantity)
    rules = LimitOrderQuantityRules.from_instrument(stock)
    assert all(rules.is_valid(value) for value in slices)


@pytest.mark.parametrize(
    ("symbol", "quantity", "cap", "expected"),
    [
        ("688802.SH", "100", "300000", [100]),
        ("688802.SH", "300", "250", [250, 50]),
        ("000001.SZ", "150", "300000", [150]),
        ("000001.SZ", "1050", "1000", [900, 150]),
        ("000001.SZ", "101", "100", [100, 1]),
        ("000001.SZ", "50", "50", [50]),
    ],
)
def test_only_liquidation_may_keep_an_odd_sized_last_slice(symbol, quantity, cap, expected):
    stock = instrument(symbol)
    slices = NotionalOrderSplitter(Decimal(cap)).split(
        stock, Decimal(quantity), Decimal("1"), allow_odd_lot=True,
    )

    assert slices == [Decimal(value) for value in expected]
    assert sum(slices) == Decimal(quantity)
    rules = LimitOrderQuantityRules.from_instrument(stock)
    assert all(rules.is_valid(value) for value in slices[:-1])
    assert rules.is_valid(slices[-1], allow_odd_lot=True)


def test_missing_instrument_metadata_fails_instead_of_using_a_default_lot():
    stock = instrument()
    stock.info.pop("limit_order_quantity_rules")
    with pytest.raises(KeyError, match="limit_order_quantity_rules"):
        NotionalOrderSplitter(Decimal("300000")).split(stock, Decimal("100"), Decimal("1"))


def test_broken_reference_limits_fail_fast():
    stock = instrument()
    stock.info["limit_order_quantity_rules"]["max_quantity"] = 100
    with pytest.raises(ValueError, match="Invalid limit-order quantity rules"):
        NotionalOrderSplitter(Decimal("300000")).split(stock, Decimal("100"), Decimal("1"))


@pytest.mark.parametrize("cap", [200, 250, 400, 700])
def test_partition_conserves_total_whenever_regular_slices_are_feasible(cap):
    stock = instrument()
    splitter = NotionalOrderSplitter(Decimal(cap))
    rules = LimitOrderQuantityRules.from_instrument(stock)
    for quantity in range(1, 1201):
        slices = splitter.split(stock, Decimal(quantity), Decimal("1"))
        assert sum(slices) <= quantity
        assert all(rules.is_valid(value) and value <= cap for value in slices)
        order_count = (quantity + cap - 1) // cap
        if order_count * 200 <= quantity:
            assert sum(slices) == quantity
