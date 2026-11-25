from __future__ import annotations

import json
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, PropertyMock, patch

from nautilus_trader.model.identifiers import AccountId, InstrumentId, Venue

from strategies.model_target_planners import ModelTargetPlanningContext
from strategies.model_target_planners import RiskManagerModelTargetPlanner
from tests.test_opening_auction import replay_fixture


class RiskManagerAccountIdTest(unittest.TestCase):
    def test_replay_posts_nautilus_account_number(self):
        for continuous in (False, True):
            with self.subTest(continuous=continuous):
                engine, strategy, _, _ = replay_fixture(
                    auction_enabled=not continuous, continuous=continuous,
                )
                strategy._target_planner = RiskManagerModelTargetPlanner(
                    "http://risk-manager.test", "cn_a_basic_constraints_integer_lots",
                    "test", mode="backtest", log=strategy.log,
                )
                response = MagicMock()
                response.__enter__.return_value.read.return_value = (
                    b'{"success": true, "target_weights": []}'
                )
                try:
                    with patch("strategies.model_target_planners.risk_manager.urlopen", return_value=response) as http:
                        engine.run()
                    http.assert_called_once()
                    payload = json.loads(http.call_args.args[0].data)
                    self.assertEqual(payload["account_id"], engine.cache.accounts()[0].id.get_id())
                    self.assertIsInstance(payload["account_id"], str)
                finally:
                    engine.dispose()

    def test_planning_uses_selected_venue_account(self):
        engine, strategy, _, _ = replay_fixture()
        self.addCleanup(engine.dispose)
        for venue in ("QMT", "BIGQMT"):
            with self.subTest(venue=venue):
                strategy._instrument_ids = [InstrumentId.from_str(f"000001.SZ.{venue}")]
                portfolio = MagicMock()
                portfolio.net_position.return_value = Decimal("0")
                portfolio.account.return_value.id = AccountId(f"{venue}-12345678")
                context = ModelTargetPlanningContext(date(2026, 7, 8), "opening_auction", {})
                with (
                    patch.object(type(strategy), "portfolio", new_callable=PropertyMock, return_value=portfolio),
                    patch.object(strategy, "_portfolio_value", return_value=Decimal("200000")),
                    patch.object(strategy, "_account_id_equals", return_value=False),
                ):
                    request, _ = strategy._target_planning_request(context.trading_date, None, planning_context=context)
                portfolio.account.assert_called_once_with(venue=Venue(venue))
                self.assertEqual(request.account_id, "12345678")

    def test_planning_requires_nautilus_account(self):
        engine, strategy, _, _ = replay_fixture()
        self.addCleanup(engine.dispose)
        portfolio = MagicMock()
        portfolio.net_position.return_value = Decimal("0")
        portfolio.account.return_value = None
        context = ModelTargetPlanningContext(date(2026, 7, 8), "opening_auction", {})
        with patch.object(type(strategy), "portfolio", new_callable=PropertyMock, return_value=portfolio):
            with self.assertRaisesRegex(ValueError, "Planning requires a Nautilus account"):
                strategy._target_planning_request(context.trading_date, None, planning_context=context)
