"""Parameterized daily return & slippage report.

Derived from ``scripts/weekly_slippage_return_report.sql`` — the CTE logic is kept
byte-for-byte identical so this stays faithful to the canonical report. The only
changes are:

  * the leading ``SET @var`` session statements are removed; the five parameters
    (@start_date, @end_date, @trader_id, @instrument_suffix, @account_id) become
    pymysql named parameters (%(start_date)s ...);
  * the final SELECT emits raw NUMERIC columns with english names instead of the
    Chinese percent-string presentation columns, so the values can be charted.

IF YOU CHANGE THE REPORT LOGIC, UPDATE BOTH THIS FILE AND THE .sql IN LOCKSTEP.
"""

from __future__ import annotations

from typing import Any

from web.db import MySqlSource

# Column order returned by RETURNS_SQL (all numeric except trade_date/week_label).
RETURN_COLUMNS = [
    "trade_date",
    "before_market_value",
    "after_market_value",
    "return_amount",
    "strat_daily_rate",
    "csi1000_daily_rate",
    "excess_daily_rate",
    "week_label",
    "week_cum_return_amount",
    "week_cum_strat_rate",
    "week_cum_csi1000_rate",
    "week_cum_excess_rate",
    "buy_slippage_bps",
    "sell_slippage_bps",
    "total_slippage_bps",
]

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
after_market AS (
    SELECT
        p.trade_date,
        SUM(COALESCE(t.last_price, t.last_close) * p.volume) AS after_market_value
    FROM live_position_snapshot p
    LEFT JOIN live_stock_tick_snapshot t
        ON p.stock_code = t.stock_code AND p.trade_date = t.trade_date
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
csi_all AS (
    SELECT
        trade_date,
        close     AS close_price,
        pre_close AS pre_close,
        pct_chg / 100 AS daily_rate
    FROM index_eod_price
    WHERE ts_code = '000985.CSI'
        AND trade_date BETWEEN %(start_date)s AND %(end_date)s
),
csi1000 AS (
    SELECT
        trade_date,
        MAX(last_price) AS close_price,
        MAX(last_close) AS pre_close,
        (MAX(last_price) - MAX(last_close)) / NULLIF(MAX(last_close), 0) AS daily_rate
    FROM live_stock_tick_snapshot
    WHERE stock_code = '399852.SZ'
        AND snapshot_type = 'after_trading'
        AND trade_date BETWEEN %(start_date)s AND %(end_date)s
    GROUP BY trade_date
),
all_dates AS (
    SELECT trade_date FROM daily_slippage
    UNION SELECT trade_date FROM before_market
    UNION SELECT trade_date FROM after_market
    UNION SELECT trade_date FROM daily_return
    UNION SELECT trade_date FROM csi_all
    UNION SELECT trade_date FROM csi1000
),
combined AS (
    SELECT
        d.trade_date,
        WEEKOFYEAR(d.trade_date) AS week_num,
        bm.before_market_value,
        am.after_market_value,
        r.return_amount,
        r.return_amount / NULLIF(am.after_market_value - r.return_amount, 0) AS strat_daily_rate,
        s.buy_slippage_amount,
        s.buy_trade_amount,
        s.sell_slippage_amount,
        s.sell_trade_amount,
        s.total_slippage_amount,
        s.total_trade_amount,
        ca.daily_rate  AS csi_all_daily_rate,
        ca.close_price AS csi_all_close,
        ca.pre_close   AS csi_all_preclose,
        ck.daily_rate  AS csi1000_daily_rate,
        ck.close_price AS csi1000_close,
        ck.pre_close   AS csi1000_preclose
    FROM all_dates d
    LEFT JOIN before_market bm ON d.trade_date = bm.trade_date
    LEFT JOIN after_market  am ON d.trade_date = am.trade_date
    LEFT JOIN daily_return  r  ON d.trade_date = r.trade_date
    LEFT JOIN daily_slippage s ON d.trade_date = s.trade_date
    LEFT JOIN csi_all       ca ON d.trade_date = ca.trade_date
    LEFT JOIN csi1000       ck ON d.trade_date = ck.trade_date
),
windowed AS (
    SELECT
        c.*,
        SUM(return_amount) OVER w AS week_cum_return_amount,
        EXP(SUM(LN(1 + strat_daily_rate)) OVER w) - 1 AS week_cum_strat_rate,
        (csi_all_close - FIRST_VALUE(csi_all_preclose) OVER w)
            / NULLIF(FIRST_VALUE(csi_all_preclose) OVER w, 0) AS week_cum_csi_all_rate,
        (csi1000_close - FIRST_VALUE(csi1000_preclose) OVER w)
            / NULLIF(FIRST_VALUE(csi1000_preclose) OVER w, 0) AS week_cum_csi1000_rate
    FROM combined c
    WINDOW w AS (
        PARTITION BY week_num
        ORDER BY trade_date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )
)
SELECT
    w.trade_date                                          AS trade_date,
    w.before_market_value                                 AS before_market_value,
    w.after_market_value                                  AS after_market_value,
    w.return_amount                                       AS return_amount,
    w.strat_daily_rate                                    AS strat_daily_rate,
    w.csi1000_daily_rate                                  AS csi1000_daily_rate,
    (w.strat_daily_rate - w.csi1000_daily_rate)           AS excess_daily_rate,
    CONCAT('W', w.week_num)                               AS week_label,
    w.week_cum_return_amount                              AS week_cum_return_amount,
    w.week_cum_strat_rate                                 AS week_cum_strat_rate,
    w.week_cum_csi1000_rate                               AS week_cum_csi1000_rate,
    (w.week_cum_strat_rate - w.week_cum_csi1000_rate)     AS week_cum_excess_rate,
    (w.buy_slippage_amount  / NULLIF(w.buy_trade_amount,  0) * 10000) AS buy_slippage_bps,
    (w.sell_slippage_amount / NULLIF(w.sell_trade_amount, 0) * 10000) AS sell_slippage_bps,
    (w.total_slippage_amount / NULLIF(w.total_trade_amount, 0) * 10000) AS total_slippage_bps
FROM windowed w
ORDER BY w.trade_date
"""

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
    *,
    account_id: str,
    trader_id: str,
    start_date: str,
    end_date: str,
    instrument_suffix: str | None = None,
) -> list[dict[str, Any]]:
    suffix = instrument_suffix or derive_instrument_suffix(mysql, account_id, trader_id)
    return mysql.query(
        RETURNS_SQL,
        {
            "account_id": account_id,
            "trader_id": trader_id,
            "start_date": start_date,
            "end_date": end_date,
            "instrument_suffix": suffix,
        },
    )
