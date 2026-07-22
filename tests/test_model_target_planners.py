from __future__ import annotations

import json
import unittest
from datetime import date
from urllib.error import URLError
from unittest.mock import patch

from strategies.model_target_planners import CurrentHolding
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
    def test_risk_manager_planner_posts_optimize_payload_and_maps_weights(self) -> None:
        request = ModelTargetPlanningRequest(
            trading_date=date(2026, 7, 2),
            signal_date=date(2026, 7, 1),
            active_instrument_ids=["000001.SZ.QMT", "000002.SZ.QMT"],
            candidates=[
                ModelTargetCandidate(
                    "000001.SZ.QMT", "000001.SZ", 0.12, open_price=10.0, expected_return=0.031,
                ),
                ModelTargetCandidate(
                    "000002.SZ.QMT", "000002.SZ", 0.08, open_price=20.0, expected_return=0.017,
                ),
            ],
            current_holdings=[
                CurrentHolding(
                    "000001.SZ.QMT",
                    "000001.SZ",
                    quantity=1000,
                    price=10.0,
                    recent_target_date=date(2026, 6, 20),
                    recent_holding_days=8,
                ),
                CurrentHolding(
                    "000004.SZ.QMT",
                    "000004.SZ",
                    quantity=500,
                    price=30.0,
                    recent_target_date=None,
                    recent_holding_days=0,
                ),
            ],
            target_cash_buffer_percent=0.05,
            max_position_percent=0.03,
            total_asset=1_000_000.0,
            investable_asset=950_000.0,
            open_prices={"000001.SZ.QMT": 10.0, "000002.SZ.QMT": 20.0},
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
                    "risk_model_id": "cn_a_basic_constraints_integer_lots",
                    "asof_date": "2026-07-01",
                    "target_weights": [
                        {"stock_code": "000002.SZ", "target_weight": 0.40, "target_quantity": 19000},
                        {"stock_code": "000001.SZ", "target_weight": 0.30, "target_quantity": 28500},
                        {"stock_code": "999999.SZ", "target_weight": 0.20, "target_quantity": 100},
                        # A liquidation row for a holding not in candidates maps back too.
                        {"stock_code": "000004.SZ", "target_weight": 0.0, "target_quantity": 0},
                    ],
                },
            )

        planner = RiskManagerModelTargetPlanner(
            base_url="http://risk-manager.local/",
            risk_model_id="cn_a_basic_constraints_integer_lots",
            mode="live",
            timeout_secs=3.5,
        )
        with patch("strategies.model_target_planners.risk_manager.urlopen", side_effect=fake_urlopen):
            plan = planner.plan(request)

        self.assertEqual(captured["url"], "http://risk-manager.local/v1/portfolio/optimize")
        self.assertEqual(captured["timeout"], 3.5)
        self.assertEqual(captured["payload"]["mode"], "live")
        self.assertEqual(captured["payload"]["risk_model_id"], "cn_a_basic_constraints_integer_lots")
        self.assertEqual(captured["payload"]["asof_date"], "2026-07-01")
        self.assertEqual(captured["payload"]["trade_date"], "2026-07-02")
        # Investable total (net of buffer) is sent so the service sizes share counts.
        self.assertAlmostEqual(captured["payload"]["total_asset"], 950_000.0)
        # Candidates come from signals only; expected_return is the model's
        # pred_return_live (not a fabricated constant).
        self.assertEqual(
            captured["payload"]["candidates"],
            [
                {"stock_code": "000001.SZ", "score": 0.12, "is_tradable": True, "expected_return": 0.031, "price": 10.0},
                {"stock_code": "000002.SZ", "score": 0.08, "is_tradable": True, "expected_return": 0.017, "price": 20.0},
            ],
        )
        # current_weights come from holdings only, with the new item schema. The internal
        # recent_target_date is serialized on the wire as recent_buy_date.
        self.assertEqual(
            captured["payload"]["current_weights"],
            [
                {
                    "stock_code": "000001.SZ",
                    "quantity": 1000,
                    "price": 10.0,
                    "recent_buy_date": "2026-06-20",
                    "recent_holding_days": 8,
                },
                {
                    "stock_code": "000004.SZ",
                    "quantity": 500,
                    "price": 30.0,
                    "recent_buy_date": None,
                    "recent_holding_days": 0,
                },
            ],
        )
        self.assertNotIn("previous_position_total", captured["payload"])
        self.assertEqual(plan.reason, "risk_manager_optimize")
        self.assertEqual(plan.signal_date, date(2026, 7, 1))
        self.assertEqual(request.signal_date, date(2026, 7, 1))
        # Weights map to instrument ids for known candidates; unknown 999999.SZ dropped.
        self.assertEqual(
            plan.weights,
            {
                "000001.SZ.QMT": 0.30,
                "000002.SZ.QMT": 0.40,
            },
        )
        # Service-provided share counts flow through verbatim, including the 0
        # (liquidation) target for a current holding not among candidates.
        self.assertEqual(
            plan.target_qty,
            {
                "000001.SZ.QMT": 28500,
                "000002.SZ.QMT": 19000,
                "000004.SZ.QMT": 0,
            },
        )

    def test_risk_manager_planner_maps_quantity_only_row(self) -> None:
        request = ModelTargetPlanningRequest(
            trading_date=date(2026, 7, 2),
            signal_date=date(2026, 7, 1),
            active_instrument_ids=["000001.SZ.QMT", "000003.SZ.QMT"],
            candidates=[
                ModelTargetCandidate(
                    "000001.SZ.QMT", "000001.SZ", 0.12, open_price=10.0, expected_return=0.02,
                ),
                ModelTargetCandidate(
                    "000003.SZ.QMT", "000003.SZ", 0.05, open_price=25.0, expected_return=0.01,
                ),
            ],
            current_holdings=[],
            target_cash_buffer_percent=0.05,
            max_position_percent=0.03,
            total_asset=1_000_000.0,
            investable_asset=1_000_000.0,
            open_prices={"000001.SZ.QMT": 10.0, "000003.SZ.QMT": 25.0},
        )

        def fake_urlopen(http_request, timeout):
            return FakeResponse(
                {
                    "success": True,
                    "status": "ok",
                    "risk_model_id": "cn_a_basic_constraints_integer_lots",
                    "asof_date": "2026-07-01",
                    "target_weights": [
                        {"stock_code": "000001.SZ", "target_weight": 0.30, "target_quantity": 30000},
                        # Quantity-only row (no weight): kept for target_qty, no weight entry.
                        {"stock_code": "000003.SZ", "target_quantity": 4000},
                    ],
                },
            )

        planner = RiskManagerModelTargetPlanner(
            base_url="http://risk-manager.local",
            risk_model_id="cn_a_basic_constraints_integer_lots",
            mode="live",
        )
        with patch("strategies.model_target_planners.risk_manager.urlopen", side_effect=fake_urlopen):
            plan = planner.plan(request)

        self.assertEqual(plan.target_qty, {"000001.SZ.QMT": 30000, "000003.SZ.QMT": 4000})
        self.assertAlmostEqual(plan.weights["000001.SZ.QMT"], 0.30)
        # Quantity-only row has no positive weight entry (nothing synthesized).
        self.assertNotIn("000003.SZ.QMT", plan.weights)

    def test_risk_manager_planner_returns_empty_plan_when_nothing_to_do(self) -> None:
        request = ModelTargetPlanningRequest(
            trading_date=date(2026, 7, 2),
            signal_date=date(2026, 7, 1),
            active_instrument_ids=[],
            candidates=[],
            current_holdings=[],
            target_cash_buffer_percent=0.05,
            max_position_percent=0.03,
        )
        planner = RiskManagerModelTargetPlanner(
            base_url="http://risk-manager.local",
            risk_model_id="cn_a_basic_constraints_integer_lots",
            mode="simulation",
        )

        with patch("strategies.model_target_planners.risk_manager.urlopen") as urlopen_mock:
            plan = planner.plan(request)

        urlopen_mock.assert_not_called()
        self.assertEqual(plan.target_qty, {})
        self.assertEqual(plan.weights, {})

    def test_risk_manager_planner_raises_on_service_failure(self) -> None:
        request = ModelTargetPlanningRequest(
            trading_date=date(2026, 7, 2),
            signal_date=date(2026, 7, 1),
            active_instrument_ids=["000001.SZ.QMT"],
            candidates=[
                ModelTargetCandidate(
                    "000001.SZ.QMT", "000001.SZ", 0.12, open_price=10.0, expected_return=0.02,
                ),
            ],
            current_holdings=[],
            target_cash_buffer_percent=0.05,
            max_position_percent=0.03,
        )
        planner = RiskManagerModelTargetPlanner(
            base_url="http://risk-manager.local",
            risk_model_id="cn_a_basic_constraints_integer_lots",
            mode="simulation",
        )

        with patch(
            "strategies.model_target_planners.risk_manager.urlopen",
            return_value=FakeResponse({"success": False, "status": "failed", "failure_reason": "no_risk_data"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "no_risk_data"):
                planner.plan(request)

    def test_risk_manager_post_json_retries_transient_failures(self) -> None:
        planner = RiskManagerModelTargetPlanner(
            base_url="http://risk-manager.local",
            risk_model_id="cn_a_basic_constraints_integer_lots",
            mode="simulation",
        )
        responses = [
            URLError("temporary failure 1"),
            URLError("temporary failure 2"),
            FakeResponse({"success": True, "target_weights": []}),
        ]

        def fake_urlopen(_http_request, timeout):
            del timeout
            result = responses.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result

        with (
            patch("strategies.model_target_planners.risk_manager.urlopen", side_effect=fake_urlopen) as urlopen_mock,
            patch("strategies.model_target_planners.risk_manager.time.sleep") as sleep_mock,
            self.assertLogs("strategies.model_target_planners.risk_manager", level="WARNING") as logs,
        ):
            payload = planner._post_json({"request_id": "retry-test"})

        self.assertEqual(payload, {"success": True, "target_weights": []})
        self.assertEqual(urlopen_mock.call_count, 3)
        sleep_mock.assert_any_call(1)
        sleep_mock.assert_any_call(2)
        self.assertEqual(sleep_mock.call_count, 2)
        self.assertEqual(len(logs.records), 2)
        self.assertEqual([record.levelname for record in logs.records], ["WARNING", "WARNING"])

    def test_risk_manager_post_json_logs_error_after_retry_exhaustion(self) -> None:
        planner = RiskManagerModelTargetPlanner(
            base_url="http://risk-manager.local",
            risk_model_id="cn_a_basic_constraints_integer_lots",
            mode="simulation",
        )

        with (
            patch(
                "strategies.model_target_planners.risk_manager.urlopen",
                side_effect=URLError("risk manager unavailable"),
            ) as urlopen_mock,
            patch("strategies.model_target_planners.risk_manager.time.sleep") as sleep_mock,
            self.assertLogs("strategies.model_target_planners.risk_manager", level="ERROR") as logs,
        ):
            with self.assertRaisesRegex(RuntimeError, "risk-manager optimize request failed"):
                planner._post_json({"request_id": "retry-exhausted"})

        self.assertEqual(urlopen_mock.call_count, 6)
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [1, 2, 3, 4, 5])
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelname, "ERROR")


if __name__ == "__main__":
    unittest.main()
