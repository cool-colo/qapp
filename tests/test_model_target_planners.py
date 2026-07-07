from __future__ import annotations

import json
import unittest
from datetime import date
from unittest.mock import patch

from strategies.model_target_planners import EqualWeightModelTargetPlanner
from strategies.model_target_planners import ModelTargetCandidate
from strategies.model_target_planners import ModelTargetPlanningRequest
from strategies.model_target_planners import RiskManagerModelTargetPlanner


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.body).encode("utf-8")


class ModelTargetPlannerTest(unittest.TestCase):
    def test_equal_weight_planner_preserves_existing_weight_caps(self) -> None:
        request = ModelTargetPlanningRequest(
            trading_date=date(2026, 7, 2),
            signal_date=date(2026, 7, 1),
            active_instrument_ids=["000001.SZ.QMT", "000002.SZ.QMT"],
            candidates=[],
            current_weights={},
            target_cash_buffer_percent=0.05,
            max_position_percent=0.03,
        )

        plan = EqualWeightModelTargetPlanner().plan(request)

        self.assertEqual(plan.reason, "model_prediction_score")
        self.assertEqual(
            plan.weights,
            {
                "000001.SZ.QMT": 0.03,
                "000002.SZ.QMT": 0.03,
            },
        )

    def test_risk_manager_planner_posts_optimize_payload_and_maps_weights(self) -> None:
        request = ModelTargetPlanningRequest(
            trading_date=date(2026, 7, 2),
            signal_date=date(2026, 7, 1),
            active_instrument_ids=["000001.SZ.QMT", "000002.SZ.QMT"],
            candidates=[
                ModelTargetCandidate("000001.SZ.QMT", "000001.SZ", 0.12),
                ModelTargetCandidate("000002.SZ.QMT", "000002.SZ", 0.08),
            ],
            current_weights={"000001.SZ": 0.10, "000002.SZ": 0.20},
            target_cash_buffer_percent=0.05,
            max_position_percent=0.03,
        )
        captured = {}

        def fake_urlopen(http_request, timeout):
            captured["url"] = http_request.full_url
            captured["timeout"] = timeout
            captured["payload"] = json.loads(http_request.data.decode("utf-8"))
            return FakeResponse(
                {
                    "success": True,
                    "status": "ok",
                    "risk_model_id": "cn_a_mean_variance",
                    "asof_date": "2026-07-01",
                    "target_weights": [
                        {"stock_code": "000002.SZ", "target_weight": 0.40},
                        {"stock_code": "000001.SZ", "target_weight": 0.30},
                        {"stock_code": "999999.SZ", "target_weight": 0.20},
                        {"stock_code": "000003.SZ", "target_weight": 0.0},
                    ],
                },
            )

        planner = RiskManagerModelTargetPlanner(
            base_url="http://risk-manager.local/",
            risk_model_id="cn_a_mean_variance",
            mode="live",
            timeout_secs=3.5,
        )
        with patch("strategies.model_target_planners.risk_manager.urlopen", side_effect=fake_urlopen):
            plan = planner.plan(request)

        self.assertEqual(captured["url"], "http://risk-manager.local/v1/portfolio/optimize")
        self.assertEqual(captured["timeout"], 3.5)
        self.assertEqual(captured["payload"]["mode"], "live")
        self.assertEqual(captured["payload"]["risk_model_id"], "cn_a_mean_variance")
        self.assertEqual(captured["payload"]["asof_date"], "2026-07-01")
        self.assertEqual(captured["payload"]["trade_date"], "2026-07-02")
        self.assertEqual(
            captured["payload"]["candidates"],
            [
                {"stock_code": "000001.SZ", "score": 0.12, "is_tradable": True},
                {"stock_code": "000002.SZ", "score": 0.08, "is_tradable": True},
            ],
        )
        self.assertEqual(
            captured["payload"]["current_weights"],
            [
                {"stock_code": "000001.SZ", "current_weight": 0.10},
                {"stock_code": "000002.SZ", "current_weight": 0.20},
            ],
        )
        self.assertAlmostEqual(captured["payload"]["previous_position_total"], 0.30)
        self.assertEqual(plan.reason, "risk_manager_optimize")
        self.assertEqual(
            plan.weights,
            {
                "000001.SZ.QMT": 0.30,
                "000002.SZ.QMT": 0.40,
            },
        )

    def test_risk_manager_planner_raises_on_service_failure(self) -> None:
        request = ModelTargetPlanningRequest(
            trading_date=date(2026, 7, 2),
            signal_date=date(2026, 7, 1),
            active_instrument_ids=["000001.SZ.QMT"],
            candidates=[ModelTargetCandidate("000001.SZ.QMT", "000001.SZ", 0.12)],
            current_weights={},
            target_cash_buffer_percent=0.05,
            max_position_percent=0.03,
        )
        planner = RiskManagerModelTargetPlanner(
            base_url="http://risk-manager.local",
            risk_model_id="cn_a_mean_variance",
            mode="simulation",
        )

        with patch(
            "strategies.model_target_planners.risk_manager.urlopen",
            return_value=FakeResponse({"success": False, "status": "failed", "failure_reason": "no_risk_data"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "no_risk_data"):
                planner.plan(request)


if __name__ == "__main__":
    unittest.main()
