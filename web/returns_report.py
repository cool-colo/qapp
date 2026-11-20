"""Parameterized daily return & slippage report.

Derived from ``scripts/weekly_slippage_return_report.sql`` — the CTE logic is kept
aligned with the standalone MySQL report for strategy NAV and slippage.
The web-specific changes are:

  * the leading ``SET @var`` session statements are removed; the five parameters
    (@start_date, @end_date, @trader_id, @instrument_suffix, @account_id) become
    pymysql named parameters (%(start_date)s ...);
  * the final SELECT emits raw NUMERIC columns with english names instead of the
    Chinese percent-string presentation columns, so the values can be charted;
  * strategy returns use the previous trading day's after-trading net value
    (``market_value + cash + frozen_cash``, less recorded cash deposits) as the
    denominator;
  * the ``combined`` CTE has a WHERE that requires an after-trading asset snapshot,
    its previous net value, and a daily return. This is a web-only divergence from
    the .sql: the filter runs before the windowed cumulatives so the displayed
    week_cum_* values stay consistent with the rows actually shown;
  * the benchmark is CSI 2000 from ClickHouse RiceQuant daily/index realtime
    data, passed into MySQL as bound CTE values; the standalone MySQL report
    retains its snapshot-based CSI 1000 benchmark;
  * the API also returns a period summary computed from the first and last
    after-trading net values in the requested date range.

IF YOU CHANGE THE REPORT LOGIC, UPDATE BOTH THIS FILE AND THE .sql IN LOCKSTEP
(except the web-specific filtering, benchmark source and presentation above).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from web.db import ClickHouseClient, MySqlSource, quote_literal

# Risk-adjusted metric conventions (also hard-coded as literals in RETURNS_SQL and
# the canonical .sql — keep all three in sync). Sharpe uses a 2% annual risk-free
# rate converted to daily (rf_annual / TRADING_DAYS); annualization is × SQRT(252).
ANNUAL_RISK_FREE = 0.02
TRADING_DAYS = 252

# Column order returned by RETURNS_SQL (all numeric except trade_date/week_label).
RETURN_COLUMNS = [
    "trade_date",
    "before_market_value",
    "after_market_value",
    "return_amount",
    "strat_daily_rate",
    "csi2000_daily_rate",
    "excess_daily_rate",
    "daily_volatility",
    "annual_volatility",
    "daily_sharpe",
    "annual_sharpe",
    "week_label",
    "week_cum_return_amount",
    "week_cum_strat_rate",
    "week_cum_csi2000_rate",
    "week_cum_excess_rate",
    "buy_slippage_bps",
    "sell_slippage_bps",
    "total_slippage_bps",
]

_EMPTY_BENCHMARK_SQL = (
    "SELECT CAST(NULL AS DATE) AS trade_date, NULL AS close_price, "
    "NULL AS pre_close, NULL AS daily_rate WHERE FALSE"
)

RETURNS_SQL = """
WITH
slippage_base AS (
    SELECT
        t.trade_date,
        UPPER(t.side) AS side,
        t.quantity,
        t.price AS trade_price,
        t.price * t.quantity AS trade_amount,
        o.open_price,
        CASE
            WHEN UPPER(t.side) = 'BUY'  THEN (t.price - o.open_price) * t.quantity
            WHEN UPPER(t.side) = 'SELL' THEN (o.open_price - t.price) * t.quantity
        END AS slippage_amount_worse_positive
    FROM live_trade t
    JOIN live_order o
        ON o.trade_date = t.trade_date
        AND o.account_id = t.account_id
        AND o.trader_id = t.trader_id
        AND o.client_order_id = t.client_order_id
    WHERE t.trade_date BETWEEN %(start_date)s AND %(end_date)s
        AND o.open_price IS NOT NULL
        AND o.open_price > 0
        AND t.price IS NOT NULL
        AND t.quantity IS NOT NULL
        AND t.quantity > 0
        AND UPPER(t.side) IN ('BUY', 'SELL')
        AND t.account_id = %(account_id)s
        AND t.trader_id = %(trader_id)s
),
daily_slippage AS (
    SELECT
        trade_date,
        SUM(CASE WHEN side = 'BUY'  THEN slippage_amount_worse_positive ELSE 0 END) AS buy_slippage_amount,
        SUM(CASE WHEN side = 'BUY'  THEN trade_amount ELSE 0 END) AS buy_trade_amount,
        SUM(CASE WHEN side = 'SELL' THEN slippage_amount_worse_positive ELSE 0 END) AS sell_slippage_amount,
        SUM(CASE WHEN side = 'SELL' THEN trade_amount ELSE 0 END) AS sell_trade_amount,
        SUM(slippage_amount_worse_positive) AS total_slippage_amount,
        SUM(trade_amount) AS total_trade_amount
    FROM slippage_base
    GROUP BY trade_date
),
before_market AS (
    SELECT
        trade_date,
        SUM(open_price * volume) AS before_market_value
    FROM live_position_snapshot
    WHERE snapshot_type = 'before_trading' AND trader_id = %(trader_id)s
        AND trade_date BETWEEN %(start_date)s AND %(end_date)s
        AND can_use_volume IS NOT NULL
    GROUP BY trade_date
),
asset_nav_history AS (
    SELECT
        trade_date,
        market_value,
        -- Keep cash deposits in sync with scripts/calculate_daily_returns.py.
        market_value + cash + frozen_cash
            - CASE WHEN account_id = '8886569186' AND trade_date >= '2026-11-19'
                   THEN 150000 ELSE 0 END AS net_value
    FROM live_asset_snapshot
    WHERE snapshot_type = 'after_trading'
        AND account_id = %(account_id)s
        AND trader_id = %(trader_id)s
        AND trade_date BETWEEN COALESCE(
            (
                SELECT MAX(previous.trade_date)
                FROM live_asset_snapshot previous
                WHERE previous.snapshot_type = 'after_trading'
                    AND previous.account_id = %(account_id)s
                    AND previous.trader_id = %(trader_id)s
                    AND previous.trade_date < %(start_date)s
            ),
            %(start_date)s
        ) AND %(end_date)s
        AND market_value IS NOT NULL
        AND cash IS NOT NULL
        AND frozen_cash IS NOT NULL
),
asset_nav AS (
    SELECT
        trade_date,
        market_value,
        net_value,
        LAG(net_value) OVER (ORDER BY trade_date) AS previous_net_value
    FROM asset_nav_history
),
after_market AS (
    SELECT
        p.trade_date,
        SUM(COALESCE(t.last_price, t.last_close) * p.volume) AS after_market_value
    FROM live_position_snapshot p
    LEFT JOIN live_stock_tick_snapshot t
        ON p.stock_code = t.stock_code AND p.trade_date = t.trade_date
        AND t.snapshot_type = 'after_trading'
    WHERE p.snapshot_type = 'after_trading' AND p.trader_id = %(trader_id)s
        AND p.trade_date BETWEEN %(start_date)s AND %(end_date)s
        AND t.instrument_id LIKE CONCAT('%%', %(instrument_suffix)s)
        AND can_use_volume IS NOT NULL
    GROUP BY p.trade_date
),
daily_return AS (
    SELECT
        trade_date,
        return_amount
    FROM live_daily_stock_return
    WHERE stock_code = 'summary' AND trader_id = %(trader_id)s
        AND trade_date BETWEEN %(start_date)s AND %(end_date)s
        AND account_id = %(account_id)s
),
csi2000 AS (
    {benchmark_sql}
),
all_dates AS (
    SELECT trade_date FROM daily_slippage
    UNION SELECT trade_date FROM before_market
    UNION SELECT trade_date FROM after_market
    UNION SELECT trade_date FROM asset_nav
    UNION SELECT trade_date FROM daily_return
    UNION SELECT trade_date FROM csi2000
),
combined AS (
    SELECT
        d.trade_date,
        WEEKOFYEAR(d.trade_date) AS week_num,
        bm.before_market_value,
        am.after_market_value,
        r.return_amount,
        r.return_amount / NULLIF(an.previous_net_value, 0) AS strat_daily_rate,
        an.net_value,
        an.previous_net_value,
        s.buy_slippage_amount,
        s.buy_trade_amount,
        s.sell_slippage_amount,
        s.sell_trade_amount,
        s.total_slippage_amount,
        s.total_trade_amount,
        ck.daily_rate  AS csi2000_daily_rate,
        ck.close_price AS csi2000_close,
        ck.pre_close   AS csi2000_preclose
    FROM all_dates d
    LEFT JOIN before_market bm ON d.trade_date = bm.trade_date
    LEFT JOIN after_market  am ON d.trade_date = am.trade_date
    LEFT JOIN asset_nav     an ON d.trade_date = an.trade_date
    LEFT JOIN daily_return  r  ON d.trade_date = r.trade_date
    LEFT JOIN daily_slippage s ON d.trade_date = s.trade_date
    LEFT JOIN csi2000       ck ON d.trade_date = ck.trade_date
    -- Require a complete after-to-after net-value pair plus a daily return so the
    -- windowed values stay consistent with the rows actually returned.
    WHERE an.net_value IS NOT NULL
      AND an.previous_net_value IS NOT NULL
      AND r.return_amount IS NOT NULL
),
windowed AS (
    SELECT
        c.*,
        net_value - FIRST_VALUE(previous_net_value) OVER w AS week_cum_return_amount,
        net_value / NULLIF(FIRST_VALUE(previous_net_value) OVER w, 0) - 1
            AS week_cum_strat_rate,
        (csi2000_close - FIRST_VALUE(csi2000_preclose) OVER w)
            / NULLIF(FIRST_VALUE(csi2000_preclose) OVER w, 0) AS week_cum_csi2000_rate,
        -- Expanding (cumulative) risk metrics over the full period up to this row
        -- (ordered by trade_date, NOT partitioned by week). Sample std (n-1);
        -- Sharpe uses a 2%% annual risk-free rate converted to daily (0.02/252).
        STDDEV_SAMP(strat_daily_rate) OVER cum AS daily_volatility,
        (AVG(strat_daily_rate) OVER cum - (0.02 / 252))
            / NULLIF(STDDEV_SAMP(strat_daily_rate) OVER cum, 0) AS daily_sharpe
    FROM combined c
    WINDOW
        w AS (
            PARTITION BY week_num
            ORDER BY trade_date
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ),
        cum AS (
            ORDER BY trade_date
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        )
)
SELECT
    w.trade_date                                          AS trade_date,
    w.before_market_value                                 AS before_market_value,
    w.after_market_value                                  AS after_market_value,
    w.return_amount                                       AS return_amount,
    w.net_value                                           AS net_value,
    w.previous_net_value                                  AS previous_net_value,
    w.strat_daily_rate                                    AS strat_daily_rate,
    w.csi2000_daily_rate                                  AS csi2000_daily_rate,
    w.csi2000_close                                       AS csi2000_close,
    w.csi2000_preclose                                    AS csi2000_preclose,
    (w.strat_daily_rate - w.csi2000_daily_rate)           AS excess_daily_rate,
    w.daily_volatility                                    AS daily_volatility,
    (w.daily_volatility * SQRT(252))                      AS annual_volatility,
    w.daily_sharpe                                        AS daily_sharpe,
    (w.daily_sharpe * SQRT(252))                          AS annual_sharpe,
    CONCAT('W', w.week_num)                               AS week_label,
    w.week_cum_return_amount                              AS week_cum_return_amount,
    w.week_cum_strat_rate                                 AS week_cum_strat_rate,
    w.week_cum_csi2000_rate                               AS week_cum_csi2000_rate,
    (w.week_cum_strat_rate - w.week_cum_csi2000_rate)     AS week_cum_excess_rate,
    (w.buy_slippage_amount  / NULLIF(w.buy_trade_amount,  0) * 10000) AS buy_slippage_bps,
    (w.sell_slippage_amount / NULLIF(w.sell_trade_amount, 0) * 10000) AS sell_slippage_bps,
    (w.total_slippage_amount / NULLIF(w.total_trade_amount, 0) * 10000) AS total_slippage_bps
FROM windowed w
ORDER BY w.trade_date
""".replace("{benchmark_sql}", _EMPTY_BENCHMARK_SQL)

# Fallback used when an account has no positions to derive the suffix from.
_DEFAULT_SUFFIX = ".QMT"


def derive_instrument_suffix(mysql: MySqlSource, account_id: str, trader_id: str) -> str:
    """Infer the venue suffix (e.g. '.BIGQMT') from this account's positions.

    instrument_id looks like '000001.SZ.BIGQMT'; the suffix is the segment after the
    stock_code. Falls back to '.QMT' if no positions exist yet.
    """
    rows = mysql.query(
        """
        SELECT instrument_id, stock_code
        FROM live_position_snapshot
        WHERE account_id = %(account_id)s AND trader_id = %(trader_id)s
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        {"account_id": account_id, "trader_id": trader_id},
    )
    if not rows:
        return _DEFAULT_SUFFIX
    instrument_id = str(rows[0]["instrument_id"])
    stock_code = str(rows[0]["stock_code"])
    if stock_code and instrument_id.startswith(stock_code):
        suffix = instrument_id[len(stock_code):]
        if suffix.startswith("."):
            return suffix
    # Fall back to everything from the last dot.
    _, _, tail = instrument_id.rpartition(".")
    return f".{tail}" if tail else _DEFAULT_SUFFIX


def query_returns(
    mysql: MySqlSource,
    clickhouse: ClickHouseClient,
    *,
    account_id: str,
    trader_id: str,
    start_date: str,
    end_date: str,
    instrument_suffix: str | None = None,
) -> list[dict[str, Any]]:
    start_date = date.fromisoformat(start_date).isoformat()
    end_date = date.fromisoformat(end_date).isoformat()
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    suffix = instrument_suffix or derive_instrument_suffix(mysql, account_id, trader_id)
    params: dict[str, Any] = {
        "account_id": account_id,
        "trader_id": trader_id,
        "start_date": start_date,
        "end_date": end_date,
        "instrument_suffix": suffix,
    }
    benchmark = clickhouse.query(csi2000_query(start_date, end_date))
    selects = []
    for i, row in enumerate(benchmark):
        params[f"benchmark_date_{i}"] = row["trade_date"]
        # MySQL NAV values are Decimal. Bind market numbers as decimal literals
        # too, so period summaries can subtract the two return rates directly.
        for parameter, column in (
            ("close", "close_price"), ("preclose", "pre_close"), ("rate", "daily_rate"),
        ):
            value = row[column]
            params[f"benchmark_{parameter}_{i}"] = Decimal(str(value)) if value is not None else None
        selects.append(
            f"SELECT CAST(%(benchmark_date_{i})s AS DATE) AS trade_date, "
            f"%(benchmark_close_{i})s AS close_price, "
            f"%(benchmark_preclose_{i})s AS pre_close, "
            f"%(benchmark_rate_{i})s AS daily_rate",
        )
    benchmark_sql = "\n    UNION ALL\n    ".join(selects) if selects else _EMPTY_BENCHMARK_SQL
    return mysql.query(RETURNS_SQL.replace(_EMPTY_BENCHMARK_SQL, benchmark_sql), params)


def csi2000_query(start_date: str, end_date: str) -> str:
    """Daily history plus today's latest index minute when EOD is not loaded.

    Keep the preceding daily close even when it lies outside the report range.
    Repeated observations use a whole-row tuple so nullable fields cannot pick
    values from an older observation independently.
    """
    start = quote_literal(date.fromisoformat(start_date).isoformat())
    end = quote_literal(date.fromisoformat(end_date).isoformat())
    return f"""
    WITH daily AS (
        SELECT trading_date AS trade_date,
               argMax(tuple(close, prev_close),
                      tuple(_ingest_time, _batch_id, _record_hash)) AS prices
        FROM ricequant_index_daily
        WHERE order_book_id = '932000.INDX'
          AND trading_date <= toDate({end})
        GROUP BY trading_date
    ), benchmark AS (
        SELECT trade_date, tupleElement(prices, 1) AS close_price,
               tupleElement(prices, 2) AS pre_close
        FROM daily
        WHERE trade_date BETWEEN toDate({start}) AND toDate({end})
        UNION ALL
        SELECT trade_date, tupleElement(prices, 1) AS close_price,
               (SELECT tupleElement(prices, 1) FROM daily
                WHERE trade_date < toDate(now('Asia/Shanghai'))
                ORDER BY trade_date DESC LIMIT 1) AS pre_close
        FROM (
            SELECT trade_date,
                   argMax(tuple(close), tuple(datetime, received_at, event_id)) AS prices
            FROM ricequant_index_minute_realtime
            WHERE order_book_id = '932000.INDX'
              AND trade_date = toDate(now('Asia/Shanghai'))
              AND trade_date BETWEEN toDate({start}) AND toDate({end})
              AND trade_date NOT IN (SELECT trade_date FROM daily)
              AND (
                  formatDateTime(datetime, '%H:%i:%S', 'Asia/Shanghai') BETWEEN '09:30:00' AND '11:30:00'
                  OR formatDateTime(datetime, '%H:%i:%S', 'Asia/Shanghai') BETWEEN '13:00:00' AND '15:00:00'
              )
              AND datetime <= now64(3, 'Asia/Shanghai')
              AND received_at <= now64(6, 'Asia/Shanghai')
            GROUP BY trade_date
        )
    )
    SELECT toString(trade_date) AS trade_date,
           if(isFinite(close_price) AND close_price > 0, close_price, NULL) AS close_price,
           if(isFinite(pre_close) AND pre_close > 0, pre_close, NULL) AS pre_close,
           if(isFinite(close_price) AND close_price > 0
              AND isFinite(pre_close) AND pre_close > 0,
              close_price / pre_close - 1, NULL) AS daily_rate
    FROM benchmark
    ORDER BY trade_date
    """


def build_return_summary(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Build the period footer from requested-range endpoint net values."""
    if not rows:
        return None

    nav_rows = [
        row for row in rows
        if row.get("net_value") is not None and row.get("previous_net_value") is not None
    ]
    if not nav_rows:
        return {"trade_date": "合计"}

    first = nav_rows[0]
    last = nav_rows[-1]
    initial_net_value = first["previous_net_value"]
    final_net_value = last["net_value"]
    period_pnl = final_net_value - initial_net_value if initial_net_value else None
    period_rate = (
        final_net_value / initial_net_value - 1
        if initial_net_value is not None and initial_net_value != 0
        else None
    )

    benchmark_rate = None
    benchmark_initial = first.get("csi2000_preclose")
    benchmark_final = last.get("csi2000_close")
    if benchmark_initial is not None and benchmark_initial != 0 and benchmark_final is not None:
        benchmark_rate = benchmark_final / benchmark_initial - 1
    excess_rate = (
        period_rate - benchmark_rate
        if period_rate is not None and benchmark_rate is not None
        else None
    )

    return {
        "trade_date": "合计",
        "return_amount": period_pnl,
        "strat_daily_rate": period_rate,
        "csi2000_daily_rate": benchmark_rate,
        "excess_daily_rate": excess_rate,
        "week_cum_return_amount": period_pnl,
        "week_cum_strat_rate": period_rate,
        "week_cum_csi2000_rate": benchmark_rate,
        "week_cum_excess_rate": excess_rate,
    }
