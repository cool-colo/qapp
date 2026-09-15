"""
EXPERIMENT — subscribe to quote ticks + order book for *every* A-share instrument.

Purpose
-------
A throwaway probe answering one question: **can the QMT venue sustain quote-tick
and order-book-depth subscriptions for the entire A-share universe at once?** It
does nothing for the trading strategy; it only opens the subscriptions and logs
how many landed and whether any data flows back, so we can watch the QMT data
client / proxy for back-pressure, dropped subscriptions, or outright failure.

Design (kept deliberately isolated so it is trivial to rip out)
---------------------------------------------------------------
* One self-contained :class:`Actor` in one file — no strategy edits.
* Takes effect *after startup*: it subscribes from :meth:`on_start`, after the
  instrument provider has loaded all instruments into the Nautilus cache (the
  node must run with ``QMT_LOAD_ALL_INSTRUMENTS=True``, which is the default).
* Enumerates the universe straight from the cache, so it needs no extra data
  wiring — whatever the venue loaded is what we subscribe to.

How to remove
-------------
Delete this file and the single ``SUBSCRIBE_ALL_EXPERIMENT``-gated block in
``lives/_target_model_node.py`` (search for that token). Nothing else references
it, and the whole thing is off unless ``SUBSCRIBE_ALL_EXPERIMENT=1`` is set.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.data import OrderBookDepth10
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import BookType


class SubscribeAllExperimentConfig(ActorConfig, frozen=True):
    """Config for :class:`SubscribeAllExperimentActor`."""

    # Order-book depth levels, matching the strategy's L2_MBP / depth=10 subscription.
    book_depth: int = 10
    # Optional cap for a staged experiment (0 = no cap, subscribe to everything).
    max_instruments: int = 0
    # How often to log how much data has arrived since start.
    report_interval_secs: float = 30.0


class SubscribeAllExperimentActor(Actor):
    """Subscribes to quote ticks + order book for every instrument, then reports flow."""

    _REPORT_TIMER = "SUBSCRIBE-ALL-EXPERIMENT-REPORT"

    def __init__(self, config: SubscribeAllExperimentConfig) -> None:
        super().__init__(config)
        self._subscribed = 0
        self._quote_ticks_received = 0
        self._book_updates_received = 0
        self._distinct_quote_instruments: set[str] = set()
        self._distinct_book_instruments: set[str] = set()

    def on_start(self) -> None:
        instrument_ids = list(self.cache.instrument_ids())
        if not instrument_ids:
            self.log.warning(
                "[subscribe-all-experiment] cache holds no instruments "
                "(started with --no-load-all-instruments?); nothing to subscribe",
            )
            return
        if int(self.config.max_instruments) > 0:
            instrument_ids = instrument_ids[: int(self.config.max_instruments)]
        self.log.info(
            f"[subscribe-all-experiment] subscribing to {len(instrument_ids)} "
            f"instruments, quote ticks + order book (depth={self.config.book_depth})",
            color=LogColor.MAGENTA,
        )
        for instrument_id in instrument_ids:
            try:
                #self.subscribe_quote_ticks(instrument_id)
                self.subscribe_order_book_depth(
                    instrument_id,
                    book_type=BookType.L2_MBP,
                    depth=int(self.config.book_depth),
                )
                self._subscribed += 1
            except Exception as exc:  # noqa: BLE001 - keep going; this is a probe
                self.log.warning(
                    f"[subscribe-all-experiment] subscribe failed for {instrument_id}: {exc}",
                )
        self.log.info(
            f"[subscribe-all-experiment] subscribe requests issued: {self._subscribed}",
            color=LogColor.MAGENTA,
        )
        interval = float(self.config.report_interval_secs)
        if interval > 0:
            self.clock.set_timer(
                name=self._REPORT_TIMER,
                interval=timedelta(seconds=interval),
                callback=self._on_report_timer,
                fire_immediately=False,
            )

    def on_quote_tick(self, tick: QuoteTick) -> None:
        self._quote_ticks_received += 1
        try:
            self._distinct_quote_instruments.add(str(tick.instrument_id))
        except Exception:  # noqa: BLE001
            pass

    def on_order_book_depth(self, depth: OrderBookDepth10) -> None:
        self._book_updates_received += 1
        try:
            self._distinct_book_instruments.add(str(depth.instrument_id))
        except Exception:  # noqa: BLE001
            pass

    def _on_report_timer(self, _event: Any) -> None:
        self.log.info(
            f"[subscribe-all-experiment] subscribed={self._subscribed} "
            f"quote_ticks={self._quote_ticks_received} "
            f"(instruments={len(self._distinct_quote_instruments)}) "
            f"book_updates={self._book_updates_received} "
            f"(instruments={len(self._distinct_book_instruments)})",
            color=LogColor.MAGENTA,
        )

    def on_stop(self) -> None:
        try:
            self.clock.cancel_timer(self._REPORT_TIMER)
        except Exception:  # noqa: BLE001 - timer may never have started
            pass
