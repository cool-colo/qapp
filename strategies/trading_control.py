"""
Standalone manual trading-control state and logic for the target-quantity strategy.

`TradingController` owns the manual-pause flag and the manual-sell orchestration so
that this operational concern is not entangled with the strategy's convergence
engine. It is driven from the live-node control-server thread and talks to the
strategy through the narrow ``TradingControlHost`` protocol below — it never reaches
into strategy internals directly.

Thread-safety: the controller runs on the control-server thread. Setting the pause
flag is an atomic bool write. ``manual_sell`` serializes its target mutation against
the trading thread's convergence via the host's convergence lock (acquired blocking
with a timeout so a manual sell is not silently dropped mid-convergence), releases it
before forcing convergence (the host re-acquires the same non-reentrant lock), and
forces convergence so the sell runs even while paused.
"""

from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal
from typing import Protocol
from typing import runtime_checkable


@runtime_checkable
class TradingControlHost(Protocol):
    """The minimal strategy surface the controller drives.

    Implemented by ``TargetQuantityStrategy``. Keeping this interface small keeps the
    strategy free of control-specific logic — it exposes only convergence primitives.
    """

    # Convergence serialization lock (a non-reentrant ``threading.Lock``).
    control_converge_lock: threading.Lock

    def control_set_paused_flag(self, paused: bool) -> None:
        """Set the persistent pause guard the convergence chokepoint consults."""

    def control_current_targets(self) -> dict[str, Decimal]:
        """Return a copy of the current per-instrument target quantities."""

    def control_open_long_quantities(self) -> dict[str, Decimal]:
        """Return held long quantities (instrument_id -> qty > 0) from the cache."""

    def control_sellable_quantity(self, instrument_id: str) -> Decimal | None:
        """Return the broker-reconciled sellable quantity, or None if unknown."""

    def control_clock_date(self) -> date:
        """Return the strategy clock's current trading date."""

    def control_apply_targets(
        self,
        targets: dict[str, Decimal],
        target_date: date,
        reason: str,
    ) -> None:
        """Push a new target map (does not itself force convergence while paused)."""

    def control_force_converge(self, current_date: date, trigger: str) -> None:
        """Force one convergence, bypassing the pause flag (re-acquires the lock)."""

    def control_log(self, message: str) -> None:
        """Emit an informational log line through the strategy logger."""


# Reason strings recorded on the manual targets / audit log. Distinct from daily and
# hourly refresh reasons so a manual sell never collides with them (and is naturally
# superseded by the next refresh — matching the "no restore" semantics).
SELL_REASON = "manual_sell"
SELL_ALL_REASON = "manual_sell_all"


class TradingController:
    """Owns manual-pause state and manual-sell orchestration for one strategy."""

    def __init__(self, host: TradingControlHost, lock_timeout: float = 5.0) -> None:
        self._host = host
        self._lock_timeout = lock_timeout
        self._paused = False

    # ------------------------------------------------------------------
    # Pause / resume
    # ------------------------------------------------------------------
    def set_paused(self, paused: bool) -> None:
        """Set the manual-pause flag (atomic) and mirror it into the host guard."""
        self._paused = bool(paused)
        self._host.control_set_paused_flag(self._paused)
        self._host.control_log(f"trading_paused set to {self._paused}")

    def is_paused(self) -> bool:
        return self._paused

    # ------------------------------------------------------------------
    # Manual sell
    # ------------------------------------------------------------------
    def sell(self, instrument_ids: list[str] | None, reason: str) -> dict:
        """
        Sell held sellable quantity for the given instruments (or all holdings).

        Copies the current target map and, for each affected instrument, lowers its
        target to ``held - sellable`` (keeping any non-sellable remainder), then
        pushes the new target and forces convergence — bypassing the manual-pause
        flag for this one action without clearing it. There is no restore: the next
        daily/hourly target refresh repopulates targets.

        Instruments whose sellable quantity is unknown (reconciler has no value yet)
        are skipped and recorded rather than sold at an unknown quantity.
        """
        target_only = instrument_ids is not None
        requested = {str(iid) for iid in (instrument_ids or [])}

        lock = self._host.control_converge_lock
        # Serialize the target mutation against the trading thread's convergence.
        acquired = lock.acquire(timeout=self._lock_timeout)
        if not acquired:
            raise TimeoutError("manual_sell could not acquire convergence lock")
        try:
            held = self._host.control_open_long_quantities()

            if target_only:
                affected_ids = [iid for iid in requested if iid in held]
                missing = sorted(requested.difference(held))
            else:
                affected_ids = sorted(held)
                missing = []

            new_targets: dict[str, Decimal] = self._host.control_current_targets()
            affected: list[str] = []
            skipped: list[dict] = []
            for iid_text in affected_ids:
                held_qty = held[iid_text]
                sellable = self._host.control_sellable_quantity(iid_text)
                if sellable is None:
                    skipped.append({"instrument_id": iid_text, "reason": "sellable_unknown"})
                    continue
                remainder = held_qty - Decimal(str(sellable))
                if remainder < 0:
                    remainder = Decimal(0)
                new_targets[iid_text] = remainder
                affected.append(iid_text)

            for iid_text in missing:
                skipped.append({"instrument_id": iid_text, "reason": "not_held"})

            current_date = self._host.control_clock_date()
        finally:
            # Release before applying targets / forcing convergence — the host
            # re-acquires this same non-reentrant lock in both.
            lock.release()

        if affected:
            self._host.control_apply_targets(
                new_targets,
                target_date=current_date,
                reason=reason,
            )
            # apply_targets' own convergence early-returns while paused; force it here
            # so the sell runs regardless of the pause flag.
            self._host.control_force_converge(current_date=current_date, trigger=reason)

        return {
            "affected": affected,
            "target_qty": {iid: int(new_targets[iid]) for iid in affected},
            "skipped": skipped,
        }
