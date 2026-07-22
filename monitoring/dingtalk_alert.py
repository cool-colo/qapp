#!/usr/bin/env python3
"""
Reusable DingTalk custom-robot alerting + lightweight ``.env`` loading.

Extracted from ``bin/check_live_status.py`` so scripts, operational tools, and
live trading can share a single alert path. Sends messages to a DingTalk
custom-robot webhook in 加签 (signed) security mode.

Dependencies are kept minimal on purpose (stdlib + ``requests``): the repo does
not ship ``python-dotenv``, so ``.env`` parsing is a small built-in reader.

``.env`` resolution precedence (first existing file wins):

  1. an explicit path passed by the caller (e.g. a ``--env-file`` flag),
  2. ``<script_dir>/.env`` (the directory of the calling script),
  3. ``<cwd>/.env`` (the current working directory).

Credentials are read from the environment (after ``load_env`` has populated it):
``DINGTALK_ACCESS_TOKEN`` and ``DINGTALK_SECRET``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import requests

logger = logging.getLogger("dingtalk_alert")

# DingTalk custom-robot webhook send endpoint. Only the per-robot access_token +
# signing secret vary; everything else is fixed.
DINGTALK_WEBHOOK_URL = "https://oapi.dingtalk.com/robot/send"


# --------------------------------------------------------------------------- #
# .env loading
# --------------------------------------------------------------------------- #
def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` .env file.

    Blank lines and ``#`` comments are ignored; an optional leading ``export``
    is stripped; surrounding single/double quotes on the value are removed.
    """
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def resolve_env_file(
    explicit: str | os.PathLike[str] | None = None,
    script_dir: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Return the first existing .env path per the documented precedence."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if script_dir:
        candidates.append(Path(script_dir) / ".env")
    candidates.append(Path.cwd() / ".env")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_env(
    explicit: str | os.PathLike[str] | None = None,
    script_dir: str | os.PathLike[str] | None = None,
    override: bool = False,
) -> dict[str, str]:
    """Load a ``.env`` file into ``os.environ`` and return the parsed values.

    Precedence: ``explicit`` → ``<script_dir>/.env`` → ``<cwd>/.env``. Only the
    first existing file is read. By default existing environment variables are
    preserved (``setdefault`` semantics); pass ``override=True`` to let the file
    win. Returns the parsed dict (empty if no file was found).
    """
    path = resolve_env_file(explicit, script_dir)
    if path is None:
        logger.debug("no .env file found (explicit=%s script_dir=%s)", explicit, script_dir)
        return {}
    values = _parse_env_file(path)
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    logger.info("loaded %d values from %s", len(values), path)
    return values


# --------------------------------------------------------------------------- #
# DingTalk alerter
# --------------------------------------------------------------------------- #
@dataclass
class DingTalkAlerter:
    """Send text messages to a DingTalk custom-robot webhook (加签 mode).

    ``access_token`` and ``secret`` are the only per-robot inputs. When either
    is missing, sends are skipped (logged) so callers still run without a
    configured webhook.
    """

    access_token: str | None = None
    secret: str | None = None
    webhook_url: str = DINGTALK_WEBHOOK_URL
    timeout: float = 5.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, timeout: float = 5.0) -> "DingTalkAlerter":
        source = env if env is not None else os.environ
        return cls(
            access_token=source.get("DINGTALK_ACCESS_TOKEN") or None,
            secret=source.get("DINGTALK_SECRET") or None,
            timeout=timeout,
        )

    @property
    def configured(self) -> bool:
        return bool(self.access_token and self.secret)

    def _signed_url(self) -> str:
        """Build the signed webhook URL (加签 security mode)."""
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            (self.secret or "").encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return f"{self.webhook_url}?access_token={self.access_token}&timestamp={timestamp}&sign={sign}"

    def send_text(
        self,
        content: str,
        title: str | None = None,
        at_all: bool = False,
        at_mobiles: list[str] | None = None,
        at_user_ids: list[str] | None = None,
    ) -> bool:
        """POST a plain-text message. Returns True on a successful send.

        ``title`` is prepended as the first line when provided. A missing
        credential is a no-op that logs and returns False.
        """
        if not self.configured:
            logger.info("DingTalk credentials missing — skipping webhook delivery")
            return False

        body_text = f"{title}\n{content}" if title else content
        body = {
            "msgtype": "text",
            "text": {"content": body_text},
            "at": {
                "isAtAll": at_all,
                "atMobiles": at_mobiles or [],
                "atUserIds": at_user_ids or [],
            },
        }
        headers = {"Content-Type": "application/json"}
        try:
            resp = requests.post(self._signed_url(), json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.error("DingTalk webhook delivery failed: %r", exc)
            return False

        logger.info("DingTalk webhook response: %s", resp.text)
        try:
            ok = resp.json().get("errcode") == 0
        except ValueError:
            ok = False
        return ok
