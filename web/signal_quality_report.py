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

# ---------------------------------------------------------------------------
# Trailing-edge label fallbacks (dashboard-only approximations)
# ---------------------------------------------------------------------------
# The reference metric's label is strictly the adjusted open-to-open forward
# return over the full h-window; the newest ``h+1`` signal dates have no such
# window yet and the reference simply drops them. For a live dashboard those
# freshest dates are the most interesting, so we approximate their label and
# flag it via ``label_source`` (precedence: normal > t1_intraday > realtime):
#
#   normal            full h-window open-to-open return (exact, reference port)
#   t1_intraday       buy day (T+1) open from the offline factor row, EXIT at the
#                     stock's CURRENT live node price. Both legs are put on the 后复权
#                     basis with dwd_stock_adj_factor (which carries today's factor
#                     intraday, unlike dws_stock_factor_wide) so a dividend/split
#                     between the buy day and today does not leak into the label:
#                     (rt_exit * af_today) / (t1_open * af_buyday) - 1.
#                     rt_exit is today's LAST price for a record whose intended sell
#                     session is still in the future, but today's OPEN (rt_last as
#                     fallback) for the single record whose sell session IS today —
#                     that record's sell leg is exactly today's open, so using it
#                     matches the exact open-to-open 'normal' definition it is about
#                     to become. Needs the node snapshot; without it, falls back to
#                     the offline T+1 (close/open - 1).
#   realtime_intraday today's intraday return (last_price/open - 1) from the node,
#                     both legs from the node snapshot (buy day == today, no offline
#                     row yet).
#
# (x/open - 1) is adj_factor-invariant, so the T+1 open needs no adjustment; the
# live last_price is a raw quote, on the same scale as that raw open.

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
    "label_source",
]


def _realtime_values_sql(rows: list[tuple[str, float, float]]) -> str:
    """Inline the node's realtime ticks as a CH ``values(...)`` literal table.

    Each row is ``(code, rt_open, rt_last)`` with ``code`` already in factor form
    (``302132.SZ``). Codes are quoted via ``quote_literal``; numbers are formatted
    as plain floats. Caller guarantees a non-empty list.
    """
    tuples = ", ".join(
        f"({quote_literal(code)}, {float(rt_open)!r}, {float(rt_last)!r})"
        for code, rt_open, rt_last in rows
    )
    return (
        "SELECT code, rt_open, rt_last FROM "
        "values('code String, rt_open Float64, rt_last Float64', "
        f"{tuples})"
    )


def build_signal_quality_sql(
    table: str,
    start_date: str,
    end_date: str,
    holding_days: int,
    realtime_ticks: dict[str, Any] | None = None,
) -> str:
    """Build the per-day signal-quality ClickHouse SQL.

    ``table`` is untrusted (it comes from a live node) and is quoted via
    ``quote_identifier``; ``start_date``/``end_date`` via ``quote_literal``.
    ``holding_days`` MUST already be validated as one of ``ALLOWED_HOLDING_DAYS`` — it is
    inlined as an int (offsets ``1`` and ``h+1``, frame ``h+1``, gate ``h+2``).

    ``realtime_ticks`` (optional) supplies the node's whole-market snapshot for the
    rule-2 fallback: ``{"date": "YYYY-MM-DD", "rows": [(code, open, last_price), …]}``.
    Its ``date`` is the node's clock (TODAY / the buy day); its intraday
    ``last_price/open - 1`` label is applied to the signal date whose buy day is today
    (the latest ``pred_date`` before ``date``), so a stale snapshot can't relabel older
    dates. The ``t1_intraday`` label puts its buy-day and today legs on the 后复权 basis
    via ``dwd_stock_adj_factor`` (``af_buyday`` at the buy day, ``af_today`` at ``date``)
    so a dividend/split in between does not distort it.
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

    # --- rule-2 realtime CTE (optional) -------------------------------------
    # The node's snapshot is TODAY's intraday tick (``rt_date`` = the node clock /
    # buy day). It labels the signal date whose buy day is today — i.e. the latest
    # prediction date strictly before ``rt_date`` (that date's t+1 open == today, so
    # its intraday label is ``last_price(today)/open(today) - 1``). We resolve that
    # signal date from the predictions themselves (``max(pred_date) < rt_date``)
    # rather than assuming ``rt_date - 1`` calendar day, and apply the tick only
    # there so a stale snapshot can't relabel older dates.
    rt_rows = (realtime_ticks or {}).get("rows") or []
    rt_date = (realtime_ticks or {}).get("date")
    if rt_rows and rt_date:
        rt_date_lit = quote_literal(str(rt_date))

        # --- today's adj_factor CTEs ----------------------------------------
        # A t1_intraday label buys at the offline RAW t+1 open (buy day) and exits
        # at the stock's CURRENT live quote (today). Both legs are raw, so a
        # dividend/split between the buy day and today puts them on different price
        # scales and the ex-jump leaks into the label. We put both legs on the
        # 后复权 basis with the same cumulative adj_factor the 'normal' label uses:
        #   label = (rt_last * af_today) / (t1_open * af_buyday) - 1
        # ``dwd_stock_adj_factor`` (unlike dws_stock_factor_wide) is built intraday,
        # so it carries TODAY's factor — the piece the offline factor table lacks
        # mid-session. ``sys_to = '2299-12-31 00:00:00.000'`` selects the current
        # (open-ended) version, one row per (code, trade_date).
        adj_factor_cte = f"""
adj_now AS (
    -- Today's (node clock) cumulative adj_factor per stock. Intraday-available.
    SELECT
        source_code AS code,
        adj_factor  AS af_today
    FROM {quote_identifier("dwd_stock_adj_factor")}
    WHERE trade_date = toDate({rt_date_lit})
      AND sys_to = '2299-12-31 00:00:00.000'
      AND adj_factor IS NOT NULL
),
buyday_af AS (
    -- adj_factor at each prediction's buy day (t1_date), per (code, pred_date).
    SELECT
        p.code      AS code,
        p.pred_date AS pred_date,
        a.adj_factor AS af_buyday
    FROM preds p
    INNER JOIN lab l
        ON p.code = l.code AND p.pred_date = l.trade_date
    INNER JOIN {quote_identifier("dwd_stock_adj_factor")} a
        ON a.source_code = p.code AND a.trade_date = l.t1_date
    WHERE a.sys_to = '2299-12-31 00:00:00.000'
      AND a.adj_factor IS NOT NULL
),"""
        adj_factor_join = """
    LEFT JOIN adj_now an
        ON p.code = an.code
    LEFT JOIN buyday_af ba
        ON p.code = ba.code AND p.pred_date = ba.pred_date"""

        realtime_cte = f"""
realtime AS (
    -- Node whole-market snapshot (today's intraday tick). last_price/open - 1.
    {_realtime_values_sql(rt_rows)}
),
rt_signal_date AS (
    -- The signal date whose buy day is today: latest pred_date < the node's date.
    SELECT max(pred_date) AS sig_date FROM preds WHERE pred_date < toDate({rt_date_lit})
),
cal AS (
    -- Market trading calendar (stock-agnostic distinct dates). We UNION in the node's
    -- clock ``rt_date`` (a known live trading session) because the offline factor
    -- table has NOT published today's row during the session — the exact window this
    -- feature serves. Without today in the calendar, leadInFrame from the buy day
    -- cannot reach today and the sell-day-is-today record would never be flagged.
    SELECT DISTINCT trade_date FROM (
        SELECT trade_date FROM {quote_identifier("dws_stock_factor_wide")}
        WHERE trade_date >= toDate({start}) - 15
          AND trade_date <= toDate({end}) + 20
          AND open > 0
          AND adj_factor IS NOT NULL
        UNION DISTINCT
        SELECT toDate({rt_date_lit}) AS trade_date
    )
),
cal_sell AS (
    -- For each prediction date, the intended sell session = t+(h+1) trading day.
    -- A pred whose sell session has not occurred yet gets the leadInFrame default
    -- (1970-01-01), which never equals rt_date, so only the true sell-day-is-today
    -- record is flagged.
    SELECT
        trade_date  AS pred_date,
        leadInFrame(trade_date, {exit_off}) OVER wc AS sell_date
    FROM cal
    WINDOW wc AS (ORDER BY trade_date ROWS BETWEEN CURRENT ROW AND {exit_off} FOLLOWING)
),{adj_factor_cte}"""
        # LEFT JOIN into preds keyed on code, only for the resolved signal date.
        realtime_join = f"""
    LEFT JOIN realtime rt
        ON p.code = rt.code AND p.pred_date = (SELECT sig_date FROM rt_signal_date)
    LEFT JOIN realtime rtl
        ON p.code = rtl.code
    LEFT JOIN cal_sell cs
        ON p.pred_date = cs.pred_date{adj_factor_join}"""
        realtime_label_expr = "if(rt.rt_open > 0, rt.rt_last / rt.rt_open - 1, NULL)"
        # For t1_intraday rows: entry = offline RAW t+1 open on the buy-day 后复权
        # basis (t1_open * af_buyday), exit = live raw quote on today's basis
        # (rt_exit * af_today), both factors from dwd_stock_adj_factor.
        #
        # rt_exit: the record whose intended SELL session is today (cs.sell_date ==
        # rt_date) is one open-to-open leg short of a full 'normal' window — its sell
        # leg IS today's open. So it exits at today's OPEN (rtl.rt_open), matching the
        # exact open-to-open definition; rtl.rt_last is only a fallback if the open is
        # missing. Every other t1_intraday record's sell session is still in the future
        # (no open exists yet), so it exits at the live last price (rtl.rt_last).
        rt_exit_expr = (
            "if(cs.sell_date = toDate({rt_date_lit}) AND rtl.rt_open > 0, rtl.rt_open, rtl.rt_last)"
        ).format(rt_date_lit=rt_date_lit)
        t1_label_expr = (
            f"if(l.t1_open > 0 AND rtl.rt_last > 0 AND an.af_today > 0 AND ba.af_buyday > 0, "
            f"({rt_exit_expr} * an.af_today) / (l.t1_open * ba.af_buyday) - 1, NULL)"
        )
        t1_ok_expr = (
            f"(l.t1_avail >= {entry_off + 1} AND l.t1_open > 0 AND rtl.rt_last > 0 "
            "AND an.af_today > 0 AND ba.af_buyday > 0)"
        )
        # The sell-day-is-today record is a COMPLETE open-to-open window (buy-day open
        # -> today's open); the only reason it is not 'normal' is that offline has not
        # published today's row yet, so its sell open is taken from the live snapshot.
        # It is therefore marked 'normal' (see the joined CTE) rather than as a
        # trailing-edge approximation.
        sell_today_expr = f"(cs.sell_date = toDate({rt_date_lit}))"
    else:
        realtime_cte = ""
        realtime_join = ""
        realtime_label_expr = "NULL"
        # No node snapshot -> fall back to the offline T+1 intraday close/open.
        t1_label_expr = "l.t1_close / l.t1_open - 1"
        t1_ok_expr = f"(l.t1_avail >= {entry_off + 1} AND l.t1_open > 0)"
        # No live open without a snapshot, so no t1 row can be a complete open-to-open
        # window here — nothing gets relabelled to 'normal'.
        sell_today_expr = "0"

    return f"""
WITH
adjpx AS (
    -- Adjusted open per stock (for the exact open-to-open window) plus raw
    -- open/close (for the adj-invariant T+1 intraday fallback). Buffered
    -- [start-15d, end+20d] so the trailing forward window always has rows.
    SELECT
        source_code AS code,
        trade_date,
        open * adj_factor AS aopen,
        open  AS o_raw,
        close AS c_raw
    FROM {quote_identifier("dws_stock_factor_wide")}
    WHERE trade_date >= toDate({start}) - 15
      AND trade_date <= toDate({end}) + 20
      AND open > 0
      AND adj_factor IS NOT NULL
),
lab AS (
    -- Per-stock forward window. Normal: entry = t+1 open, exit = t+(h+1) open,
    -- ``avail`` gates a full window. T+1 intraday fallback: close/open on the
    -- buy day (t+1), needing just the current row + its T+1 row (``t1_avail``).
    SELECT
        code,
        trade_date,
        leadInFrame(aopen, {entry_off}) OVER w AS entry_px,
        leadInFrame(aopen, {exit_off}) OVER w AS exit_px,
        count() OVER w AS avail,
        leadInFrame(o_raw, {entry_off}) OVER w2 AS t1_open,
        leadInFrame(c_raw, {entry_off}) OVER w2 AS t1_close,
        leadInFrame(trade_date, {entry_off}) OVER w2 AS t1_date,
        count() OVER w2 AS t1_avail
    FROM adjpx
    WINDOW
        w AS (
            PARTITION BY code
            ORDER BY trade_date
            ROWS BETWEEN CURRENT ROW AND {frame_off} FOLLOWING
        ),
        w2 AS (
            PARTITION BY code
            ORDER BY trade_date
            ROWS BETWEEN CURRENT ROW AND {entry_off} FOLLOWING
        )
),
bench_px AS (
    SELECT
        trade_date,
        argMax(open, _ingest_time)  AS bopen
    FROM {quote_identifier("index_daily")}
    WHERE ts_code = {bench}
      AND trade_date >= toDate({start}) - 15
      AND trade_date <= toDate({end}) + 20
    GROUP BY trade_date
),
bench AS (
    -- Open-to-open forward window; consumed only by the 'normal' benchmark
    -- (the approximate label sources get a NULL benchmark, see final SELECT).
    SELECT
        trade_date,
        leadInFrame(bopen, {entry_off}) OVER wb AS b_entry,
        leadInFrame(bopen, {exit_off}) OVER wb AS b_exit,
        count() OVER wb AS b_avail
    FROM bench_px
    WINDOW
        wb AS (
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
),{realtime_cte}
resolved AS (
    -- Resolve each (code, pred_date) label by precedence: normal (full window)
    -- > t1_intraday (T+1 offline) > realtime_intraday (node snapshot). A row
    -- with none of the three available is dropped.
    SELECT
        p.pred_date AS pred_date,
        p.code      AS code,
        p.score     AS score,
        (l.avail = {full_window} AND l.entry_px > 0)                       AS normal_ok,
        {t1_ok_expr}                                                       AS t1_ok,
        {sell_today_expr}                                                  AS sell_today,
        l.exit_px / l.entry_px - 1                                         AS normal_label,
        {t1_label_expr}                                                    AS t1_label,
        {realtime_label_expr}                                             AS rt_label
    FROM preds p
    LEFT JOIN lab l
        ON p.code = l.code AND p.pred_date = l.trade_date{realtime_join}
),
joined AS (
    -- A t1_intraday row whose sell session is today is a complete open-to-open
    -- window sourced from the live open, so it is marked 'normal' (it will become a
    -- true offline 'normal' row once today's factor bar publishes after close). Its
    -- benchmark is still gated on a complete offline benchmark window in the final
    -- SELECT, so it shows "—" until the index open for today lands.
    SELECT
        pred_date,
        code,
        score,
        multiIf(normal_ok, normal_label, t1_ok, t1_label, rt_label) AS label,
        multiIf(
            normal_ok, 'normal',
            t1_ok AND sell_today, 'normal',
            t1_ok, 't1_intraday',
            'realtime_intraday'
        ) AS label_source
    FROM resolved
    WHERE normal_ok OR t1_ok OR (rt_label IS NOT NULL)
),
ranked AS (
    SELECT
        pred_date,
        code,
        score,
        label,
        label_source,
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
        label_source,
        rnk,
        cnt,
        rnk <= layer_n              AS is_top_decile,
        rnk > cnt - layer_n         AS is_bot_decile
    FROM ranked
)
SELECT
    f.pred_date                                            AS trade_date,
    -- label_source is constant within a pred_date (precedence is per-date).
    any(f.label_source)                                    AS label_source,
    if(count() < 3, NULL, rankCorr(score, label))          AS rankic,
    if(count() < 3, NULL, corr(score, label))              AS ic,
    avgIf(label, is_top_decile) - avgIf(label, is_bot_decile) AS ls10,
    if(any(cnt) < 20, NULL, avgIf(label, rnk <= 20))       AS top20_label_return,
    if(any(cnt) < 50, NULL, avgIf(label, rnk <= 50))       AS top50_label_return,
    -- Benchmark is the open-to-open forward window. It is emitted only when the
    -- benchmark's OWN forward window is complete (b_avail = full_window, b_exit > 0),
    -- NOT merely when the day is labelled 'normal'. The sell-day-is-today record is
    -- marked 'normal' but its benchmark exit (today's index open) has not published
    -- offline yet — gating on the window keeps that row's benchmark NULL ("—") instead
    -- of the garbage b_exit=0 -> -100% it would otherwise show. The other approximate
    -- sources (t1_intraday future-sell, realtime_intraday) also lack a complete
    -- window, so they stay NULL as before.
    if(any(b.b_avail) = {full_window} AND any(b.b_exit) > 0, any(b.b_exit / b.b_entry - 1), NULL) AS benchmark_label_return,
    if(any(cnt) < 20, NULL, avgIf(label, rnk <= 20) - benchmark_label_return) AS top20_label_excess,
    if(any(cnt) < 50, NULL, avgIf(label, rnk <= 50) - benchmark_label_return) AS top50_label_excess,
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
    realtime_ticks: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run the signal-quality SQL and return per-day rows (already JSON-native)."""
    sql = build_signal_quality_sql(
        table, start_date, end_date, holding_days, realtime_ticks=realtime_ticks
    )
    return clickhouse.query(sql)
