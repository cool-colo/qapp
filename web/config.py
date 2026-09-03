"""Load and validate the dashboard configuration (web/config.yaml).

Supports ``${ENV}`` and ``${ENV:-default}`` placeholder expansion from the process
environment, plus a best-effort ``.env`` loader (no python-dotenv dependency).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


@dataclass(frozen=True)
class MySqlConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass(frozen=True)
class SourceConfig:
    name: str
    mysql: MySqlConfig


@dataclass(frozen=True)
class ClickHouseConfig:
    url: str = "http://127.0.0.1:8123"
    database: str | None = None
    user: str | None = "default"
    password: str | None = None
    timeout_secs: float = 30.0


@dataclass(frozen=True)
class AppConfig:
    sources: list[SourceConfig]
    clickhouse: ClickHouseConfig
    # Per-account return-report start dates, keyed by "account_id/trader_id".
    # Value is an ISO date (YYYY-MM-DD). Falls back to one month ago when absent.
    return_report_start: dict[str, str]
    # Accounts hidden from the UI, each key "account_id/trader_id".
    account_blacklist: frozenset[str]

    def source(self, name: str) -> SourceConfig:
        for src in self.sources:
            if src.name == name:
                return src
        raise KeyError(f"unknown source: {name!r}")

    def report_start_for(self, account_id: str, trader_id: str) -> str | None:
        return self.return_report_start.get(f"{account_id}/{trader_id}")

    def is_blocked(self, account_id: str, trader_id: str) -> bool:
        return f"{account_id}/{trader_id}" in self.account_blacklist


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file without overriding existing values."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _expand(value: Any) -> Any:
    """Recursively expand ${ENV} / ${ENV:-default} placeholders in strings."""
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if not isinstance(value, str):
        return value

    def repl(match: re.Match[str]) -> str:
        env_name, default = match.group(1), match.group(2)
        found = os.environ.get(env_name)
        if found is not None:
            return found
        if default is not None:
            return default
        raise KeyError(
            f"config references ${{{env_name}}} but it is not set and has no default",
        )

    return _PLACEHOLDER_RE.sub(repl, value)


def _build_mysql(raw: dict[str, Any], source_name: str) -> MySqlConfig:
    try:
        return MySqlConfig(
            host=str(raw["host"]),
            port=int(raw["port"]),
            user=str(raw["user"]),
            password=str(raw.get("password", "")),
            database=str(raw["database"]),
        )
    except KeyError as exc:
        raise ValueError(f"source {source_name!r} mysql config missing key: {exc}") from exc


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    config_path = Path(path) if path else REPO_ROOT / "web" / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"dashboard config not found at {config_path}. "
            "Copy web/config.example.yaml to web/config.yaml and edit it.",
        )

    _load_dotenv(REPO_ROOT / ".env")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw = _expand(raw)

    raw_sources = raw.get("sources") or []
    if not raw_sources:
        raise ValueError("config must define at least one entry under 'sources'")

    sources: list[SourceConfig] = []
    seen: set[str] = set()
    for entry in raw_sources:
        name = str(entry["name"])
        if name in seen:
            raise ValueError(f"duplicate source name: {name!r}")
        seen.add(name)
        sources.append(SourceConfig(name=name, mysql=_build_mysql(entry["mysql"], name)))

    ch_raw = raw.get("clickhouse") or {}
    clickhouse = ClickHouseConfig(
        url=str(ch_raw.get("url", "http://127.0.0.1:8123")),
        database=(str(ch_raw["database"]) if ch_raw.get("database") else None),
        user=(str(ch_raw["user"]) if ch_raw.get("user") else "default"),
        password=(str(ch_raw["password"]) if ch_raw.get("password") else None),
        timeout_secs=float(ch_raw.get("timeout_secs", 30.0)),
    )

    report_raw = raw.get("return_report_start") or {}
    if not isinstance(report_raw, dict):
        raise ValueError("return_report_start must be a mapping of 'account/trader' -> date")
    return_report_start = {str(k): str(v) for k, v in report_raw.items()}

    blacklist_raw = raw.get("account_blacklist") or []
    if not isinstance(blacklist_raw, list):
        raise ValueError("account_blacklist must be a list of 'account_id/trader_id' strings")
    account_blacklist = frozenset(str(k) for k in blacklist_raw)

    return AppConfig(
        sources=sources,
        clickhouse=clickhouse,
        return_report_start=return_report_start,
        account_blacklist=account_blacklist,
    )
