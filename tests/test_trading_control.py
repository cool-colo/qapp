from __future__ import annotations

import threading
import unittest
from datetime import date
from decimal import Decimal

from strategies.trading_control import SELL_ALL_REASON
from strategies.trading_control import SELL_REASON
from strategies.trading_control import TradingController


class FakeControlHost:
    """A minimal in-memory ``TradingControlHost`` for exercising the controller.

    Records the target map handed to ``control_apply_targets`` so tests can assert
    exactly what the controller would push to convergence.
    """

    def __init__(
        self,
        targets: dict[str, Decimal],
        held: dict[str, Decimal],
        sellable: dict[str, Decimal | None],
    ) -> None:
        self.control_converge_lock = threading.Lock()
        self._targets = dict(targets)
        self._held = dict(held)
        self._sellable = dict(sellable)
        self.applied_targets: dict[str, Decimal] | None = None
        self.forced = False

    def control_set_paused_flag(self, paused: bool) -> None:
        pass

    def control_current_targets(self) -> dict[str, Decimal]:
        return dict(self._targets)

    def control_open_long_quantities(self) -> dict[str, Decimal]:
        return dict(self._held)

    def control_sellable_quantity(self, instrument_id: str) -> Decimal | None:
        return self._sellable.get(instrument_id)

    def control_clock_date(self) -> date:
        return date(2025, 1, 2)

    def control_apply_targets(self, targets, target_date, reason) -> None:
        self.applied_targets = dict(targets)

    def control_force_converge(self, current_date, trigger) -> None:
        self.forced = True

    def control_log(self, message: str) -> None:
        pass


class SellAllTests(unittest.TestCase):
    def test_sell_all_zeros_every_target_not_just_held(self) -> None:
        # A is held and fully sellable; B has a positive buy target but is NOT held
        # yet. The bug: sell-all copied the current targets, leaving B's positive
        # target live, which convergence then bought. Sell-all must target zero for
        # everything and never leave a positive (buy) target.
        host = FakeControlHost(
            targets={"A.QMT": Decimal(100), "B.QMT": Decimal(200)},
            held={"A.QMT": Decimal(100)},
            sellable={"A.QMT": Decimal(100)},
        )
        controller = TradingController(host)

        result = controller.sell(None, SELL_ALL_REASON)

        self.assertEqual(result["affected"], ["A.QMT"])
        self.assertEqual(host.applied_targets, {"A.QMT": Decimal(0)})
        self.assertNotIn("B.QMT", host.applied_targets)
        self.assertTrue(host.forced)

    def test_sell_all_keeps_non_sellable_remainder(self) -> None:
        # 100 held, only 60 sellable -> target the 40-share non-sellable remainder.
        host = FakeControlHost(
            targets={"A.QMT": Decimal(100)},
            held={"A.QMT": Decimal(100)},
            sellable={"A.QMT": Decimal(60)},
        )
        controller = TradingController(host)

        controller.sell(None, SELL_ALL_REASON)

        self.assertEqual(host.applied_targets, {"A.QMT": Decimal(40)})

    def test_per_name_sell_preserves_other_targets(self) -> None:
        # A per-name sell of A must still lower A to 0 but keep B's target intact.
        host = FakeControlHost(
            targets={"A.QMT": Decimal(100), "B.QMT": Decimal(200)},
            held={"A.QMT": Decimal(100), "B.QMT": Decimal(200)},
            sellable={"A.QMT": Decimal(100), "B.QMT": Decimal(200)},
        )
        controller = TradingController(host)

        result = controller.sell(["A.QMT"], SELL_REASON)

        self.assertEqual(result["affected"], ["A.QMT"])
        self.assertEqual(host.applied_targets, {"A.QMT": Decimal(0), "B.QMT": Decimal(200)})

    def test_sell_all_skips_unknown_sellable(self) -> None:
        # Sellable unknown -> skipped, not targeted (avoid selling an unknown qty).
        host = FakeControlHost(
            targets={"A.QMT": Decimal(100)},
            held={"A.QMT": Decimal(100)},
            sellable={"A.QMT": None},
        )
        controller = TradingController(host)

        result = controller.sell(None, SELL_ALL_REASON)

        self.assertEqual(result["affected"], [])
        self.assertEqual(result["skipped"], [{"instrument_id": "A.QMT", "reason": "sellable_unknown"}])
        # Nothing affected -> no targets applied and no convergence forced.
        self.assertIsNone(host.applied_targets)
        self.assertFalse(host.forced)


if __name__ == "__main__":
    unittest.main()
