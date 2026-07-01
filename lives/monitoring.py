"""
Prometheus exporter for the live trading node.

Nautilus has no native metrics/Prometheus integration, so this standalone
:class:`Actor` runs alongside the strategy inside the live ``TradingNode``. On a
timer it reads the live ``Portfolio`` and ``Cache`` (the same objects the strategy
uses — no QMT/Redis re-querying) and publishes summary gauges. It never touches the
strategy, keeping the "strategy runs unchanged in backtest and live" boundary intact.

Kept deliberately summary-level: total cash, equity, PnL, and open order/position
counts, plus a few strategy-specific health counters. Per-instrument breakdowns are
intentionally omitted to keep the Prometheus time-series cardinality low.

``prometheus_client`` is an optional dependency: if it is not installed the actor
logs a warning on start and becomes a no-op, so the trading node still runs.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.common.enums import LogColor

try:  # Optional dependency — degrade gracefully if absent.
    from prometheus_client import Gauge
    from prometheus_client import start_http_server

    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on deployment env
    Gauge = None  # type: ignore[assignment]
    start_http_server = None  # type: ignore[assignment]
    _PROMETHEUS_AVAILABLE = False


class PrometheusExporterConfig(ActorConfig, frozen=True):
    """Config for :class:`PrometheusExporter`."""

    port: int = 9100
    addr: str = "0.0.0.0"
    scrape_interval_secs: float = 10.0
    # Label value distinguishing multiple nodes/accounts in one Prometheus.
    account_label: str = "default"


# Module-level metric objects. prometheus_client registers each metric name once
# per process in its default registry; keeping them module-level makes re-creating
# the actor (e.g. across a node restart in the same process) idempotent.
if _PROMETHEUS_AVAILABLE:
    _LABELS = ("account",)
    _CASH_FREE = Gauge("qapp_cash_free", "Free (available) cash balance", _LABELS)
    _CASH_TOTAL = Gauge("qapp_cash_total", "Total cash balance", _LABELS)
    _EQUITY = Gauge("qapp_equity", "Account equity (cash + positions mark value)", _LABELS)
    _NET_EXPOSURE = Gauge("qapp_net_exposure", "Total net exposure across positions", _LABELS)
    _UNREALIZED_PNL = Gauge("qapp_unrealized_pnl", "Total unrealized PnL", _LABELS)
    _REALIZED_PNL = Gauge("qapp_realized_pnl", "Total realized PnL", _LABELS)
    _OPEN_ORDERS = Gauge("qapp_open_orders", "Number of open (working) orders", _LABELS)
    _OPEN_POSITIONS = Gauge("qapp_open_positions", "Number of open positions", _LABELS)
    _DEFERRED_BUYS = Gauge("qapp_deferred_buys", "Buys deferred waiting for free cash", _LABELS)
    _INSUFFICIENT_FUNDS = Gauge(
        "qapp_insufficient_funds_instruments",
        "Instruments blocked on insufficient funds (废单 backoff)",
        _LABELS,
    )
    _REJECTED_ORDERS = Gauge("qapp_rejected_orders", "Terminal rejected orders tracked", _LABELS)
    _SCRAPE_OK = Gauge("qapp_exporter_up", "1 if the last metrics collection succeeded", _LABELS)


class PrometheusExporter(Actor):
    """Periodically snapshots portfolio/cache state into Prometheus gauges."""

    _TIMER = "QAPP-PROMETHEUS-EXPORTER"

    def __init__(self, config: PrometheusExporterConfig) -> None:
        super().__init__(config)
        self._enabled = _PROMETHEUS_AVAILABLE
        self._server_started = False
        # Set by the runner after construction: a direct handle to the strategy so
        # its in-memory health counters can be read. The Cache does not store live
        # Strategy instances, so there is no cache lookup for this.
        self.strategy_ref: Any = None

    def on_start(self) -> None:
        if not self._enabled:
            self.log.warning(
                "prometheus_client not installed — PrometheusExporter is a no-op. "
                "Install with `pip install prometheus_client` to enable metrics.",
            )
            return
        if not self._server_started:
            start_http_server(self.config.port, addr=self.config.addr)
            self._server_started = True
            self.log.info(
                f"Prometheus metrics on http://{self.config.addr}:{self.config.port}/metrics",
                color=LogColor.GREEN,
            )
        interval = float(self.config.scrape_interval_secs)
        if interval > 0:
            self.clock.set_timer(
                name=self._TIMER,
                interval=timedelta(seconds=interval),
                callback=self._on_timer,
                fire_immediately=True,
            )

    def on_stop(self) -> None:
        if self._enabled:
            try:
                self.clock.cancel_timer(self._TIMER)
            except Exception:  # timer may not exist if start failed
                pass

    def _on_timer(self, _event: Any) -> None:
        self.collect()

    def collect(self) -> None:
        """Read portfolio/cache and set gauges. Never raises into the node."""
        if not self._enabled:
            return
        label = self.config.account_label
        try:
            # Single-venue deployment: pass venue=None so the portfolio aggregates
            # across the (one) venue. Avoids fragile venue-object derivation.
            _EQUITY.labels(label).set(self._sum_money(self.portfolio.equity()))
            _NET_EXPOSURE.labels(label).set(self._sum_money(self.portfolio.net_exposures()))
            _UNREALIZED_PNL.labels(label).set(self._sum_money(self.portfolio.unrealized_pnls()))
            _REALIZED_PNL.labels(label).set(self._sum_money(self.portfolio.realized_pnls()))

            free, total = self._balance_totals()
            _CASH_FREE.labels(label).set(free)
            _CASH_TOTAL.labels(label).set(total)

            _OPEN_ORDERS.labels(label).set(len(self.cache.orders_open()))
            _OPEN_POSITIONS.labels(label).set(len(self.cache.positions_open()))

            self._set_strategy_health(label)

            _SCRAPE_OK.labels(label).set(1)
        except Exception as exc:  # keep the node alive even if a query fails
            _SCRAPE_OK.labels(label).set(0)
            self.log.warning(f"PrometheusExporter collect failed: {exc}")

    # ---- helpers -------------------------------------------------------------

    def _balance_totals(self) -> tuple[float, float]:
        """(free, total) cash summed across the single account's currencies."""
        accounts = self.cache.accounts()
        if not accounts:
            return (0.0, 0.0)
        account = accounts[0]
        free = sum(float(m.as_double()) for m in account.balances_free().values())
        total = sum(float(m.as_double()) for m in account.balances_total().values())
        return (free, total)

    @staticmethod
    def _sum_money(money_map: Any) -> float:
        """Portfolio.* return {Currency: Money}; sum to a single float.

        A-share trading is single-currency (CNY) so summing is exact here; for a
        multi-currency book this would need FX conversion, which the target_currency
        argument on the portfolio methods can provide.
        """
        if not money_map:
            return 0.0
        return sum(float(v.as_double()) for v in money_map.values())

    def _set_strategy_health(self, label: str) -> None:
        """Read strategy-specific health counters if a strategy handle was set."""
        strategy = self.strategy_ref
        if strategy is None:
            return
        _DEFERRED_BUYS.labels(label).set(len(getattr(strategy, "_deferred_buys", {}) or {}))
        _INSUFFICIENT_FUNDS.labels(label).set(len(getattr(strategy, "_insufficient_funds", set()) or set()))
        _REJECTED_ORDERS.labels(label).set(len(getattr(strategy, "_rejected_order_ids", set()) or set()))
