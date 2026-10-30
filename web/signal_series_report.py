"""Per-stock signal rank/score time series (ClickHouse-backed, per-account).

Backs the 信号R线 dashboard view: for one stock over a date window, the daily model
``score`` and its daily ``rank`` within the whole cross-section. ``rank`` is NOT a stored
column — it is ``row_number() OVER (PARTITION BY pred_date ORDER BY score DESC, stock_code
ASC)``, matching how the offline ranking and ``signal_quality_report`` derive it. Ranking
therefore runs over the FULL daily cross-section first, and only then filters to the target
stock (a single stock cannot be ranked in isolation).

The predictions database and table are per-account and come from a live node
(``/strategy/info`` → ``predictions_database`` / ``predictions_table``); they are
untrusted and quoted via ``quote_qualified_table``. Dates are quoted via
``quote_literal``. The table stores ``stock_code`` in exchange-prefix form
(``SZ302132``); it is converted to factor form (``302132.SZ``) in-SQL exactly as
``signal_quality_report`` does, and the target stock is matched against that factor form.
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from typing import Any

from web.db import quote_qualified_table, quote_literal

# Per-row column order returned by build_signal_series_sql.
SIGNAL_SERIES_COLUMNS = ["date", "rank", "score", "signal_date"]

# Per-row column order returned by build_snapshot_signals_sql.
SNAPSHOT_SIGNALS_COLUMNS = ["rank", "stock_code", "score", "pred_return_live"]

RELATION_EXPLICIT = "explicit"
RELATION_TARGET = "target"
RELATION_INFERRED = "inferred"
RELATION_UNLINKED = "unlinked"


def build_signal_series_sql(
    database: str | None,
    table: str,
    stock_code: str,
    start: str,
    end: str,
) -> str:
    """Build the per-stock rank/score series on each signal's effective date.

    ``database``/``table`` are untrusted (from a live node) and quoted via
    ``quote_qualified_table``;
    ``start``/``end``/``stock_code`` via ``quote_literal``. ``stock_code`` is the factor
    form (``000001.SZ``) as used elsewhere in the dashboard — matched against the in-SQL
    conversion of the table's exch-prefix ``stock_code``. ``date`` is the next open
    SSE trading date after the prediction date; the prediction date itself is returned
    as ``signal_date``.
    """
    tbl = quote_qualified_table(database, table)
    return f"""
WITH calendar AS (
    SELECT DISTINCT cal_date
    FROM dwd_trade_calendar
    WHERE exchange = 'SSE'
      AND is_open = 1
      AND cal_date >= toDate({quote_literal(start)}) - INTERVAL 20 DAY
      AND cal_date <= toDate({quote_literal(end)}) + INTERVAL 20 DAY
),
ranked AS (
    SELECT
        pred_date,
        concat(substring(stock_code, 3), '.', substring(stock_code, 1, 2)) AS code,
        score,
        row_number() OVER (PARTITION BY pred_date ORDER BY score DESC, stock_code ASC) AS rank
    FROM {tbl}
    WHERE pred_date >= toDate({quote_literal(start)}) - INTERVAL 20 DAY
      AND pred_date <= toDate({quote_literal(end)}) + INTERVAL 20 DAY
)
,
effective AS (
    SELECT
        r.pred_date AS signal_date,
        r.rank,
        r.score,
        min(c.cal_date) AS date
    FROM ranked r
    INNER JOIN calendar c ON c.cal_date > r.pred_date
    WHERE r.code = {quote_literal(stock_code)}
    GROUP BY r.pred_date, r.rank, r.score
)
SELECT date, rank, score, signal_date
FROM effective
WHERE date BETWEEN toDate({quote_literal(start)}) AND toDate({quote_literal(end)})
ORDER BY date
""".strip()


def build_snapshot_signals_sql(database: str | None, table: str, date: str) -> str:
    """Build the ranked signal cross-section for a single ``pred_date`` (ClickHouse).

    Returns every stock's ``rank`` (by score within that day), factor-form ``stock_code``,
    ``score`` and ``pred_return_live`` for the given date, ordered by rank. This is the
    historical warehouse view behind 按日快照 → 信号 (not the live node's in-memory signals).

    ``database``/``table`` are untrusted (from a live node) and quoted via
    ``quote_qualified_table``; ``date`` via ``quote_literal``.
    """
    tbl = quote_qualified_table(database, table)
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


def resolve_trade_signal_relations(
    trades: list[dict[str, Any]],
    series: list[dict[str, Any]],
    target_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach each trade to the signal date that drove it, when that is knowable.

    Preference order is explicit ``live_order.signal_date``, an unambiguous
    ``live_target_portfolio`` mapping, then a clearly-labeled historical inference from
    the latest prediction date strictly before the trade date. The inference exists
    only for rows written before order-level signal lineage was persisted; it is not
    authoritative and is reported as ``inferred``.
    """
    effect_dates_by_signal = {
        _date_text(row.get("signal_date")): _date_text(row.get("date"))
        for row in series
        if _date_text(row.get("signal_date")) is not None
    }
    signal_dates = sorted(
        {
            value
            for value in (_date_text(row.get("signal_date") or row.get("date")) for row in series)
            if value is not None
        }
    )
    target_dates_by_version: dict[tuple[str, str, str], set[str]] = {}
    target_dates_by_stock: dict[tuple[str, str], set[str]] = {}
    for row in target_rows:
        trade_date = _date_text(row.get("trade_date"))
        stock_code = row.get("stock_code")
        signal_date = _date_text(row.get("signal_date"))
        target_version = row.get("target_version")
        if trade_date is None or not stock_code or signal_date is None:
            continue
        stock_key = (trade_date, str(stock_code))
        target_dates_by_stock.setdefault(stock_key, set()).add(signal_date)
        if target_version:
            version_key = (*stock_key, str(target_version))
            target_dates_by_version.setdefault(version_key, set()).add(signal_date)

    resolved: list[dict[str, Any]] = []
    for trade in trades:
        result = dict(trade)
        trade_date = _date_text(trade.get("trade_date"))
        if trade_date is None:
            result["signal_date"] = None
            result["effect_date"] = None
            result["relation_status"] = RELATION_UNLINKED
            result["relation_source"] = None
            resolved.append(result)
            continue
        result["trade_date"] = trade_date

        explicit_signal = _date_text(trade.get("order_signal_date"))
        if explicit_signal is not None:
            result["signal_date"] = explicit_signal
            result["effect_date"] = effect_dates_by_signal.get(explicit_signal)
            result["relation_status"] = RELATION_EXPLICIT
            result["relation_source"] = "live_order"
            resolved.append(result)
            continue

        stock_code = str(trade.get("stock_code") or "")
        target_version = trade.get("target_version")
        target_signal: str | None = None
        if stock_code and target_version:
            candidates = target_dates_by_version.get((trade_date, stock_code, str(target_version)), set())
            if len(candidates) == 1:
                target_signal = next(iter(candidates))
        if target_signal is None and stock_code:
            candidates = target_dates_by_stock.get((trade_date, stock_code), set())
            if len(candidates) == 1:
                target_signal = next(iter(candidates))
        if target_signal is not None:
            result["signal_date"] = target_signal
            result["effect_date"] = effect_dates_by_signal.get(target_signal)
            result["relation_status"] = RELATION_TARGET
            result["relation_source"] = "live_target_portfolio"
            resolved.append(result)
            continue

        prior_signals = [value for value in signal_dates if value < trade_date]
        if prior_signals:
            prior_signal = prior_signals[-1]
            result["signal_date"] = prior_signal
            result["effect_date"] = effect_dates_by_signal.get(prior_signal)
            result["relation_status"] = RELATION_INFERRED
            result["relation_source"] = "prior_prediction_date"
        else:
            result["signal_date"] = None
            result["effect_date"] = None
            result["relation_status"] = RELATION_UNLINKED
            result["relation_source"] = None
        resolved.append(result)
    return resolved


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text[:10] if text else None
