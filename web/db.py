"""Data access for the dashboard: MySQL (live_* tables) and ClickHouse (bars).

Both clients are read-only and self-contained. The ClickHouse client is a small
standalone copy of the HTTP query helpers in
``backtests/data_providers/clickhouse.py`` — that module is NOT imported here because
it pulls ``nautilus_trader`` at import time, which the dashboard must not require.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pymysql
from pymysql.cursors import DictCursor

from web.config import AppConfig, ClickHouseConfig, MySqlConfig

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def jsonable(value: Any) -> Any:
    """Convert DB row values into JSON-serializable primitives."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


def jsonable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: jsonable(v) for k, v in row.items()} for row in rows]


class MySqlSource:
    """A per-source MySQL reader. Opens a short-lived connection per query.

    The dashboard is a low-traffic single-user tool, so a fresh connection per
    request (with pymysql's own reconnect semantics) is simpler and safer than a
    long-lived pool; pre-ping/recycle concerns don't apply.
    """

    def __init__(self, config: MySqlConfig) -> None:
        self._config = config

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        conn = pymysql.connect(
            host=self._config.host,
            port=self._config.port,
            user=self._config.user,
            password=self._config.password,
            database=self._config.database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=10,
            read_timeout=60,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or {})
                return list(cur.fetchall())
        finally:
            conn.close()


def quote_identifier(identifier: str) -> str:
    parts = [part.strip() for part in identifier.split(".")]
    if not parts or any(not IDENTIFIER_RE.match(part) for part in parts):
        raise ValueError(f"unsafe ClickHouse identifier: {identifier!r}")
    return ".".join(f"`{part}`" for part in parts)


def quote_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def ensure_json_each_row(sql: str) -> str:
    stripped = sql.strip().rstrip(";")
    if re.search(r"\bFORMAT\s+JSONEachRow\b", stripped, flags=re.IGNORECASE):
        return stripped
    return f"{stripped}\nFORMAT JSONEachRow"


class ClickHouseClient:
    """Minimal read-only ClickHouse HTTP client (JSONEachRow)."""

    def __init__(self, config: ClickHouseConfig) -> None:
        self._config = config

    def query(self, sql: str) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if self._config.database:
            params["database"] = self._config.database
        url = self._config.url.rstrip("/")
        if params:
            url = f"{url}?{urlencode(params)}"

        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if self._config.user:
            headers["X-ClickHouse-User"] = self._config.user
        if self._config.password:
            headers["X-ClickHouse-Key"] = self._config.password

        request = Request(
            url=url,
            data=ensure_json_each_row(sql).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._config.timeout_secs) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ClickHouse HTTP {exc.code}: {body[:1000]}") from exc
        except URLError as exc:
            raise RuntimeError(f"ClickHouse request failed: {exc}") from exc

        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def stock_names(self, codes: list[str]) -> dict[str, str]:
        """Map stock_code (e.g. '000001.SZ') -> name via stock_basic.ts_code.

        Returns an empty dict on any ClickHouse error so name lookups never break
        the tables that use them.
        """
        codes = [c for c in {str(c) for c in codes} if c]
        if not codes:
            return {}
        in_list = ", ".join(quote_literal(c) for c in codes)
        sql = f"SELECT ts_code, name FROM stock_basic WHERE ts_code IN ({in_list})"
        try:
            rows = self.query(sql)
        except RuntimeError:
            return {}
        return {str(r["ts_code"]): str(r["name"]) for r in rows}

    def bar_query(
        self,
        symbol: str,
        start: str,
        end: str,
        *,
        table: str = "dws_stock_factor_wide",
        symbol_column: str = "source_code",
        limit: int = 0,
    ) -> str:
        ts = quote_identifier("trade_date")
        where = [
            f"{quote_identifier(symbol_column)} = {quote_literal(symbol)}",
            f"{ts} >= parseDateTimeBestEffort({quote_literal(start)})",
            f"{ts} <= parseDateTimeBestEffort({quote_literal(end)})",
        ]
        limit_clause = f"\nLIMIT {limit:d}" if limit > 0 else ""
        return f"""
SELECT
    {ts} AS ts,
    {quote_identifier("open")} AS open,
    {quote_identifier("high")} AS high,
    {quote_identifier("low")} AS low,
    {quote_identifier("close")} AS close,
    {quote_identifier("vol")} AS volume
FROM {quote_identifier(table)}
WHERE {" AND ".join(where)}
ORDER BY {ts} ASC{limit_clause}
"""


class NodeApiClient:
    """Minimal client for a live node's control API (stdlib urllib, like ClickHouse).

    The token, if configured, is sent as the ``X-Control-Token`` header on writes and
    stays entirely server-side (the browser talks only to the dashboard, which
    proxies here).
    """

    def __init__(self, base_url: str, token: str | None = None, timeout_secs: float = 15.0) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_secs

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._base}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params)}"
        return self._request(Request(url=url, method="GET"))

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self._base}/{path.lstrip('/')}"
        data = json.dumps(body or {}).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self._token:
            headers["X-Control-Token"] = self._token
        return self._request(Request(url=url, data=data, headers=headers, method="POST"))

    def _request(self, request: Request) -> Any:
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"node API HTTP {exc.code}: {body[:1000]}") from exc
        except URLError as exc:
            raise RuntimeError(f"node API request failed: {exc}") from exc
        if not raw.strip():
            return {}
        return json.loads(raw)


class DataAccess:
    """Bundles per-source MySQL readers plus the shared ClickHouse client."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._mysql = {src.name: MySqlSource(src.mysql) for src in config.sources}
        self.clickhouse = ClickHouseClient(config.clickhouse)
        # One control-API client per account (account_id/trader_id) — each account
        # runs its own live node, so the node URL is keyed by account, not source.
        self._node_api = {
            key: NodeApiClient(cfg.url, cfg.token)
            for key, cfg in config.node_api.items()
        }
        self._name_cache: dict[str, str] = {}

    @property
    def source_names(self) -> list[str]:
        return [src.name for src in self._config.sources]

    def resolve_names(self, codes: list[str]) -> dict[str, str]:
        """Cached stock_code -> name lookup (names are effectively static)."""
        missing = [c for c in {str(c) for c in codes if c} if c not in self._name_cache]
        if missing:
            self._name_cache.update(self.clickhouse.stock_names(missing))
        return {c: self._name_cache.get(c, "") for c in codes if c}

    def attach_names(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add a stock_name field (after stock_code) to rows that carry stock_code."""
        codes = [r.get("stock_code") for r in rows if r.get("stock_code")]
        names = self.resolve_names(codes)
        out = []
        for r in rows:
            new_row: dict[str, Any] = {}
            for k, v in r.items():
                new_row[k] = v
                if k == "stock_code":
                    new_row["stock_name"] = names.get(str(v), "")
            out.append(new_row)
        return out

    def mysql(self, source: str) -> MySqlSource:
        try:
            return self._mysql[source]
        except KeyError:
            raise KeyError(f"unknown source: {source!r}") from None

    def node_api(self, account_id: str, trader_id: str) -> "NodeApiClient | None":
        """The live-node control-API client for an account, or None if not configured."""
        return self._node_api.get(f"{account_id}/{trader_id}")
