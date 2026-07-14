from __future__ import annotations

from typing import Callable


class LiveExecutionReconciliation:
    """Requests execution-state reconciliation at live lifecycle points."""

    def __init__(
        self,
        request_reconcile: Callable[[], None],
        warn: Callable[[str], None],
    ) -> None:
        self._request_reconcile = request_reconcile
        self._warn = warn

    def request_after_strategy_start(self) -> None:
        # Startup reconciliation can publish mass status before the strategy has
        # subscribed to it. Request again after start so broker sellable quantity
        # is republished before the first sell.
        self._request("start")

    def request_after_target_refresh(self) -> None:
        # Refreshing reference data can change the active universe; request
        # reconciliation again so the broker sellable map is current.
        self._request("refresh")

    def _request(self, trigger: str) -> None:
        try:
            self._request_reconcile()
        except Exception as exc:
            self._warn(f"Execution reconcile request on {trigger} failed: {exc}")
