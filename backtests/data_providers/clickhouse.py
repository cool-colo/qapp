from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen

from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity

from backtests.data_providers.base import BarDataProvider
from backtests.data_providers.base import PreparedBarData


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NANOS_PER_SECOND = 1_000_000_000
NANOS_PER_MILLI = 1_000_000
NANOS_PER_MICRO = 1_000


@dataclass(frozen=True)
class ClickHouseConnectionConfig:
    url: str = "http://127.0.0.1:8123"
    database: str | None = None
    user: str | None = "default"
    password: str | None = None
    timeout_secs: float = 30.0


@dataclass(frozen=True)
class ClickHouseBarSchema:
    table: str = "dws_stock_factor_wide"
    symbol_column: str = "instrument_id"
    timestamp_column: str = "trade_date"
    open_column: str = "open"
    high_column: str = "high"
    low_column: str = "low"
    close_column: str = "close"
    volume_column: str = "vol"
    period_column: str | None = None
    period_value: str | None = None


class ClickHouseBarDataProvider(BarDataProvider):
    """
    Reusable ClickHouse-backed provider for Nautilus bar backtests.

    Backtest scripts should depend on this provider rather than embedding SQL
    and row-to-Bar conversion logic.
    """

    def __init__(
        self,
        connection: ClickHouseConnectionConfig,
        schema: ClickHouseBarSchema,
    ) -> None:
        if bool(schema.period_column) != bool(schema.period_value):
            raise ValueError("period_column and period_value must be provided together")
        self.connection = connection
        self.schema = schema

    def prepare_bars(
        self,
        symbol: str,
        bar_type: BarType,
        start: str,
        end: str,
        timezone_name: str = "UTC",
        price_precision: int = 2,
        strict_data: bool = False,
        limit: int = 0,
    ) -> PreparedBarData:
        rows = self.fetch_bar_rows(symbol=symbol, start=start, end=end, limit=limit)
        bars, skipped = self.build_bars(
            rows=rows,
            bar_type=bar_type,
            timezone_name=timezone_name,
            price_precision=price_precision,
            strict_data=strict_data,
        )
        return PreparedBarData(
            bar_type=bar_type,
            bars=bars,
            skipped_rows=skipped,
        )

    def fetch_bar_rows(
        self,
        symbol: str,
        start: str,
        end: str,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        query = self.bar_query(symbol=symbol, start=start, end=end, limit=limit)
        return self.fetch_json_each_row(query)

    def preview_request(
        self,
        symbol: str,
        start: str,
        end: str,
        limit: int = 0,
    ) -> str:
        return self.bar_query(symbol=symbol, start=start, end=end, limit=limit)

    def bar_query(self, symbol: str, start: str, end: str, limit: int = 0) -> str:
        schema = self.schema
        ts_col = quote_identifier(schema.timestamp_column)
        where = [
            f"{quote_identifier(schema.symbol_column)} = {quote_literal(symbol)}",
            f"{ts_col} >= parseDateTimeBestEffort({quote_literal(start)})",
            f"{ts_col} < parseDateTimeBestEffort({quote_literal(end)})",
        ]
        if schema.period_column and schema.period_value:
            where.append(
                f"{quote_identifier(schema.period_column)} = {quote_literal(schema.period_value)}",
            )

        limit_clause = f"\nLIMIT {limit:d}" if limit > 0 else ""
        sql = f"""
SELECT
    {ts_col} AS ts,
    {quote_identifier(schema.open_column)} AS open,
    {quote_identifier(schema.high_column)} AS high,
    {quote_identifier(schema.low_column)} AS low,
    {quote_identifier(schema.close_column)} AS close,
    {quote_identifier(schema.volume_column)} AS volume
FROM {quote_identifier(schema.table)}
WHERE {" AND ".join(where)}
ORDER BY {ts_col} ASC{limit_clause}
"""
        return ensure_json_each_row(sql)

    def fetch_json_each_row(self, sql: str) -> list[dict[str, Any]]:
        params = {}
        if self.connection.database:
            params["database"] = self.connection.database
        url = self.connection.url.rstrip("/")
        if params:
            url = f"{url}?{urlencode(params)}"

        headers = {
            "Content-Type": "text/plain; charset=utf-8",
        }
        if self.connection.user:
            headers["X-ClickHouse-User"] = self.connection.user
        if self.connection.password:
            headers["X-ClickHouse-Key"] = self.connection.password

        request = Request(
            url=url,
            data=sql.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.connection.timeout_secs) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ClickHouse HTTP {exc.code}: {body[:1000]}") from exc
        except URLError as exc:
            raise RuntimeError(f"ClickHouse request failed: {exc}") from exc

        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    @staticmethod
    def build_bars(
        rows: list[dict[str, Any]],
        bar_type: BarType,
        timezone_name: str,
        price_precision: int,
        strict_data: bool,
    ) -> tuple[list[Bar], int]:
        bars = []
        skipped = 0
        for index, row in enumerate(rows, start=1):
            try:
                prices = {
                    name: Decimal(str(row[name]))
                    for name in ("open", "high", "low", "close")
                }
                if min(prices.values()) <= 0:
                    raise ValueError(f"non-positive price in row {index}: {row}")
                ts_event = timestamp_to_nanos(row["ts"], timezone_name)
                volume = int(Decimal(str(row.get("volume", 0) or 0)))
                bars.append(
                    Bar(
                        bar_type=bar_type,
                        open=Price.from_str(
                            decimal_to_fixed(row["open"], price_precision),
                        ),
                        high=Price.from_str(
                            decimal_to_fixed(row["high"], price_precision),
                        ),
                        low=Price.from_str(
                            decimal_to_fixed(row["low"], price_precision),
                        ),
                        close=Price.from_str(
                            decimal_to_fixed(row["close"], price_precision),
                        ),
                        volume=Quantity.from_int(volume),
                        ts_event=ts_event,
                        ts_init=ts_event,
                    ),
                )
            except Exception:
                if strict_data:
                    raise
                skipped += 1

        bars.sort(key=lambda bar: bar.ts_init)
        return bars, skipped


def quote_identifier(identifier: str) -> str:
    parts = [part.strip() for part in identifier.split(".")]
    if not parts or any(not IDENTIFIER_RE.match(part) for part in parts):
        raise ValueError(f"Unsafe ClickHouse identifier: {identifier!r}")
    return ".".join(f"`{part}`" for part in parts)


def quote_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def ensure_json_each_row(sql: str) -> str:
    stripped = sql.strip().rstrip(";")
    if re.search(r"\bFORMAT\s+JSONEachRow\b", stripped, flags=re.IGNORECASE):
        return stripped
    return f"{stripped}\nFORMAT JSONEachRow"


def timestamp_to_nanos(value: Any, timezone_name: str) -> int:
    if value is None:
        raise ValueError("missing timestamp")

    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        number = int(value)
        if number > 10_000_000_000_000_000:
            return number
        if number > 10_000_000_000_000:
            return number * NANOS_PER_MICRO
        if number > 10_000_000_000:
            return number * NANOS_PER_MILLI
        return number * NANOS_PER_SECOND

    import pandas as pd

    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(timezone_name)
    return int(timestamp.tz_convert("UTC").value)


def decimal_to_fixed(value: Any, precision: int) -> str:
    decimal_value = Decimal(str(value))
    quantum = Decimal("1").scaleb(-precision)
    return format(decimal_value.quantize(quantum), "f")
