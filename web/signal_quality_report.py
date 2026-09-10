"""Parameterized signal-quality report (ClickHouse-backed, per-account).

This is a ClickHouse SQL port of the **daily cross-section + TopN-label block** of the
reference implementation
``/nfs_data/wangzichao/workspace/quant_data_ana_service/scripts/compute_universal_offline_metrics_20260820.py``.
It measures how well a model's per-day ``score`` predicts a realized forward return
(the *label*) for the predictions table that a live node reports via ``/strategy/info``.

The label and every metric are defined to match that reference EXACTLY:

  * **Label (forward return), holding_days = h (reference default 3):** buy at the
    ``t+1`` adjusted open, sell at the ``t+(h+1)`` adjusted open —
    ``label = Ref(open*adj_factor, -(h+1)) / Ref(open*adj_factor, -1) - 1``.
    Adjusted price = ``open * adj_factor``. Open-to-open, adjusted — NOT close-to-close
    ``pct_chg``.
  * **IC** = per-day Pearson ``corr(score, label)``; **RankIC** = per-day Spearman
    ``rankCorr(score, label)``. ``safe_corr`` rule: NULL when the day has ``< 3`` names
    (a ``< 2`` unique-value day yields a NULL corr from ClickHouse anyway).
  * **LS10** = per day, ``mean(label of top score-decile) − mean(label of bottom
    score-decile)`` where ``layer_n = max(count // 10, 1)``; ties broken by score desc
    then code asc.
  * **TopN (20, 50):** ``topN_label_return`` = mean label of the top-N by score (NULL
    when ``count < N``); ``topN_label_excess = topN_label_return − benchmark_label_return``.
  * **Benchmark** = ``000985.CSI`` (中证全指) **open**, same window:
    ``open[t+h+1] / open[t+1] − 1`` (source ``index_daily``, ``argMax(open, _ingest_time)``
    per ``trade_date``).

**ICIR / RankICIR / positive-ratios are period-level scalars** (``mean(daily ic) /
std(daily ic)``, ``share of days > 0``), matching the reference's
``sub.ic.mean() / sub.ic.std()`` aggregation. They are NOT per-day columns — the
frontend footer computes them from the visible per-day rows (see ``app.js``
``buildSignalQualitySummary``), so the two stay consistent.

We deliberately implement only this daily-cross-section + TopN-label block. The
reference's staggered holding-period portfolio, size-decile distribution, and
qlib-provider path need the qlib provider / RQAlpha and are out of scope for a live
dashboard report.

IF THE REFERENCE CHANGES the label definition, correlation rules, LS10 decile rule, or
TopN/IR aggregation, UPDATE THIS MODULE (and the footer aggregation in ``app.js``) IN
LOCKSTEP.
"""

from __future__ import annotations

from typing import Any

from web.db import ClickHouseClient, quote_identifier, quote_literal

# Benchmark index for the excess-return columns (中证全指).
BENCHMARK_TS_CODE = "000985.CSI"

# Forward-window choices offered in the UI / accepted by the API. Inlined as ints into
# the SQL (ClickHouse rejects a Nullable/subquery leadInFrame offset), so the set is
# closed and validated by the caller.
ALLOWED_HOLDING_DAYS = (1, 3, 5)

# Per-day column order returned by build_signal_quality_sql. ``trade_date`` is aliased
# from ``pred_date`` so 随时间/对比 align on the same key as the other datasets.
SIGNAL_QUALITY_COLUMNS = [
    "trade_date",
    "rankic",
    "ic",
    "ls10",
    "top20_label_return",
    "top50_label_return",
    "top20_label_excess",
    "top50_label_excess",
    "benchmark_label_return",
    "sample_count",
]


def build_signal_quality_sql(
    table: str,
    start_date: str,
    end_date: str,
    holding_days: int,
) -> str:
    """Build the per-day signal-quality ClickHouse SQL.

    ``table`` is untrusted (it comes from a live node) and is quoted via
    ``quote_identifier``; ``start_date``/``end_date`` via ``quote_literal``.
    ``holding_days`` MUST already be validated as one of ``ALLOWED_HOLDING_DAYS`` — it is
    inlined as an int (offsets ``1`` and ``h+1``, frame ``h+1``, gate ``h+2``).
    """
    if holding_days not in ALLOWED_HOLDING_DAYS:
        raise ValueError(f"holding_days must be one of {ALLOWED_HOLDING_DAYS}: {holding_days!r}")

    tbl = quote_identifier(table)
    start = quote_literal(start_date)
    end = quote_literal(end_date)
    bench = quote_literal(BENCHMARK_TS_CODE)
    h = int(holding_days)
    entry_off = 1          # buy at t+1 open
    exit_off = h + 1       # sell at t+(h+1) open
    frame_off = h + 1      # forward frame reaches the exit row
    full_window = h + 2    # a row + its (h+1) forward rows are all present

    return f"""
WITH
adjpx AS (
    -- Adjusted open per stock; buffered [start-15d, end+20d] so the trailing forward
    -- window (and the benchmark alignment) always have rows to lead into.
    SELECT
        source_code AS code,
        trade_date,
        open * adj_factor AS aopen
    FROM {quote_identifier("dws_stock_factor_wide")}
    WHERE trade_date >= toDate({start}) - 15
      AND trade_date <= toDate({end}) + 20
      AND open > 0
      AND adj_factor IS NOT NULL
),
lab AS (
    -- Per-stock forward window: entry = t+1 open, exit = t+(h+1) open. ``avail`` gates
    -- rows that lack a full forward window (edge of the series).
    SELECT
        code,
        trade_date,
        leadInFrame(aopen, {entry_off}) OVER w AS entry_px,
        leadInFrame(aopen, {exit_off}) OVER w AS exit_px,
        count() OVER w AS avail
    FROM adjpx
    WINDOW w AS (
        PARTITION BY code
        ORDER BY trade_date
        ROWS BETWEEN CURRENT ROW AND {frame_off} FOLLOWING
    )
),
bench_px AS (
    SELECT
        trade_date,
        argMax(open, _ingest_time) AS bopen
    FROM {quote_identifier("index_daily")}
    WHERE ts_code = {bench}
      AND trade_date >= toDate({start}) - 15
      AND trade_date <= toDate({end}) + 20
    GROUP BY trade_date
),
bench AS (
    -- Same open-to-open forward window applied to the benchmark index.
    SELECT
        trade_date,
        leadInFrame(bopen, {entry_off}) OVER wb AS b_entry,
        leadInFrame(bopen, {exit_off}) OVER wb AS b_exit,
        count() OVER wb AS b_avail
    FROM bench_px
    WINDOW wb AS (
        ORDER BY trade_date
        ROWS BETWEEN CURRENT ROW AND {frame_off} FOLLOWING
    )
),
preds AS (
    -- stock_code 'SZ302132' (exch-prefix, no dot) -> factor form '302132.SZ'.
    SELECT
        concat(substring(stock_code, 3), '.', substring(stock_code, 1, 2)) AS code,
        pred_date,
        score
    FROM {tbl}
    WHERE pred_date BETWEEN toDate({start}) AND toDate({end})
),
joined AS (
    SELECT
        p.pred_date AS pred_date,
        p.code      AS code,
        p.score     AS score,
        l.exit_px / l.entry_px - 1 AS label
    FROM preds p
    INNER JOIN lab l
        ON p.code = l.code AND p.pred_date = l.trade_date
    WHERE l.avail = {full_window}
      AND l.entry_px > 0
),
ranked AS (
    SELECT
        pred_date,
        code,
        score,
        label,
        row_number() OVER (PARTITION BY pred_date ORDER BY score DESC, code ASC) AS rnk,
        count() OVER (PARTITION BY pred_date) AS cnt,
        greatest(intDiv(count() OVER (PARTITION BY pred_date), 10), 1) AS layer_n
    FROM joined
),
flagged AS (
    -- Decile membership as per-row flags: avgIf's condition cannot reference a
    -- group-level aggregate, but layer_n/cnt are constant within a pred_date, so we
    -- fix membership here and aggregate over the plain flag.
    SELECT
        pred_date,
        score,
        label,
        rnk,
        cnt,
        rnk <= layer_n              AS is_top_decile,
        rnk > cnt - layer_n         AS is_bot_decile
    FROM ranked
)
SELECT
    f.pred_date                                            AS trade_date,
    if(count() < 3, NULL, rankCorr(score, label))          AS rankic,
    if(count() < 3, NULL, corr(score, label))              AS ic,
    avgIf(label, is_top_decile) - avgIf(label, is_bot_decile) AS ls10,
    if(any(cnt) < 20, NULL, avgIf(label, rnk <= 20))       AS top20_label_return,
    if(any(cnt) < 50, NULL, avgIf(label, rnk <= 50))       AS top50_label_return,
    any(b.b_exit / b.b_entry - 1)                          AS benchmark_label_return,
    if(any(cnt) < 20, NULL, avgIf(label, rnk <= 20) - any(b.b_exit / b.b_entry - 1)) AS top20_label_excess,
    if(any(cnt) < 50, NULL, avgIf(label, rnk <= 50) - any(b.b_exit / b.b_entry - 1)) AS top50_label_excess,
    count()                                                AS sample_count
FROM flagged f
LEFT JOIN bench b ON f.pred_date = b.trade_date
GROUP BY f.pred_date
ORDER BY trade_date
"""


def query_signal_quality(
    clickhouse: ClickHouseClient,
    *,
    table: str,
    start_date: str,
    end_date: str,
    holding_days: int,
) -> list[dict[str, Any]]:
    """Run the signal-quality SQL and return per-day rows (already JSON-native)."""
    sql = build_signal_quality_sql(table, start_date, end_date, holding_days)
    return clickhouse.query(sql)
