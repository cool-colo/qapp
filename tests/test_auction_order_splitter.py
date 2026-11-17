from __future__ import annotations

import unittest
from decimal import Decimal

from nautilus_trader.adapters.qmt.common import parse_equity
from nautilus_trader.model.enums import OrderSide

from strategies.order_splitter import AuctionOrderSplitter, NotionalOrderSplitter


class AuctionOrderSplitterTest(unittest.TestCase):
    def setUp(self):
        self.instrument = parse_equity("000001.SZ", {}, 0, 0)
        self.splitter = AuctionOrderSplitter(Decimal("0.000085"), Decimal(5))

    def test_full_small_target_is_allowed_including_minimum_commission(self):
        result = self.splitter.allocate(self.instrument, Decimal(1000), Decimal(10), Decimal(10005))
        self.assertEqual(result.quantities, (Decimal(1000),))
        self.assertEqual(result.commission, Decimal(5))
        self.assertEqual(result.budget_used, Decimal(10005))
        result = self.splitter.allocate(self.instrument, Decimal(1000), Decimal(10), Decimal(10000))
        self.assertEqual(result.reason, "partial_target")
        self.assertEqual(result.quantities, (Decimal(900),))

    def test_full_target_ignores_continuous_notional_cap(self):
        full = self.splitter.allocate(self.instrument, Decimal(40000), Decimal(10), Decimal(400034))
        self.assertEqual(full.quantities, (Decimal(40000),))
        regular = NotionalOrderSplitter(Decimal(300000)).split(
            self.instrument, Decimal(40000), Decimal(10))
        self.assertEqual(regular, [Decimal(30000), Decimal(10000)])

    def test_affordable_partial_has_no_58000_threshold(self):
        at_threshold = self.splitter.allocate(self.instrument, Decimal(20000), Decimal(10), Decimal(58005))
        self.assertEqual(at_threshold.quantities, (Decimal(5800),))
        above_threshold = self.splitter.allocate(self.instrument, Decimal(20000), Decimal(10), Decimal(60000))
        self.assertEqual(above_threshold.quantities, (Decimal(5900),))
        self.assertEqual(above_threshold.commission, Decimal("5.02"))
        self.assertLessEqual(above_threshold.budget_used, Decimal(60000))
        self.assertGreater(above_threshold.notional, Decimal(58000))

    def test_partial_ignores_the_remaining_target_amount(self):
        result = self.splitter.allocate(self.instrument, Decimal(10000), Decimal(10), Decimal(60000))
        self.assertEqual(result.quantities, (Decimal(5900),))
        self.assertEqual(result.reason, "partial_target")

    def test_remaining_target_at_threshold_is_allowed(self):
        result = self.splitter.allocate(self.instrument, Decimal(11700), Decimal(10), Decimal(60000))
        self.assertEqual(result.quantities, (Decimal(5900),))
        self.assertEqual(Decimal(117000) - result.notional, Decimal(58000))

    def test_top_rank_gets_the_largest_affordable_partial_first(self):
        ranks = {"large": 1, "small": 2, "last": 3}
        quantities = {"large": Decimal(10000), "small": Decimal(2000), "last": Decimal(1000)}
        remaining = Decimal(40000)
        submitted = []
        for key in self.splitter.ranked_ids(ranks, OrderSide.BUY):
            result = self.splitter.allocate(self.instrument, quantities[key], Decimal(10), remaining)
            if result.quantities:
                submitted.append(key)
                remaining -= result.budget_used
        self.assertEqual(submitted, ["large"])
        self.assertEqual(remaining, Decimal(995))

    def test_rank_order_reverses_for_sells_and_unranked_holdings_sell_first(self):
        ranks = {"a": 3, "b": 1, "c": 2, "d": None}
        self.assertEqual(self.splitter.ranked_ids(ranks, OrderSide.BUY), ["b", "c", "a"])
        self.assertEqual(self.splitter.ranked_ids(ranks, OrderSide.SELL), ["d", "a", "c", "b"])
        remaining = Decimal(40000)
        quantities = {"a": Decimal(2000), "b": Decimal(1000), "c": Decimal(1000), "d": Decimal(10000)}
        submitted = []
        for key in self.splitter.ranked_ids(ranks, OrderSide.SELL):
            result = self.splitter.allocate(self.instrument, quantities[key], Decimal(10), remaining)
            if result.quantities:
                submitted.append(key)
                remaining -= result.budget_used
        self.assertEqual(submitted, ["d"])
        self.assertEqual(remaining, Decimal(995))

    def test_exchange_quantity_limits_reserve_minimum_fee_for_every_order(self):
        self.instrument.info["limit_order_quantity_rules"]["max_quantity"] = 1000
        result = self.splitter.allocate(self.instrument, Decimal(2500), Decimal(10), Decimal(25015))
        self.assertEqual(result.quantities, (Decimal(1000), Decimal(1000), Decimal(500)))
        self.assertEqual(result.commission, Decimal(15))
        self.assertEqual(self.splitter.allocate(self.instrument, Decimal(2500), Decimal(10),
                                              Decimal(25010)).quantities, (Decimal(1000), Decimal(1000), Decimal(400)))

    def test_odd_lot_liquidation_is_allowed_only_for_complete_sellable_target(self):
        result = self.splitter.allocate(self.instrument, Decimal(255), Decimal(10), Decimal(2555),
                                       allow_odd_lot=True)
        self.assertEqual(result.quantities, (Decimal(255),))
        splitter = AuctionOrderSplitter(Decimal("0.000085"), Decimal(5))
        partial = splitter.allocate(self.instrument, Decimal(255), Decimal(10), Decimal(2500),
                                    allow_odd_lot=True)
        self.assertEqual(partial.quantities, (Decimal(200),))

    def test_invalid_fee_fails_fast(self):
        for rate, fee in [("nan", 5), (1, 5), (.000085, -5)]:
            with self.assertRaises(ValueError):
                AuctionOrderSplitter(Decimal(str(rate)), Decimal(fee))

    def test_buy_partial_must_exceed_10000_after_fees_and_lot_rounding(self):
        for budget, expected in [(Decimal(10000), ()), (Decimal(10005), ()),
                                 (Decimal(11005), (Decimal(1100),))]:
            with self.subTest(budget=budget):
                result = self.splitter.allocate(self.instrument, Decimal(3000), Decimal(10), budget,
                                                min_order_notional=Decimal(10000))
                self.assertEqual(result.quantities, expected)
                self.assertLessEqual(result.budget_used, budget)
                if not expected:
                    self.assertEqual(result.reason, "auction_buy_below_min_notional")

    def test_whole_small_target_is_allowed_but_its_partial_is_not(self):
        for quantity in (Decimal(500), Decimal(1000)):
            full = self.splitter.allocate(self.instrument, quantity, Decimal(10), quantity * 10 + 5,
                                         min_order_notional=Decimal(10000))
            self.assertEqual(full.quantities, (quantity,))
            partial = self.splitter.allocate(self.instrument, quantity, Decimal(10), quantity * 10,
                                            min_order_notional=Decimal(10000))
            self.assertEqual(partial.quantities, ())

    def test_small_exchange_forced_buy_tail_is_omitted(self):
        self.instrument.info["limit_order_quantity_rules"]["max_quantity"] = 2000
        result = self.splitter.allocate(self.instrument, Decimal(2500), Decimal(10), Decimal(25010),
                                        min_order_notional=Decimal(10000))
        self.assertEqual(result.quantities, (Decimal(2000),))
        self.assertEqual(result.budget_used, Decimal(20005))
        self.assertEqual(result.reason, "partial_target")

    def test_unaligned_target_can_allocate_its_legal_part(self):
        result = self.splitter.allocate(self.instrument, Decimal(2250), Decimal(10), Decimal(30000),
                                        min_order_notional=Decimal(10000))
        self.assertEqual(result.quantities, (Decimal(2200),))

    def test_invalid_minimum_fails_fast(self):
        for minimum in (Decimal(-1), Decimal("NaN"), Decimal("Infinity")):
            with self.assertRaises(ValueError):
                self.splitter.allocate(self.instrument, Decimal(1000), Decimal(10), Decimal(20000),
                                        min_order_notional=minimum)
