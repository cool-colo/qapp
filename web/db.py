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
from time import monotonic
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


def quote_qualified_table(database: str | None, table: str) -> str:
    """Quote a ClickHouse table, with its database supplied separately."""
    if not database:
        return quote_identifier(table)
    if "." in table:
        raise ValueError("table must not be database-qualified when database is supplied separately")
    return f"{quote_identifier(database)}.{quote_identifier(table)}"


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
        self._classification_cache: dict[
            tuple[str, tuple[str, ...]],
            tuple[float, dict[str, dict[str, Any]]],
        ] = {}

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

    def stock_classifications(self, codes: list[str], as_of: str) -> dict[str, dict[str, Any]]:
        """Return as-of DWD industry/concept classifications for several stocks.

        The dashboard uses this bulk path for holdings charts. Eastmoney board
        memberships from ``dwd_dc_index``/``dwd_dc_member`` are split by their real
        concept, industry, and region types. Current-version filters exclude
        superseded DWD rows, and availability dates prevent historical look-ahead.
        """
        requested = sorted({str(code) for code in codes if code})
        if not requested:
            return {}
        cache_key = (as_of, tuple(requested))
        cached = self._classification_cache.get(cache_key)
        now = monotonic()
        if cached and cached[0] > now:
            return cached[1]
        code_literals = ", ".join(quote_literal(code) for code in requested)
        requested_array = f"[{code_literals}]"
        day = quote_literal(as_of)
        current = "toDateTime64('2299-12-31 00:00:00.000', 3)"
        sql = f"""
SELECT
    requested.stock_code,
    sw.sw_industry_l1,
    sw.sw_industry_l2,
    sw.sw_industry_l3,
    concepts.dc_concepts,
    boards.dc_member_concepts,
    boards.dc_member_industries,
    boards.dc_member_regions
FROM (
    SELECT arrayJoin({requested_array}) AS stock_code
) AS requested
LEFT JOIN (
    SELECT
        ts_code,
        argMax(l1_name, in_date) AS sw_industry_l1,
        argMax(l2_name, in_date) AS sw_industry_l2,
        argMax(l3_name, in_date) AS sw_industry_l3
    FROM dwd_sw_industry_member FINAL
    WHERE ts_code IN ({code_literals})
      AND in_date <= toDate({day})
      AND end_date >= toDate({day})
      AND available_date <= toDate({day})
    GROUP BY ts_code
) AS sw ON sw.ts_code = requested.stock_code
LEFT JOIN (
    SELECT
        member.ts_code,
        arraySort(groupUniqArray(ifNull(concept.name, member.theme_code))) AS dc_concepts
    FROM dwd_dc_concept_cons AS member
    LEFT JOIN dwd_dc_concept AS concept
      ON concept.theme_code = member.theme_code
     AND concept.trade_date = member.trade_date
     AND concept.sys_to = {current}
    WHERE member.ts_code IN ({code_literals})
      AND member.sys_to = {current}
      AND (member.ts_code, member.trade_date) IN (
          SELECT ts_code, max(trade_date)
          FROM dwd_dc_concept_cons
          WHERE ts_code IN ({code_literals})
            AND available_trade_date <= toDate({day})
            AND sys_to = {current}
          GROUP BY ts_code
      )
    GROUP BY member.ts_code
) AS concepts ON concepts.ts_code = requested.stock_code
LEFT JOIN (
    SELECT
        member.con_code,
        arraySort(groupUniqArrayIf(
            ifNull(board.name, member.ts_code),
            ifNull(board.idx_type, '') = '概念板块'
              AND ifNull(board.name, '') != '融资融券'
        )) AS dc_member_concepts,
        arraySort(groupUniqArrayIf(
            ifNull(board.name, member.ts_code),
            ifNull(board.idx_type, '') = '行业板块'
        )) AS dc_member_industries,
        arraySort(groupUniqArrayIf(
            ifNull(board.name, member.ts_code),
            ifNull(board.idx_type, '') = '地域板块'
        )) AS dc_member_regions
    FROM dwd_dc_member AS member
    LEFT JOIN dwd_dc_index AS board
      ON board.ts_code = member.ts_code
     AND board.trade_date = member.trade_date
     AND board.sys_to = {current}
    WHERE member.con_code IN ({code_literals})
      AND member.sys_to = {current}
      AND (member.con_code, member.trade_date) IN (
          SELECT con_code, max(trade_date)
          FROM dwd_dc_member
          WHERE con_code IN ({code_literals})
            AND available_trade_date <= toDate({day})
            AND sys_to = {current}
          GROUP BY con_code
      )
    GROUP BY member.con_code
) AS boards ON boards.con_code = requested.stock_code
SETTINGS join_use_nulls = 1
"""
        rows = self.query(sql)
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            code = str(row["stock_code"])
            result[code] = {
                "sw_industry_l1": row.get("sw_industry_l1"),
                "sw_industry_l2": row.get("sw_industry_l2"),
                "sw_industry_l3": row.get("sw_industry_l3"),
                "dc_concepts": row.get("dc_concepts") or [],
                "dc_member_concepts": row.get("dc_member_concepts") or [],
                "dc_member_industries": row.get("dc_member_industries") or [],
                "dc_member_regions": row.get("dc_member_regions") or [],
            }
        # Realtime holdings can refresh every 15 seconds while classifications are
        # daily data. A short bounded cache avoids repeatedly scanning the DWD tables
        # without hiding same-day warehouse updates for long.
        if len(self._classification_cache) >= 64:
            oldest_key = min(
                self._classification_cache,
                key=lambda key: self._classification_cache[key][0],
            )
            self._classification_cache.pop(oldest_key, None)
        self._classification_cache[cache_key] = (now + 300.0, result)
        return result

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

    def stock_info_query(self, stock_code: str, as_of: str) -> str:
        """Latest daily stock metrics on/before as_of, with current basic details.

        The wide table retains Tushare units: market caps in 万元, amount in 千元,
        volume in 手, and rates in percent. Expose CNY, shares and fractional rates
        to the frontend. Industry and concept memberships come from the curated DWD
        tables and only use records available by ``as_of``. Missing/non-finite metrics
        remain NULL, never zero.
        """
        metrics = [
            ("close", "close"),
            ("open", "open"),
            ("high", "high"),
            ("low", "low"),
            ("pre_close", "previous_close"),
            ("pct_chg / 100.0", "daily_change"),
            ("total_mv * 10000.0", "total_market_cap"),
            ("circ_mv * 10000.0", "circulating_market_cap"),
            ("turn_over / 100.0", "turnover_rate"),
            ("turnover_rate_f / 100.0", "free_float_turnover_rate"),
            ("vol_ratio", "volume_ratio"),
            ("pe_ttm", "pe_ttm"),
            ("pb", "pb"),
            ("vol * 100.0", "volume"),
            ("amount * 1000.0", "amount"),
        ]
        select_metrics = ",\n        ".join(
            f"if(isFinite({expression}), {expression}, NULL) AS {alias}"
            for expression, alias in metrics
        )
        metric_names = ", ".join(f"daily.{alias}" for _, alias in metrics)
        return f"""
SELECT
    basic.name AS stock_name, basic.industry, basic.area, basic.market,
    nullIf(basic.list_date, toDate('1970-01-01')) AS listed_date,
    daily.trade_date, {metric_names},
    sw.l1_name AS sw_industry_l1,
    sw.l2_name AS sw_industry_l2,
    sw.l3_name AS sw_industry_l3,
    concepts.dc_concepts,
    boards.dc_boards
FROM (
    SELECT name, industry, area, market, list_date
    FROM stock_basic
    WHERE ts_code = {quote_literal(stock_code)}
    LIMIT 1
) AS basic
LEFT JOIN (
    SELECT trade_date,
        {select_metrics}
    FROM dws_stock_factor_wide
    WHERE source_code = {quote_literal(stock_code)}
      AND trade_date <= toDate({quote_literal(as_of)})
    ORDER BY trade_date DESC
    LIMIT 1
) AS daily ON 1 = 1
LEFT JOIN (
    SELECT l1_name, l2_name, l3_name
    FROM dwd_sw_industry_member FINAL
    WHERE ts_code = {quote_literal(stock_code)}
      AND in_date <= toDate({quote_literal(as_of)})
      AND end_date >= toDate({quote_literal(as_of)})
      AND available_date <= toDate({quote_literal(as_of)})
    ORDER BY in_date DESC
    LIMIT 1
) AS sw ON 1 = 1
LEFT JOIN (
    SELECT arraySort(groupUniqArray(ifNull(concept.name, member.theme_code))) AS dc_concepts
    FROM dwd_dc_concept_cons AS member
    LEFT JOIN dwd_dc_concept AS concept
      ON concept.theme_code = member.theme_code
     AND concept.trade_date = member.trade_date
     AND concept.sys_to = toDateTime64('2299-12-31 00:00:00.000', 3)
    WHERE member.ts_code = {quote_literal(stock_code)}
      AND member.sys_to = toDateTime64('2299-12-31 00:00:00.000', 3)
      AND member.trade_date = (
          SELECT max(trade_date)
          FROM dwd_dc_concept_cons
          WHERE ts_code = {quote_literal(stock_code)}
            AND available_trade_date <= toDate({quote_literal(as_of)})
            AND sys_to = toDateTime64('2299-12-31 00:00:00.000', 3)
      )
) AS concepts ON 1 = 1
LEFT JOIN (
    SELECT arrayFilter(
        value -> value != '融资融券',
        arraySort(groupUniqArray(ifNull(board.name, member.ts_code)))
    ) AS dc_boards
    FROM dwd_dc_member AS member
    LEFT JOIN dwd_dc_index AS board
      ON board.ts_code = member.ts_code
     AND board.trade_date = member.trade_date
     AND board.sys_to = toDateTime64('2299-12-31 00:00:00.000', 3)
    WHERE member.con_code = {quote_literal(stock_code)}
      AND member.sys_to = toDateTime64('2299-12-31 00:00:00.000', 3)
      AND member.trade_date = (
          SELECT max(trade_date)
          FROM dwd_dc_member
          WHERE con_code = {quote_literal(stock_code)}
            AND available_trade_date <= toDate({quote_literal(as_of)})
            AND sys_to = toDateTime64('2299-12-31 00:00:00.000', 3)
)
) AS boards ON 1 = 1
SETTINGS join_use_nulls = 1
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
