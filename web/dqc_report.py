"""Data Quality Check (DQC) report SQL for the dashboard (ClickHouse-backed).

The tushare-integration project runs two quality subsystems and writes results into the
SAME ClickHouse instance the dashboard reads (db ``default``):

- **systematic DQC** (``dq_dqc_*``) — daily suites of rule checks over the factor warehouse,
  emitting per-rule results, raw metric values, and offending-row samples.
- **publish validation** (``dq_validation_*``) — pre-publish gate checks run per staging table.

All ``dq_*`` tables are **append-only** ``MergeTree ORDER BY (run_id)``: the same suite runs
repeatedly (daily, and sometimes twice a day), so every "current state" query must pick the
**latest run per grouping** in SQL. We do this with a CTE selecting ``argMax(run_id,
started_at)`` from ``dq_dqc_run`` (the only table carrying ``started_at``) and joining the
fact tables on ``run_id``; per-``trade_date`` metric series dedup with ``argMax(metric_value,
created_at)`` instead.

Two schema facts drive the joins:

- ``dq_dqc_result`` has **no ``as_of_date``** — the date comes from the joined run row.
- ``dq_validation_result`` has **no ``table_name``/``layer``/``stage``** — those come from the
  joined ``dq_validation_run`` row.

Table names here are hard-coded trusted constants. User filter *values* (dates, layer, suite,
metric_name, entity_name, rule_id, instrument_id) are quoted via ``quote_literal``; enum
params are additionally validated against allow-lists in ``app.py`` before reaching SQL.
Every query against a large table (``dq_dqc_metric`` ~2.2M rows, ``dq_dqc_sample`` ~27k) is
filtered and LIMITed.
"""

from __future__ import annotations

from web.db import quote_literal

# The comprehensive suite run rolls up every check_layer under one run keyed table_name='all';
# the per-table run (table_name='dws_stock_factor_wide') is a subset. The health/checks views
# use the 'all' run so consistency/completeness/etc. all appear.
RUN_TABLE_ALL = "all"

# The factor warehouse whose per-metric drift/trend the metric explorer inspects. A parallel
# ``dws_stock_factor_wide_matrix`` staging table shares the same factor entity_names in
# ``dq_dqc_metric``; it is upstream plumbing, not the served warehouse, so the metric-trend
# query scopes to this table_name to keep matrix rows from leaking into (or double-counting)
# the trend. Publish-validation queries likewise exclude ``%_matrix`` tables (see below).
FACTOR_WIDE_TABLE = "dws_stock_factor_wide"

# ---- allow-lists (validated in app.py; duplicated here as documentation) ----
DQC_LAYERS = {"ods", "dwd", "dws"}
DQC_SEVERITIES = {"BLOCKER", "WARN", "MONITOR"}
DQC_STATUSES = {"PASS", "FAIL", "MONITOR", "SKIPPED"}
DQC_SAMPLE_TYPES = {
    "spot_check",
    "factor_cross_check_failed",
    "factor_cross_check_passed",
    "metric_drift",
}

# ---- per-row column orders (frontend renders in this order) ----
DQC_HEALTH_COLUMNS = ["as_of_date", "check_layer", "fails", "monitors", "passes", "issues"]

DQC_RUN_COLUMNS = [
    "as_of_date", "layer", "suite_name", "table_name", "mode", "status",
    "started_at", "finished_at", "run_id",
]

DQC_CHECK_COLUMNS = [
    "check_layer", "check_type", "rule_id", "severity", "status",
    "checked_count", "issue_count", "issue_rate",
    "observed_value", "expected_min", "expected_max",
    "baseline_mean", "baseline_std", "z_score", "message", "run_id",
]

DQC_METRIC_TREND_COLUMNS = ["trade_date", "metric_value"]

VALIDATION_RUN_COLUMNS = [
    "layer", "stage", "table_name", "target_table_name", "mode", "status",
    "started_at", "finished_at", "run_id",
]

VALIDATION_RESULT_COLUMNS = [
    "table_name", "layer", "stage", "rule_id", "severity", "status",
    "issue_count", "issue_rate", "description", "message", "run_id",
]


def _date_range(column: str, start: str, end: str) -> str:
    return (
        f"{column} >= toDate({quote_literal(start)}) "
        f"AND {column} <= toDate({quote_literal(end)})"
    )


def _opt_eq(column: str, value: str | None) -> str:
    """An optional ``AND col = 'value'`` clause, empty when value is falsy."""
    return f"\n  AND {column} = {quote_literal(value)}" if value else ""


# ``dws_stock_factor_wide_matrix`` (and any future ``*_matrix`` staging variant) is upstream
# plumbing, not a served warehouse table; its validation runs are noise in the 发布校验 view.
_EXCLUDE_MATRIX = "\n  AND table_name NOT LIKE '%\\_matrix'"


# ---- systematic DQC -------------------------------------------------------


def build_dqc_health_timeline_sql(
    start: str, end: str, layer: str | None = None, suite: str | None = None
) -> str:
    """Per ``(as_of_date, check_layer)`` FAIL/MONITOR/PASS counts + total issue_count.

    Feeds the health-timeline stacked bar. Deduped to the latest ``all`` run per day; the
    result table has no ``as_of_date``, so it is taken from the joined run (``l.as_of_date``).
    """
    return f"""
WITH latest AS (
    SELECT as_of_date, argMax(run_id, started_at) AS run_id
    FROM dq_dqc_run
    WHERE table_name = {quote_literal(RUN_TABLE_ALL)}
      AND {_date_range("as_of_date", start, end)}{_opt_eq("layer", layer)}{_opt_eq("suite_name", suite)}
    GROUP BY as_of_date
)
SELECT
    toString(l.as_of_date) AS as_of_date,
    res.check_layer AS check_layer,
    countIf(res.status = 'FAIL') AS fails,
    countIf(res.status = 'MONITOR') AS monitors,
    countIf(res.status = 'PASS') AS passes,
    sum(res.issue_count) AS issues
FROM dq_dqc_result AS res
INNER JOIN latest AS l ON res.run_id = l.run_id
GROUP BY l.as_of_date, res.check_layer
ORDER BY as_of_date ASC, check_layer ASC
""".strip()


def build_dqc_runs_sql(
    start: str, end: str, layer: str | None = None, suite: str | None = None
) -> str:
    """Latest ``all`` run per day within the window (run-level status overview)."""
    return f"""
WITH latest AS (
    SELECT argMax(run_id, started_at) AS run_id
    FROM dq_dqc_run
    WHERE table_name = {quote_literal(RUN_TABLE_ALL)}
      AND {_date_range("as_of_date", start, end)}{_opt_eq("layer", layer)}{_opt_eq("suite_name", suite)}
    GROUP BY as_of_date
)
SELECT
    toString(as_of_date) AS as_of_date,
    layer, suite_name, table_name, mode, status,
    toString(started_at) AS started_at,
    toString(finished_at) AS finished_at,
    run_id
FROM dq_dqc_run
WHERE run_id IN (SELECT run_id FROM latest)
ORDER BY as_of_date DESC
""".strip()


def build_dqc_run_tables_sql() -> str:
    """DISTINCT ``table_name`` values present in ``dq_dqc_run`` (populates the checks dropdown).

    Currently ``all`` (the comprehensive rollup) and ``dws_stock_factor_wide`` (per-table subset).
    """
    return """
SELECT DISTINCT table_name
FROM dq_dqc_run
ORDER BY table_name
""".strip()


def build_dqc_max_date_sql(
    table_name: str = FACTOR_WIDE_TABLE, layer: str | None = None, suite: str | None = None
) -> str:
    """The newest ``as_of_date`` that has a run for ``table_name`` (default for the checks table)."""
    return f"""
SELECT toString(max(as_of_date)) AS as_of_date
FROM dq_dqc_run
WHERE table_name = {quote_literal(table_name)}{_opt_eq("layer", layer)}{_opt_eq("suite_name", suite)}
""".strip()


def build_dqc_latest_checks_sql(
    as_of_date: str,
    table_name: str = FACTOR_WIDE_TABLE,
    layer: str | None = None,
    suite: str | None = None,
) -> str:
    """Every rule result for the latest run of ``table_name`` on ``as_of_date``.

    ``table_name`` selects which suite's run to show: ``all`` is the comprehensive rollup
    (every check_layer), a per-table name (e.g. ``dws_stock_factor_wide``) its subset.
    Ordered FAIL-first, then severity (BLOCKER > WARN > MONITOR), then issue_count desc, so the
    worst problems sit at the top of the table.
    """
    return f"""
WITH latest AS (
    SELECT argMax(run_id, started_at) AS run_id
    FROM dq_dqc_run
    WHERE table_name = {quote_literal(table_name)}
      AND as_of_date = toDate({quote_literal(as_of_date)}){_opt_eq("layer", layer)}{_opt_eq("suite_name", suite)}
)
SELECT
    check_layer, check_type, rule_id, severity, status,
    checked_count, issue_count, issue_rate,
    observed_value, expected_min, expected_max,
    baseline_mean, baseline_std, z_score, message, run_id
FROM dq_dqc_result
WHERE run_id = (SELECT run_id FROM latest)
ORDER BY
    status = 'FAIL' DESC,
    multiIf(severity = 'BLOCKER', 0, severity = 'WARN', 1, 2),
    issue_count DESC,
    rule_id ASC
""".strip()


def build_dqc_metric_options_sql(
    layer: str | None = None, suite: str | None = None
) -> str:
    """DISTINCT (metric_scope, metric_name, entity_name) for the latest per-table run.

    Scoped to one run so the ~2.2M-row metric table is not scanned wholesale; ~375 entities.
    """
    return f"""
WITH latest AS (
    SELECT argMax(run_id, started_at) AS run_id
    FROM dq_dqc_run
    WHERE table_name = {quote_literal(FACTOR_WIDE_TABLE)}{_opt_eq("layer", layer)}{_opt_eq("suite_name", suite)}
)
SELECT DISTINCT metric_scope, metric_name, entity_name
FROM dq_dqc_metric
WHERE run_id = (SELECT run_id FROM latest)
  AND table_name = {quote_literal(FACTOR_WIDE_TABLE)}
ORDER BY metric_scope, metric_name, entity_name
LIMIT 5000
""".strip()


def build_dqc_metric_trend_sql(
    metric_name: str, entity_name: str, start: str, end: str
) -> str:
    """One point per ``trade_date`` for a chosen (metric_name, entity_name) over a window.

    Deduped across repeated runs via ``argMax(metric_value, created_at)``.
    """
    return f"""
SELECT toString(trade_date) AS trade_date, metric_value
FROM (
    SELECT trade_date, argMax(metric_value, created_at) AS metric_value
    FROM dq_dqc_metric
    WHERE metric_name = {quote_literal(metric_name)}
      AND entity_name = {quote_literal(entity_name)}
      AND table_name = {quote_literal(FACTOR_WIDE_TABLE)}
      AND {_date_range("trade_date", start, end)}
    GROUP BY trade_date
    ORDER BY trade_date ASC
    LIMIT 5000
)
""".strip()


def build_dqc_drift_baseline_sql(entity_name: str, as_of_date: str) -> str:
    """The drift result row (baseline_mean/std, z_score) for an entity on a date, if any.

    ``dqc_metric_drift`` records the baseline it compared against; the metric-trend chart
    overlays it as a band. Returns 0 rows when the entity has no drift check (skip the band).
    """
    return f"""
WITH latest AS (
    SELECT argMax(run_id, started_at) AS run_id
    FROM dq_dqc_run
    WHERE table_name = {quote_literal(FACTOR_WIDE_TABLE)}
      AND as_of_date = toDate({quote_literal(as_of_date)})
)
SELECT baseline_mean, baseline_std, z_score, observed_value
FROM dq_dqc_result
WHERE run_id = (SELECT run_id FROM latest)
  AND check_type = 'drift'
  AND rule_id LIKE {quote_literal("%" + entity_name + "%")}
  AND baseline_mean IS NOT NULL
LIMIT 1
""".strip()


def build_dqc_samples_sql(
    rule_id: str,
    as_of_date: str,
    sample_type: str | None = None,
    limit: int = 500,
    table: str | None = None,
) -> str:
    """Offending-row samples for a rule on a date (latest run's samples).

    The comprehensive ``all`` run holds every table's samples under one ``run_id`` (each
    ``dq_dqc_sample`` row carries its own ``table_name``), so ``table`` filters the sample rows
    directly — not which run is picked. Pass it to scope samples to the table the user selected
    in 最新运行检查 (e.g. ``dws_stock_factor_wide``); omit / ``all`` shows every table's samples.

    ``sample_json`` is returned raw; the endpoint parses/flattens it. Ordered newest
    trade_date first so the most recent offenders surface. ``limit`` is capped in the endpoint.
    """
    limit = max(1, min(int(limit), 2000))
    table_clause = _opt_eq("table_name", table) if table and table != RUN_TABLE_ALL else ""
    return f"""
WITH latest AS (
    SELECT argMax(run_id, started_at) AS run_id
    FROM dq_dqc_run
    WHERE table_name = {quote_literal(RUN_TABLE_ALL)}
      AND as_of_date = toDate({quote_literal(as_of_date)})
)
SELECT
    rule_id,
    toString(as_of_date) AS as_of_date,
    toString(trade_date) AS trade_date,
    instrument_id, entity_name, sample_type, sample_json
FROM dq_dqc_sample
WHERE run_id = (SELECT run_id FROM latest)
  AND rule_id = {quote_literal(rule_id)}{_opt_eq("sample_type", sample_type)}{table_clause}
ORDER BY trade_date DESC
LIMIT {limit:d}
""".strip()


def build_dqc_investigate_point_sql(
    source_code: str, trade_date: str, table: str = "dws_stock_factor_wide"
) -> str:
    """A copy-able point query re-selecting the offending warehouse row (not executed here).

    Keyed on ``source_code`` + ``trade_date`` — the identifiers carried in every sample_json —
    so it returns exactly the row the DQC sample flagged, for the user to inspect in ClickHouse.
    """
    return f"""
SELECT *
FROM {table}
WHERE source_code = {quote_literal(source_code)}
  AND trade_date = toDate({quote_literal(trade_date)})
""".strip()


def build_dqc_investigate_history_sql(
    source_code: str,
    history_start: str,
    history_end: str,
    table: str = "dws_stock_factor_wide",
) -> str:
    """A copy-able rolling-history query (the window a cross-check/drift compared against)."""
    return f"""
SELECT source_code, trade_date, *
FROM {table}
WHERE source_code = {quote_literal(source_code)}
  AND trade_date >= toDate({quote_literal(history_start)})
  AND trade_date <= toDate({quote_literal(history_end)})
ORDER BY trade_date ASC
""".strip()


# ---- publish validation ---------------------------------------------------


def build_validation_runs_sql(
    start: str,
    end: str,
    layer: str | None = None,
    stage: str | None = None,
    table: str | None = None,
) -> str:
    """Latest validation run per ``(layer, stage, table_name)`` within the window."""
    return f"""
WITH latest AS (
    SELECT argMax(run_id, started_at) AS run_id
    FROM dq_validation_run
    WHERE {_date_range("toDate(started_at)", start, end)}{_opt_eq("layer", layer)}{_opt_eq("stage", stage)}{_opt_eq("table_name", table)}{_EXCLUDE_MATRIX}
    GROUP BY layer, stage, table_name
)
SELECT
    layer, stage, table_name, target_table_name, mode, status,
    toString(started_at) AS started_at,
    toString(finished_at) AS finished_at,
    run_id
FROM dq_validation_run
WHERE run_id IN (SELECT run_id FROM latest)
ORDER BY status = 'FAIL' DESC, started_at DESC
LIMIT 2000
""".strip()


def build_validation_results_sql(
    start: str,
    end: str,
    layer: str | None = None,
    stage: str | None = None,
    table: str | None = None,
) -> str:
    """Validation results joined to their run (results carry no table_name/layer/stage).

    Deduped to the latest run per ``(layer, stage, table_name)``; FAIL-first, worst issue_count
    first.
    """
    return f"""
WITH latest AS (
    SELECT argMax(run_id, started_at) AS run_id
    FROM dq_validation_run
    WHERE {_date_range("toDate(started_at)", start, end)}{_opt_eq("layer", layer)}{_opt_eq("stage", stage)}{_opt_eq("table_name", table)}{_EXCLUDE_MATRIX}
    GROUP BY layer, stage, table_name
),
runs AS (
    SELECT run_id, layer, stage, table_name
    FROM dq_validation_run
    WHERE run_id IN (SELECT run_id FROM latest)
)
SELECT
    runs.table_name AS table_name,
    runs.layer AS layer,
    runs.stage AS stage,
    r.rule_id AS rule_id,
    r.severity AS severity,
    r.status AS status,
    r.issue_count AS issue_count,
    r.issue_rate AS issue_rate,
    r.description AS description,
    r.message AS message,
    r.run_id AS run_id
FROM dq_validation_result AS r
INNER JOIN runs ON r.run_id = runs.run_id
ORDER BY r.status = 'FAIL' DESC, r.issue_count DESC
LIMIT 2000
""".strip()
