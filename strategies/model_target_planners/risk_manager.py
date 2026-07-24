from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request
from urllib.request import urlopen

from nautilus_trader.common.enums import LogColor

from strategies.model_target_planners.base import CurrentHolding
from strategies.model_target_planners.base import ModelTargetCandidate
from strategies.model_target_planners.base import ModelTargetPlan
from strategies.model_target_planners.base import ModelTargetPlanner
from strategies.model_target_planners.base import ModelTargetPlanningRequest
from strategies.model_target_planners.base import TargetContext
from strategies.model_target_planners.base import TargetInfo
from strategies.model_target_planners.base import normalize_stock_code


class RiskManagerModelTargetPlanner(ModelTargetPlanner):
    reason = "risk_manager_optimize"

    def __init__(
        self,
        base_url: str,
        risk_model_id: str,
        mode: str,
        *,
        log: Any,
        timeout_secs: float = 10.0,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.risk_model_id = str(risk_model_id or "").strip()
        self.mode = str(mode or "").strip()
        self.timeout_secs = float(timeout_secs)
        if log is None:
            raise ValueError("log is required for risk_manager target planner")
        self.log = log
        if not self.base_url:
            raise ValueError("risk_manager_base_url is required for risk_manager target planner")
        if not self.risk_model_id:
            raise ValueError("risk_manager_risk_model_id is required for risk_manager target planner")
        if self.mode not in {"backtest", "simulation", "live"}:
            raise ValueError("risk_manager_mode must be one of: backtest, simulation, live")

    def plan(self, request: ModelTargetPlanningRequest) -> ModelTargetPlan:
        # Nothing to optimize when there are neither signal candidates nor holdings.
        if not request.candidates and not request.current_holdings:
            return ModelTargetPlan(
                trading_date=request.trading_date,
                signal_date=request.signal_date,
                targets=[],
                reason=self.reason,
            )
        request_id = self._request_id(request)
        response = self._post_json(self._payload(request, request_id))
        if not bool(response.get("success")):
            status = response.get("status")
            failure_reason = response.get("failure_reason")
            raise RuntimeError(
                f"risk-manager optimize failed status={status} failure_reason={failure_reason}",
            )
        # Response rows may reference either a signal candidate (buy/hold) or a current
        # holding being liquidated (target_qty == 0), so map both back to instrument ids.
        stock_to_instrument = self._stock_to_instrument(request)
        targets = self._targets(response, stock_to_instrument)
        return ModelTargetPlan(
            trading_date=request.trading_date,
            signal_date=request.signal_date,
            targets=targets,
            reason=self.reason,
            request_id=request_id,
        )

    def _payload(self, request: ModelTargetPlanningRequest, request_id: str) -> dict[str, Any]:
        asof_date = request.signal_date or request.trading_date
        holdings = sorted(request.current_holdings, key=lambda holding: holding.stock_code)
        current_weights = [self._holding_payload(holding) for holding in holdings]
        payload: dict[str, Any] = {
            "request_id": request_id,
            "mode": self.mode,
            "risk_model_id": self.risk_model_id,
            "asof_date": asof_date.isoformat(),
            "trade_date": request.trading_date.isoformat(),
            "candidates": [self._candidate_payload(candidate) for candidate in request.candidates],
            "current_weights": current_weights,
            "benchmark_weights": [],
        }
        # Pre-market investable total (net of trading buffer) so the service sizes share
        # counts server-side from candidate open prices.
        investable = request.investable_asset if request.investable_asset is not None else request.total_asset
        if investable is not None and float(investable) > 0:
            payload["total_asset"] = float(investable)
        return payload

    @staticmethod
    def _holding_payload(holding: CurrentHolding) -> dict[str, Any]:
        # ``recent_target_date`` is the internal field name; the wire contract uses
        # ``recent_buy_date`` (the value is the last positive-target date).
        payload: dict[str, Any] = {
            "stock_code": holding.stock_code,
            "quantity": int(holding.quantity),
            "price": float(holding.price),
            "recent_buy_date": (
                None if holding.recent_target_date is None else holding.recent_target_date.isoformat()
            ),
            "recent_holding_days": int(holding.recent_holding_days),
        }
        return payload

    @staticmethod
    def _candidate_payload(candidate: ModelTargetCandidate) -> dict[str, Any]:
        # Candidates are built with a guaranteed open price and pred_return_live
        # (see TargetModelPredictionsStrategy._build_candidates), so both are sent
        # unconditionally here.
        return {
            "stock_code": candidate.stock_code,
            "score": candidate.score,
            "is_tradable": True,
            "expected_return": float(candidate.expected_return),
            "price": float(candidate.open_price),
        }

    def _request_id(self, request: ModelTargetPlanningRequest) -> str:
        signal_text = "none" if request.signal_date is None else request.signal_date.isoformat()
        return (
            f"qapp-model-target-{request.trading_date.isoformat()}-{signal_text}"
            f"-{len(request.candidates)}-{uuid4().hex}"
        )

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"{self.base_url}/v1/portfolio/optimize"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._log_payload_info("risk-manager optimize request payload", payload)
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
                self._log_payload_info("risk-manager optimize response payload", loaded)
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
                self.log.error(
                    (
                        "risk-manager optimize request failed after "
                        f"{max_attempts} attempts ({max_retries} retries): {error}"
                    ),
                    color=LogColor.RED,
                )
                if cause is not None:
                    raise error from cause
                raise error

            retry_number = attempt
            retry_delay_secs = retry_number
            self.log.warning(
                (
                    "risk-manager optimize request failed "
                    f"(attempt {attempt}/{max_attempts}), "
                    f"retry {retry_number}/{max_retries} in {retry_delay_secs}s: {error}"
                ),
                color=LogColor.YELLOW,
            )
            time.sleep(retry_delay_secs)

        raise RuntimeError(f"risk-manager optimize request failed after {max_attempts} attempts")

    def _log_payload_info(self, label: str, payload: Any) -> None:
        payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.log.info(f"{label}: {payload_text}", color=LogColor.BLUE)

    def _targets(
        self,
        response: dict[str, Any],
        stock_to_instrument: dict[str, str],
    ) -> list[TargetInfo]:
        """
        Map service-provided target rows to first-class target records.

        ``target_quantity == 0`` is a valid target (liquidate / hold none) and is kept.
        Missing or non-positive weights remain absent; no synthetic weight is fabricated.
        """
        targets: list[TargetInfo] = []
        for row in self._target_rows(response):
            if not isinstance(row, dict):
                continue
            stock_code = normalize_stock_code(row.get("stock_code"))
            instrument_id = stock_to_instrument.get(stock_code)
            if instrument_id is None:
                continue
            weight = self._coerce_float(row.get("target_weight", row.get("weight")))
            quantity = self._target_quantity(row.get("target_quantity"))
            targets.append(
                TargetInfo(
                    stock_code=stock_code,
                    weight=weight if weight is not None and weight > 0 else None,
                    quantity=quantity,
                    target_context=TargetContext(),
                    target_version=None,
                    instrument_id=instrument_id,
                    is_locked=self._is_locked(row),
                ),
            )
        return sorted(targets, key=lambda target: target.instrument_id)

    @staticmethod
    def _stock_to_instrument(request: ModelTargetPlanningRequest) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for candidate in request.candidates:
            mapping[normalize_stock_code(candidate.stock_code)] = candidate.instrument_id
        for holding in request.current_holdings:
            mapping.setdefault(normalize_stock_code(holding.stock_code), holding.instrument_id)
        return mapping

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

    @staticmethod
    def _target_quantity(value: Any) -> int | None:
        if value is None:
            return None
        try:
            quantity = int(round(float(value)))
        except (TypeError, ValueError):
            return None
        if quantity < 0:
            return None
        return quantity

    @staticmethod
    def _is_locked(row: dict[str, Any]) -> bool | None:
        value = row.get("is_locked")
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        raise RuntimeError("risk-manager optimize response is_locked must be a boolean when present")
