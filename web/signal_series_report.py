"""Per-stock signal rank/score time series (ClickHouse-backed, per-account).

Backs the 信号R线 dashboard view: for one stock over a date window, the daily model
``score`` and its daily ``rank`` within the whole cross-section. ``rank`` is NOT a stored
column — it is ``row_number() OVER (PARTITION BY pred_date ORDER BY score DESC, stock_code
ASC)``, matching how the offline ranking and ``signal_quality_report`` derive it. Ranking
therefore runs over the FULL daily cross-section first, and only then filters to the target
stock (a single stock cannot be ranked in isolation).

The predictions table is per-account and comes from a live node (``/strategy/info`` →
``predictions_table``); it is untrusted and quoted via ``quote_identifier``. Dates are
quoted via ``quote_literal``. The table stores ``stock_code`` in exchange-prefix form
(``SZ302132``); it is converted to factor form (``302132.SZ``) in-SQL exactly as
``signal_quality_report`` does, and the target stock is matched against that factor form.
"""

from __future__ import annotations

from web.db import quote_identifier, quote_literal

# Per-row column order returned by build_signal_series_sql.
SIGNAL_SERIES_COLUMNS = ["date", "rank", "score"]

# Per-row column order returned by build_snapshot_signals_sql.
SNAPSHOT_SIGNALS_COLUMNS = ["rank", "stock_code", "score", "pred_return_live"]


def build_signal_series_sql(table: str, stock_code: str, start: str, end: str) -> str:
    """Build the per-stock rank/score time-series ClickHouse SQL.

    ``table`` is untrusted (from a live node) and quoted via ``quote_identifier``;
    ``start``/``end``/``stock_code`` via ``quote_literal``. ``stock_code`` is the factor
    form (``000001.SZ``) as used elsewhere in the dashboard — matched against the in-SQL
    conversion of the table's exch-prefix ``stock_code``.
    """
    tbl = quote_identifier(table)
    return f"""
WITH ranked AS (
    SELECT
        pred_date,
        concat(substring(stock_code, 3), '.', substring(stock_code, 1, 2)) AS code,
        score,
        row_number() OVER (PARTITION BY pred_date ORDER BY score DESC, stock_code ASC) AS rank
    FROM {tbl}
    WHERE pred_date BETWEEN toDate({quote_literal(start)}) AND toDate({quote_literal(end)})
)
SELECT pred_date AS date, rank, score
FROM ranked
WHERE code = {quote_literal(stock_code)}
ORDER BY date
""".strip()


def build_snapshot_signals_sql(table: str, date: str) -> str:
    """Build the ranked signal cross-section for a single ``pred_date`` (ClickHouse).

    Returns every stock's ``rank`` (by score within that day), factor-form ``stock_code``,
    ``score`` and ``pred_return_live`` for the given date, ordered by rank. This is the
    historical warehouse view behind 按日快照 → 信号 (not the live node's in-memory signals).

    ``table`` is untrusted (from a live node) and quoted via ``quote_identifier``; ``date``
    via ``quote_literal``.
    """
    tbl = quote_identifier(table)
    return f"""
SELECT
    row_number() OVER (ORDER BY score DESC, stock_code ASC) AS rank,
    concat(substring(stock_code, 3), '.', substring(stock_code, 1, 2)) AS stock_code,
    score,
    pred_return_live
FROM {tbl}
WHERE pred_date = toDate({quote_literal(date)})
ORDER BY rank
""".strip()
