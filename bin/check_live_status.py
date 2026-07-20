#!/usr/bin/env python3
"""
Poll live-trading + QMT-proxy health and raise alerts, for periodic crontab use.

This is a standalone operational script (not a package module). It fetches two
status endpoints over HTTP:

  * the live trading node's in-process status server (``lives.status_server``),
    default ``http://127.0.0.1:9200/health`` — a JSON snapshot of
    ``TradingNode.health_status()``.
  * the QMT proxy readiness probe, ``<proxy>/health/ready``, which deep-probes
    the xtdata/xttrader connections and returns HTTP 503 when not ready.

It then evaluates a set of *tunable* alert rules (a config dict overridable by
CLI flags) and, if anything is wrong, dispatches alerts. Alerting is currently a
MOCK — it prints structured lines and logs; the DingTalk hook is a stub for
later.

Exit code is 0 when no alert fired, non-zero otherwise, so a crontab / external
monitor can react to the process status directly.

Example crontab (every 5 minutes during trading hours)::

    */5 9-15 * * 1-5  cd /data/flc/code/quant/qapp && python bin/check_live_status.py >> logs/status_check.log 2>&1
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NAUTILUS_TRADER_PATH = Path(
    os.environ.get("NAUTILUS_TRADER_PATH", "/data/flc/code/quant/nautilus_trader"),
)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if NAUTILUS_TRADER_PATH.exists() and str(NAUTILUS_TRADER_PATH) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_TRADER_PATH))

import requests

logger = logging.getLogger("check_live_status")


# --------------------------------------------------------------------------- #
# Tunable alert rules
# --------------------------------------------------------------------------- #

# Each flag toggles one check. Defaults are the conservative "everything must be
# healthy" set; relax individual checks with the matching --no-* CLI flag (or an
# env var) when a deployment legitimately runs without that guarantee (e.g.
# reconciliation disabled).
DEFAULT_ALERT_RULES: dict[str, bool] = {
    "require_running": True,          # node.is_running must be True
    "require_reconciliation": True,   # if recon enabled, it must have succeeded
    "require_data_connected": True,   # all data clients connected
    "require_exec_connected": True,   # all exec clients connected
    "require_strategy_running": True, # every strategy state == RUNNING
    "require_proxy_ready": True,      # proxy /health/ready must be ready
}


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# --------------------------------------------------------------------------- #
# Alert model
# --------------------------------------------------------------------------- #


@dataclass
class Alert:
    level: str      # "critical" | "warning"
    source: str     # "live" | "proxy" | "check"
    message: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "source": self.source,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class FetchResult:
    ok: bool                 # did we successfully obtain a parseable status body?
    status_code: int | None
    payload: dict | None
    error: str | None


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def fetch_json(url: str, timeout: float, ok_codes: tuple[int, ...] = (200,)) -> FetchResult:
    """GET ``url`` and parse JSON.

    ``ok_codes`` lists status codes whose body we still treat as a valid status
    payload. For the proxy readiness probe, 503 is a *valid, parseable* "not
    ready" answer — not a fetch failure — so callers pass ``(200, 503)``.
    """
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        return FetchResult(ok=False, status_code=None, payload=None, error=repr(exc))

    try:
        payload = resp.json()
    except ValueError as exc:
        return FetchResult(ok=False, status_code=resp.status_code, payload=None, error=f"bad json: {exc}")

    if resp.status_code not in ok_codes:
        return FetchResult(
            ok=False,
            status_code=resp.status_code,
            payload=payload,
            error=f"unexpected status {resp.status_code}",
        )
    return FetchResult(ok=True, status_code=resp.status_code, payload=payload, error=None)


def fetch_live_status(url: str, timeout: float) -> FetchResult:
    return fetch_json(url, timeout, ok_codes=(200,))


def fetch_proxy_ready(base_url: str, timeout: float) -> FetchResult:
    url = base_url.rstrip("/") + "/health/ready"
    # 503 = deep probe ran and reports not-ready; that IS a status, not a failure.
    return fetch_json(url, timeout, ok_codes=(200, 503))


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def _proxy_is_ready(payload: dict | None) -> bool:
    """The proxy wraps its readiness snapshot; tolerate both flat and wrapped shapes."""
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("ready"), bool):
        return payload["ready"]
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("ready"), bool):
        return data["ready"]
    return False


def evaluate_live(status: FetchResult, rules: dict[str, bool]) -> list[Alert]:
    if not status.ok:
        return [
            Alert(
                level="critical",
                source="live",
                message="failed to fetch live trading status",
                detail={"status_code": status.status_code, "error": status.error},
            )
        ]

    payload = status.payload or {}
    alerts: list[Alert] = []

    if rules["require_running"] and not payload.get("is_running"):
        alerts.append(Alert("critical", "live", "trading node is not running",
                             {"is_running": payload.get("is_running")}))

    if rules["require_data_connected"]:
        dc = payload.get("data_clients") or {}
        if not dc.get("all_connected"):
            alerts.append(Alert("critical", "live", "data clients not all connected",
                                 {"data_clients": dc}))

    if rules["require_exec_connected"]:
        ec = payload.get("exec_clients") or {}
        if not ec.get("all_connected"):
            alerts.append(Alert("critical", "live", "exec clients not all connected",
                                 {"exec_clients": ec}))

    if rules["require_reconciliation"]:
        recon = payload.get("reconciliation") or {}
        # Only alert when reconciliation is enabled but did not succeed.
        if recon.get("enabled") and recon.get("succeeded") is not True:
            alerts.append(Alert("critical", "live", "startup reconciliation did not succeed",
                                 {"reconciliation": recon}))

    if rules["require_strategy_running"]:
        strategies = payload.get("strategies") or {}
        bad = {sid: st for sid, st in strategies.items() if st != "RUNNING"}
        if bad:
            alerts.append(Alert("critical", "live", "one or more strategies not RUNNING",
                                 {"not_running": bad}))
        elif not strategies:
            alerts.append(Alert("warning", "live", "no strategies registered", {}))

    return alerts


def evaluate_proxy(status: FetchResult, rules: dict[str, bool]) -> list[Alert]:
    if not status.ok:
        return [
            Alert(
                level="critical",
                source="proxy",
                message="failed to fetch proxy readiness",
                detail={"status_code": status.status_code, "error": status.error},
            )
        ]

    if rules["require_proxy_ready"] and not _proxy_is_ready(status.payload):
        return [
            Alert(
                level="critical",
                source="proxy",
                message="proxy is not ready",
                detail={"status_code": status.status_code, "payload": status.payload},
            )
        ]
    return []


def evaluate(live: FetchResult, proxy: FetchResult, rules: dict[str, bool]) -> list[Alert]:
    return evaluate_live(live, rules) + evaluate_proxy(proxy, rules)


# --------------------------------------------------------------------------- #
# Dispatch (MOCK)
# --------------------------------------------------------------------------- #


# DingTalk custom-robot webhook base (send endpoint). Everything except the
# per-robot access_token + signing secret is hardcoded here.
DINGTALK_WEBHOOK_URL = "https://oapi.dingtalk.com/robot/send"
DINGTALK_AT_USER_IDS: list[str] = []
DINGTALK_AT_MOBILES: list[str] = []
DINGTALK_IS_AT_ALL = False


def _format_alert_message(alerts: list[Alert]) -> str:
    """Render the alerts into a plain-text body for the DingTalk robot."""
    lines = ["[qapp] 实盘状态告警"]
    for alert in alerts:
        lines.append(f"[{alert.level.upper()}] ({alert.source}) {alert.message}")
        if alert.detail:
            lines.append(f"    detail: {json.dumps(alert.detail, ensure_ascii=False, default=str)}")
    return "\n".join(lines)


def _dingtalk_signed_url(access_token: str, secret: str) -> str:
    """Build the signed webhook URL (加签 security mode)."""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{DINGTALK_WEBHOOK_URL}?access_token={access_token}&timestamp={timestamp}&sign={sign}"


def send_dingtalk(
    alerts: list[Alert],
    access_token: str | None,
    secret: str | None,
    timeout: float = 5.0,
) -> None:
    """POST the alert summary to the DingTalk custom-robot webhook (加签 mode).

    ``access_token`` and ``secret`` are the only per-robot inputs; the webhook
    URL and @-targets are hardcoded above. When either credential is missing the
    delivery is skipped (logged), so the checker still runs without a webhook.
    """
    if not access_token or not secret:
        logger.info("DingTalk credentials missing — skipping webhook delivery")
        return None

    url = _dingtalk_signed_url(access_token, secret)
    body = {
        "msgtype": "text",
        "text": {"content": _format_alert_message(alerts)},
        "at": {
            "isAtAll": DINGTALK_IS_AT_ALL,
            "atUserIds": DINGTALK_AT_USER_IDS,
            "atMobiles": DINGTALK_AT_MOBILES,
        },
    }
    headers = {"Content-Type": "application/json"}
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
        logger.info("DingTalk webhook response: %s", resp.text)
    except requests.RequestException as exc:
        logger.error("DingTalk webhook delivery failed: %r", exc)
    return None


def print_status_report(live: FetchResult, proxy: FetchResult) -> None:
    """Print the full content fetched from each endpoint (URL, code, payload, error)."""
    for name, result in (("LIVE TRADING", live), ("QMT PROXY", proxy)):
        print(f"===== {name} =====")
        print(f"  fetched   : {'ok' if result.ok else 'FAILED'}")
        print(f"  http_code : {result.status_code}")
        if result.error:
            print(f"  error     : {result.error}")
        if result.payload is not None:
            body = json.dumps(result.payload, ensure_ascii=False, default=str, indent=2)
            # indent the multi-line payload under a "payload:" header
            print("  payload   :")
            for line in body.splitlines():
                print(f"    {line}")
        print()


def dispatch_alerts(
    alerts: list[Alert],
    access_token: str | None = None,
    secret: str | None = None,
    timeout: float = 5.0,
) -> None:
    """Log + print the structured alerts, then deliver them to DingTalk."""
    if not alerts:
        logger.info("status check OK — no alerts")
        print("OK: live trading and proxy healthy")
        return

    for alert in alerts:
        logger.warning("ALERT %s", json.dumps(alert.as_dict(), ensure_ascii=False, default=str))
        print(f"[{alert.level.upper()}] ({alert.source}) {alert.message}")

    send_dingtalk(alerts, access_token, secret, timeout=timeout)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _add_rule_flag(parser: argparse.ArgumentParser, name: str, key: str, help_text: str) -> None:
    """Add a matched --<name>/--no-<name> pair defaulting to DEFAULT_ALERT_RULES[key]."""
    dest = f"rule_{key}"
    group = parser.add_mutually_exclusive_group()
    group.add_argument(f"--{name}", dest=dest, action="store_true", default=None, help=f"require: {help_text}")
    group.add_argument(f"--no-{name}", dest=dest, action="store_false", default=None,
                       help=f"do not require: {help_text}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check live-trading + QMT-proxy health and alert.")
    parser.add_argument(
        "--live-status-url",
        default=_env("MODEL_STATUS_URL", "http://127.0.0.1:9200/health"),
        help="Live trading node status endpoint (lives.status_server).",
    )
    parser.add_argument(
        "--proxy-url",
        default=_env("QMT_BASE_URL_HTTP", "http://172.18.193.224:8000"),
        help="QMT proxy base URL; /health/ready is appended.",
    )
    parser.add_argument(
        "--timeout-secs",
        type=float,
        default=float(_env("MODEL_STATUS_TIMEOUT_SECS", "5") or "5"),
        help="Per-request HTTP timeout.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw fetched payloads as a single JSON object instead of the readable report.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the full per-endpoint status report; print only the alert summary.",
    )
    parser.add_argument(
        "--access-token",
        default=_env("DINGTALK_ACCESS_TOKEN"),
        help="DingTalk custom-robot webhook access_token (or env DINGTALK_ACCESS_TOKEN).",
    )
    parser.add_argument(
        "--secret",
        default=_env("DINGTALK_SECRET"),
        help="DingTalk robot signing secret for 加签 mode (or env DINGTALK_SECRET).",
    )

    _add_rule_flag(parser, "require-running", "require_running", "node is_running")
    _add_rule_flag(parser, "require-reconciliation", "require_reconciliation", "reconciliation succeeded")
    _add_rule_flag(parser, "require-data-connected", "require_data_connected", "data clients connected")
    _add_rule_flag(parser, "require-exec-connected", "require_exec_connected", "exec clients connected")
    _add_rule_flag(parser, "require-strategy-running", "require_strategy_running", "strategies RUNNING")
    _add_rule_flag(parser, "require-proxy-ready", "require_proxy_ready", "proxy ready")

    return parser.parse_args(argv)


def resolve_rules(args: argparse.Namespace) -> dict[str, bool]:
    """Start from defaults; apply any CLI override that was explicitly set."""
    rules = dict(DEFAULT_ALERT_RULES)
    for key in rules:
        override = getattr(args, f"rule_{key}", None)
        if override is not None:
            rules[key] = override
    return rules


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = parse_args(argv)
    rules = resolve_rules(args)

    live = fetch_live_status(args.live_status_url, args.timeout_secs)
    proxy = fetch_proxy_ready(args.proxy_url, args.timeout_secs)

    if args.json:
        print(json.dumps({"live": live.payload, "proxy": proxy.payload}, ensure_ascii=False, default=str, indent=2))
    elif not args.quiet:
        print_status_report(live, proxy)

    alerts = evaluate(live, proxy, rules)
    dispatch_alerts(alerts, args.access_token, args.secret, timeout=args.timeout_secs)

    return 1 if alerts else 0


if __name__ == "__main__":
    raise SystemExit(main())
