"""
In-process HTTP status server for the live trading node.

Runs a tiny stdlib :mod:`http.server` on a daemon thread alongside the live
``TradingNode``, holding a direct reference to the node. On request it calls
``node.health_status()`` — a synchronous, side-effect-free read of the node's
health-relevant state — and returns it as JSON. Because it does not go through
the Nautilus event loop, it stays responsive even if a strategy callback is
blocked, which is exactly when a monitor most needs an answer.

This is intentionally separate from ``lives.monitoring.PrometheusExporter``:
that exporter is a Nautilus ``Actor`` (fired by the node's timer/event loop and
scoped to portfolio/cache), whereas ``health_status()`` is a *node* method an
Actor cannot reach. Here we run outside the node and read the node directly.

The server never raises into the trading node: every handler is wrapped so a
failure becomes an HTTP 500 rather than a crashed thread.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class StatusServerConfig:
    """Config for :class:`LiveStatusServer`."""

    port: int = 9200
    addr: str = "0.0.0.0"


class LiveStatusServer:
    """Serves ``node.health_status()`` over HTTP on a daemon thread.

    Parameters
    ----------
    node : TradingNode
        The live node to read health from. Only ``health_status()`` is called.
    strategy_ref : Any, optional
        Direct handle to the running strategy so its in-memory health counters
        (deferred buys, insufficient-funds backoff, rejected orders) can be
        surfaced alongside the node status. The cache does not store live
        Strategy instances, so this is passed in explicitly.
    config : StatusServerConfig, optional
        Bind address / port.
    """

    def __init__(
        self,
        node: Any,
        strategy_ref: Any = None,
        config: StatusServerConfig | None = None,
    ) -> None:
        self._node = node
        self._strategy_ref = strategy_ref
        self._config = config or StatusServerConfig()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Bind the socket and serve on a background daemon thread."""
        if self._httpd is not None:
            return
        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer((self._config.addr, self._config.port), handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="qapp-live-status-server",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Shut the server down and join the thread. Safe to call more than once."""
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    @property
    def port(self) -> int:
        return self._config.port

    # ---- status assembly -----------------------------------------------------

    def _build_status(self) -> dict:
        """Node health plus strategy-specific counters. Never raises."""
        status = self._node.health_status()
        status["server_time"] = datetime.now(timezone.utc).isoformat()
        status["strategy_health"] = self._strategy_health()
        return status

    def _strategy_health(self) -> dict:
        strategy = self._strategy_ref
        if strategy is None:
            return {}
        return {
            "deferred_buys": len(getattr(strategy, "_deferred_buys", {}) or {}),
            "insufficient_funds": len(getattr(strategy, "_insufficient_funds", set()) or set()),
            "rejected_orders": len(getattr(strategy, "_rejected_order_ids", set()) or set()),
        }

    # ---- request handling ----------------------------------------------------

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class _Handler(BaseHTTPRequestHandler):
            # Silence the default stderr access log; the trading node has its own logging.
            def log_message(self, *_args: Any) -> None:  # noqa: N802
                pass

            def _send(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, default=str).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0].rstrip("/") or "/"
                try:
                    if path == "/health/live":
                        self._send(200, {"status": "alive"})
                    elif path in ("/health", "/"):
                        self._send(200, server._build_status())
                    else:
                        self._send(404, {"error": "not found", "path": self.path})
                except Exception as exc:  # never let a handler crash the thread
                    self._send(500, {"error": repr(exc)})

        return _Handler
