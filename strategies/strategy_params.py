#!/usr/bin/env python3
"""Strategy-tuning parameters loaded from a YAML config file.

The live entrypoints (``lives/live_qmt_target_model_predictions.py``,
``lives/live_bigqmt_target_model_predictions.py``) and the backtest runner
(``backtests/target_model_predictions/run_backtest.py``) all build the same
``TargetModelPredictionsStrategyConfig``. The scalar knobs that feed that config
(``max_positions``, ``stop_loss``, ``risk_manager_*``, ``trading_windows``, ...)
used to be threaded through argparse + env-var defaults in each entrypoint. They
now live in a single YAML file located via ``--config``; this module is the
shared loader.

Runtime data fields (``instrument_ids``, ``bar_types``, ``signals_by_date``, the
calendars, ``initial_last_closes``, ...) are NOT here — they are loaded from
ClickHouse at build time. Node/infra params (account id, redis, clickhouse,
logging, metrics) stay on CLI/env.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import fields
from decimal import Decimal
from typing import Any


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value).replace(",", ""))


def _to_prefix_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return tuple(str(item) for item in value)


@dataclass(frozen=True)
class StrategyParams:
    """Scalar strategy-tuning parameters shared by live and backtest.

    Defaults mirror ``TargetModelPredictionsStrategyConfig`` /
    ``TargetQuantityStrategyConfig`` so an empty/partial YAML behaves exactly as
    the pre-config-file code did.
    """

    # Position sizing / rotation
    # NOTE: max_positions and timezone_name are intentionally NOT here — they are
    # consumed at startup before the strategy is built (universe query, log-file
    # naming), so they stay CLI/env args (--max-positions, --exchange-timezone).
    max_position_percent: float = 0.03
    stop_loss: float = 0.1
    trailing_take_profit: float = 0.0
    trailing_take_profit_start: float = 0.0
    min_listed_days: int = 120
    holding_days: int = 10  # backtest rebalance cadence; unused by target-timer live path
    initial_cash: Decimal = Decimal("1000000")

    # Order lifecycle / cash gating
    unfilled_timeout_secs: float = 60.0
    resubmit_interval_secs: float = 10.0
    cash_buffer_percent: float = 0.0
    target_cash_buffer_percent: float = 0.05
    order_slice_notional: Decimal = Decimal("300000")
    limit_stop_mode: str = "freeze_symbol"
    leave_non_targets: bool = False  # -> exit_non_targets = not leave_non_targets
    stop_time: str = "14:55"

    # Target weight planning / risk manager
    target_weight_planner: str = "risk_manager"
    target_weight_planner_error_policy: str = "raise"
    risk_manager_base_url: str = "http://127.0.0.1:8000"
    risk_manager_risk_model_id: str = "cn_a_basic_constraints_integer_lots"
    risk_manager_mode: str = "live"  # backtest configs set this to "backtest"
    risk_manager_timeout_secs: float = 10.0

    # Universe name filtering
    excluded_name_prefixes: tuple[str, ...] = ("*ST", "ST", "退市")

    # Exchange windows (timezone_name stays on CLI as --exchange-timezone)
    trading_windows: str = "09:29-11:30,13:00-14:55"
    exchange_trading_windows: str = "09:30-11:30,13:00-14:55"

    # Full-tick snapshot
    full_tick_refresh_secs: float = 1.0
    full_tick_prefetch_time: str = "09:27"

    # Order tagging (live)
    order_id_tag: str = "001"

    # Logging sample rates
    trade_tick_log_sample_rate: float = 0.0
    order_book_depth_log_sample_rate: float = 0.0

    @property
    def exit_non_targets(self) -> bool:
        """Whether to sell holdings absent from the latest target weights."""
        return not self.leave_non_targets

    def pretty(self, *, source: str | None = None) -> str:
        """Render the params as an aligned, boxed table for startup logging."""
        return "\n".join(self.pretty_lines(source=source))

    def pretty_lines(self, *, source: str | None = None) -> list[str]:
        """Return the boxed-table rendering as a list of lines.

        Emitting line-by-line lets a Nautilus ``Logger`` prefix each row with its
        timestamp/component instead of dumping one multi-line blob.
        """
        rows: list[tuple[str, str]] = []
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, tuple):
                value = ", ".join(str(item) for item in value)
            rows.append((f.name, str(value)))
        # Include the derived flag so the effective config is visible at a glance.
        rows.append(("exit_non_targets (derived)", str(self.exit_non_targets)))

        key_width = max(len(key) for key, _ in rows)
        val_width = max(len(val) for _, val in rows)
        title = "StrategyParams"
        if source:
            title = f"{title} ({source})"
        inner_width = max(key_width + val_width + 3, len(title))

        border = "+" + "-" * (inner_width + 2) + "+"
        lines = [border, f"| {title.ljust(inner_width)} |", border]
        for key, val in rows:
            padded = f"{key.ljust(key_width)} : {val.ljust(val_width)}"
            lines.append(f"| {padded.ljust(inner_width)} |")
        lines.append(border)
        return lines

    def log(self, logger: Any, *, source: str | None = None) -> None:
        """Emit the boxed table to a Nautilus ``Logger`` (one ``info`` per line).

        ``logger`` is a ``nautilus_trader.common.component.Logger`` — e.g.
        ``node.get_logger()`` or ``Logger("StrategyParams")``.
        """
        from nautilus_trader.common.enums import LogColor

        for line in self.pretty_lines(source=source):
            logger.info(line, color=LogColor.CYAN)

    @classmethod
    def from_yaml(cls, path: str | None) -> "StrategyParams":
        """Load strategy params from a YAML file.

        Unknown keys raise ``ValueError`` (typos fail loudly). Missing keys fall
        back to the field defaults above. ``order_slice_notional`` /
        ``initial_cash`` are coerced to ``Decimal`` and ``excluded_name_prefixes``
        to a tuple.
        """
        if not path:
            raise ValueError(
                "No strategy config file provided. Pass --config <path> "
                "(or set STRATEGY_CONFIG_FILE).",
            )

        import yaml  # lazy: importing this dataclass must not require PyYAML

        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"Strategy config {path!r} must be a YAML mapping, got {type(raw).__name__}.")

        known = {f.name for f in fields(cls)}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(
                f"Unknown strategy config key(s) in {path!r}: {', '.join(unknown)}. "
                f"Known keys: {', '.join(sorted(known))}.",
            )

        data = dict(raw)
        if "order_slice_notional" in data:
            data["order_slice_notional"] = _to_decimal(data["order_slice_notional"])
        if "initial_cash" in data:
            data["initial_cash"] = _to_decimal(data["initial_cash"])
        if "excluded_name_prefixes" in data:
            data["excluded_name_prefixes"] = _to_prefix_tuple(data["excluded_name_prefixes"])

        return cls(**data)
