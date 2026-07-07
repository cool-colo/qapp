from __future__ import annotations

from datetime import date
import json
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request
from urllib.request import urlopen

from strategies.model_target_planners.base import ModelTargetCandidate
from strategies.model_target_planners.base import ModelTargetPlan
from strategies.model_target_planners.base import ModelTargetPlanner
from strategies.model_target_planners.base import ModelTargetPlanningRequest
from strategies.model_target_planners.base import normalize_stock_code


class RiskManagerModelTargetPlanner(ModelTargetPlanner):
    reason = "risk_manager_optimize"

    def __init__(
        self,
        base_url: str,
        risk_model_id: str,
        mode: str,
        timeout_secs: float = 10.0,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.risk_model_id = str(risk_model_id or "").strip()
        self.mode = str(mode or "").strip()
        self.timeout_secs = float(timeout_secs)
        if not self.base_url:
            raise ValueError("risk_manager_base_url is required for risk_manager target planner")
        if not self.risk_model_id:
            raise ValueError("risk_manager_risk_model_id is required for risk_manager target planner")
        if self.mode not in {"backtest", "simulation", "live"}:
            raise ValueError("risk_manager_mode must be one of: backtest, simulation, live")

    def plan(self, request: ModelTargetPlanningRequest) -> ModelTargetPlan:
        if not request.active_instrument_ids:
            return ModelTargetPlan(request.trading_date, request.signal_date, {}, self.reason)
        if not request.candidates:
            raise RuntimeError("risk-manager optimize requires active positions mapped to stock codes")
        request.signal_date = date.fromisoformat("2026-06-24")  #TODO Replace with actual date if needed
        response = self._post_json(self._payload(request))
        if not bool(response.get("success")):
            status = response.get("status")
            failure_reason = response.get("failure_reason")
            raise RuntimeError(
                f"risk-manager optimize failed status={status} failure_reason={failure_reason}",
            )
        return ModelTargetPlan(
            trading_date=request.trading_date,
            signal_date=request.signal_date,
            weights=self._target_weights(response, request.candidates),
            reason=self.reason,
        )

    def _payload(self, request: ModelTargetPlanningRequest) -> dict[str, Any]:
        asof_date = request.signal_date or request.trading_date
        current_weights = [
            {"stock_code": stock_code, "current_weight": weight}
            for stock_code, weight in sorted(request.current_weights.items())
        ]
        previous_position_total = sum(max(0.0, weight) for weight in request.current_weights.values())
        return {
            "request_id": self._request_id(request),
            "mode": self.mode,
            "risk_model_id": self.risk_model_id,
            "asof_date": asof_date.isoformat(),
            "trade_date": request.trading_date.isoformat(),
            "candidates": [
                {
                    "stock_code": candidate.stock_code,
                    "score": candidate.score,
                    "is_tradable": True,
                }
                for candidate in request.candidates
            ],
            "current_weights": current_weights,
            "benchmark_weights": [],
            "previous_position_total": previous_position_total,
        }

    def _request_id(self, request: ModelTargetPlanningRequest) -> str:
        signal_text = "none" if request.signal_date is None else request.signal_date.isoformat()
        return f"qapp-model-target-{request.trading_date.isoformat()}-{signal_text}-{len(request.candidates)}"

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"{self.base_url}/v1/portfolio/optimize"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            endpoint,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_secs) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"risk-manager optimize HTTP {exc.code}: {body[:500]}") from exc
        except URLError as exc:
            raise RuntimeError(f"risk-manager optimize request failed: {exc}") from exc
        try:
            loaded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("risk-manager optimize returned invalid JSON") from exc
        if not isinstance(loaded, dict):
            raise RuntimeError("risk-manager optimize returned a non-object JSON payload")
        return loaded

    def _target_weights(
        self,
        response: dict[str, Any],
        candidates: list[ModelTargetCandidate],
    ) -> dict[str, float]:
        stock_to_instrument = {
            normalize_stock_code(candidate.stock_code): candidate.instrument_id
            for candidate in candidates
        }
        weights: dict[str, float] = {}
        rows = response.get("target_weights") or []
        if not isinstance(rows, list):
            raise RuntimeError("risk-manager optimize response target_weights must be a list")
        for row in rows:
            if not isinstance(row, dict):
                continue
            stock_code = normalize_stock_code(row.get("stock_code"))
            instrument_id = stock_to_instrument.get(stock_code)
            if instrument_id is None:
                continue
            raw_weight = row.get("target_weight", row.get("weight"))
            try:
                weight = float(raw_weight)
            except (TypeError, ValueError):
                continue
            if weight > 0:
                weights[instrument_id] = weight
        return dict(sorted(weights.items()))
