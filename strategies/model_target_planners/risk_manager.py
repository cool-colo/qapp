from __future__ import annotations

import json
import logging
import time
from typing import Any
from uuid import uuid4
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request
from urllib.request import urlopen

from strategies.model_target_planners.base import ModelTargetCandidate
from strategies.model_target_planners.base import ModelTargetPlan
from strategies.model_target_planners.base import ModelTargetPlanner
from strategies.model_target_planners.base import ModelTargetPlanningRequest
from strategies.model_target_planners.base import normalize_stock_code


_LOGGER = logging.getLogger(__name__)


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
        request_id = self._request_id(request)
        response = self._post_json(self._payload(request, request_id))
        if not bool(response.get("success")):
            status = response.get("status")
            failure_reason = response.get("failure_reason")
            raise RuntimeError(
                f"risk-manager optimize failed status={status} failure_reason={failure_reason}",
            )
        target_qty = self._target_quantities(response, request.candidates)
        weights = self._audit_weights(response, request.candidates)
        return ModelTargetPlan(
            trading_date=request.trading_date,
            signal_date=request.signal_date,
            weights=weights,
            reason=self.reason,
            request_id=request_id,
            target_qty=target_qty,
        )

    def _payload(self, request: ModelTargetPlanningRequest, request_id: str) -> dict[str, Any]:
        asof_date = request.signal_date or request.trading_date
        current_weights = [
            {"stock_code": stock_code, "current_weight": weight}
            for stock_code, weight in sorted(request.current_weights.items())
        ]
        previous_position_total = sum(max(0.0, weight) for weight in request.current_weights.values())
        payload: dict[str, Any] = {
            "request_id": request_id,
            "mode": self.mode,
            "risk_model_id": self.risk_model_id,
        #    "asof_date": asof_date.isoformat(),
            "asof_date": "2026-06-24",
            "trade_date": request.trading_date.isoformat(),
            "candidates": [self._candidate_payload(candidate) for candidate in request.candidates],
            "current_weights": current_weights,
            "benchmark_weights": [],
            "previous_position_total": previous_position_total,
        }
        # Pre-market investable total (net of trading buffer) so the service sizes share
        # counts server-side from candidate open prices.
        investable = request.investable_asset if request.investable_asset is not None else request.total_asset
        if investable is not None and float(investable) > 0:
            payload["total_asset"] = float(investable)
        return payload

    @staticmethod
    def _candidate_payload(candidate: ModelTargetCandidate) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stock_code": candidate.stock_code,
            "score": candidate.score,
            "is_tradable": True,
            "expected_return": 0.02,
        }
        # Open price lets the service compute target_quantity; omit when unknown so the
        # service simply skips the share-count for that candidate.
        if candidate.open_price is not None and float(candidate.open_price) > 0:
            payload["price"] = float(candidate.open_price)
        return payload

    def _request_id(self, request: ModelTargetPlanningRequest) -> str:
        signal_text = "none" if request.signal_date is None else request.signal_date.isoformat()
        return (
            f"qapp-model-target-{request.trading_date.isoformat()}-{signal_text}"
            f"-{len(request.candidates)}-{uuid4().hex}"
        )

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"{self.base_url}/v1/portfolio/optimize"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        max_retries = 5
        max_attempts = max_retries + 1
        for attempt in range(1, max_attempts + 1):
            cause: BaseException | None = None
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
                loaded = json.loads(body)
                if not isinstance(loaded, dict):
                    raise RuntimeError("risk-manager optimize returned a non-object JSON payload")
                return loaded
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                error = RuntimeError(f"risk-manager optimize HTTP {exc.code}: {body[:500]}")
                cause = exc
            except URLError as exc:
                error = RuntimeError(f"risk-manager optimize request failed: {exc}")
                cause = exc
            except json.JSONDecodeError as exc:
                error = RuntimeError("risk-manager optimize returned invalid JSON")
                cause = exc
            except RuntimeError as exc:
                error = exc

            if attempt >= max_attempts:
                _LOGGER.error(
                    "risk-manager optimize request failed after %d attempts (%d retries): %s",
                    max_attempts,
                    max_retries,
                    error,
                )
                if cause is not None:
                    raise error from cause
                raise error

            retry_number = attempt
            retry_delay_secs = retry_number
            _LOGGER.warning(
                "risk-manager optimize request failed (attempt %d/%d), retry %d/%d in %ds: %s",
                attempt,
                max_attempts,
                retry_number,
                max_retries,
                retry_delay_secs,
                error,
            )
            time.sleep(retry_delay_secs)

        raise RuntimeError(f"risk-manager optimize request failed after {max_attempts} attempts")

    def _target_quantities(
        self,
        response: dict[str, Any],
        candidates: list[ModelTargetCandidate],
    ) -> dict[str, int]:
        """
        Map the service-provided ``target_quantity`` per row to instrument ids.

        ``target_quantity == 0`` is a valid target (liquidate / hold none) and is kept;
        only a missing or non-numeric value is skipped.
        """
        stock_to_instrument = self._stock_to_instrument(candidates)
        rows = self._target_rows(response)
        quantities: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            instrument_id = stock_to_instrument.get(normalize_stock_code(row.get("stock_code")))
            if instrument_id is None:
                continue
            raw_qty = row.get("target_quantity")
            if raw_qty is None:
                continue
            try:
                qty = int(round(float(raw_qty)))
            except (TypeError, ValueError):
                continue
            if qty < 0:
                continue
            quantities[instrument_id] = qty
        return dict(sorted(quantities.items()))

    def _audit_weights(
        self,
        response: dict[str, Any],
        candidates: list[ModelTargetCandidate],
    ) -> dict[str, float]:
        """
        Map the service-provided ``target_weight`` per row to instrument ids.

        Weights are recorded to MySQL for audit only — execution is driven entirely by
        ``target_qty``. Rows the service sized by quantity only (no positive weight)
        simply have no weight entry; no synthetic weight is fabricated.
        """
        stock_to_instrument = self._stock_to_instrument(candidates)
        weights: dict[str, float] = {}
        for row in self._target_rows(response):
            if not isinstance(row, dict):
                continue
            instrument_id = stock_to_instrument.get(normalize_stock_code(row.get("stock_code")))
            if instrument_id is None:
                continue
            weight = self._coerce_float(row.get("target_weight", row.get("weight")))
            if weight is not None and weight > 0:
                weights[instrument_id] = weight
        return dict(sorted(weights.items()))

    @staticmethod
    def _stock_to_instrument(candidates: list[ModelTargetCandidate]) -> dict[str, str]:
        return {
            normalize_stock_code(candidate.stock_code): candidate.instrument_id
            for candidate in candidates
        }

    @staticmethod
    def _target_rows(response: dict[str, Any]) -> list[Any]:
        rows = response.get("target_weights") or []
        if not isinstance(rows, list):
            raise RuntimeError("risk-manager optimize response target_weights must be a list")
        return rows

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
