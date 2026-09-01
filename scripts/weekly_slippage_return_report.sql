SET @start_date = '2026-08-14';
SET @end_date   = '2026-09-01';
SET @trader_id   = 'BIGQMT-002';
SET @instrument_suffix = '.BIGQMT';
SET @account_id = '66623901';

WITH
-- ========== 1. 滑点基础数据 ==========
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
    WHERE t.trade_date BETWEEN @start_date AND @end_date
        AND o.open_price IS NOT NULL
        AND o.open_price > 0
        AND t.price IS NOT NULL
        AND t.quantity IS NOT NULL
        AND t.quantity > 0
        AND UPPER(t.side) IN ('BUY', 'SELL')
        AND t.account_id = @account_id
        AND t.trader_id = @trader_id
),

-- ========== 2. 按日汇总滑点金额与成交金额 ==========
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

-- ========== 3. 盘前市值 ==========
before_market AS (
    SELECT
        trade_date,
        SUM(open_price * volume) AS before_market_value
    FROM live_position_snapshot
    WHERE snapshot_type = 'before_trading' and trader_id = @trader_id
        AND trade_date BETWEEN @start_date AND @end_date
        AND can_use_volume is not NULL
    GROUP BY trade_date
),

-- ========== 4. 盘后市值 ==========
after_market AS (
    SELECT
        p.trade_date,
        SUM(COALESCE(t.last_price, t.last_close) * p.volume) AS after_market_value
    FROM live_position_snapshot p
    LEFT JOIN live_stock_tick_snapshot t
        ON p.stock_code = t.stock_code AND p.trade_date = t.trade_date
    WHERE p.snapshot_type = 'after_trading' AND p.trader_id = @trader_id
        AND p.trade_date BETWEEN @start_date AND @end_date
        AND t.instrument_id like CONCAT('%', @instrument_suffix)
        AND can_use_volume is not NULL
    GROUP BY p.trade_date
),

-- ========== 5. 日收益（汇总） ==========
daily_return AS (
    SELECT
        trade_date,
        return_amount
    FROM live_daily_stock_return
    WHERE stock_code = 'summary' and trader_id = @trader_id
        AND trade_date BETWEEN @start_date AND @end_date
        AND account_id = @account_id
),

-- ========== 6. 中证全指 000985.CSI（暴露收盘/昨收，用于端点式周累计；仅用于中间计算，不再输出） ==========
csi_all AS (
    SELECT
        trade_date,
        close     AS close_price,                                          -- 当日收盘
        pre_close AS pre_close,                                            -- 当日昨收
        pct_chg / 100 AS daily_rate
    FROM index_eod_price
    WHERE ts_code = '000985.CSI'
        AND trade_date BETWEEN @start_date AND @end_date
),

-- ========== 7. 中证1000 399852.SZ（暴露收盘/昨收，用于端点式周累计；目前暂无数据） ==========
csi1000 AS (
    SELECT
        trade_date,
        MAX(last_price) AS close_price,                                     -- 当日收盘
        MAX(last_close) AS pre_close,                                       -- 当日昨收
        (MAX(last_price) - MAX(last_close)) / NULLIF(MAX(last_close), 0) AS daily_rate
    FROM live_stock_tick_snapshot
    WHERE stock_code = '399852.SZ'
        AND snapshot_type = 'after_trading'
        AND trade_date BETWEEN @start_date AND @end_date
    GROUP BY trade_date
),

-- ========== 8. 所有出现过的日期 ==========
all_dates AS (
    SELECT trade_date FROM daily_slippage
    UNION SELECT trade_date FROM before_market
    UNION SELECT trade_date FROM after_market
    UNION SELECT trade_date FROM daily_return
    UNION SELECT trade_date FROM csi_all
    UNION SELECT trade_date FROM csi1000
),

-- ========== 9. 合并每日指标 ==========
combined AS (
    SELECT
        d.trade_date,
        WEEKOFYEAR(d.trade_date) AS week_num,
        bm.before_market_value,
        am.after_market_value,
        r.return_amount,
        -- 日收益率：纯持仓盈亏 / (盘后市值 − 当日收益)
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

-- ========== 10. 周累计 ==========
windowed AS (
    SELECT
        c.*,
        -- 策略周累计收益金额（金额求和）
        SUM(return_amount) OVER w AS week_cum_return_amount,
        -- 策略周累计收益率：日收益率复利（时间加权，不受入金影响）
        EXP(SUM(LN(1 + strat_daily_rate)) OVER w) - 1 AS week_cum_strat_rate,
        -- 中证全指周累计收益率：(当日收盘 − 本周首日昨收) / 本周首日昨收
        (csi_all_close - FIRST_VALUE(csi_all_preclose) OVER w)
            / NULLIF(FIRST_VALUE(csi_all_preclose) OVER w, 0) AS week_cum_csi_all_rate,
        -- 中证1000周累计收益率：(当日收盘 − 本周首日昨收) / 本周首日昨收
        (csi1000_close - FIRST_VALUE(csi1000_preclose) OVER w)
            / NULLIF(FIRST_VALUE(csi1000_preclose) OVER w, 0) AS week_cum_csi1000_rate
    FROM combined c
    WINDOW w AS (
        PARTITION BY week_num
        ORDER BY trade_date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )
)

-- ========== 11. 最终输出 ==========
SELECT
    w.trade_date AS 日期,
    @account_id   AS 账号ID,
    ROUND(w.before_market_value, 0) AS 盘前市值,
    ROUND(w.after_market_value, 0)  AS 盘后市值,
    ROUND(w.return_amount, 0)       AS 日收益,
    CONCAT(ROUND(w.strat_daily_rate * 100, 2), '%')       AS 收益率,
    CONCAT(ROUND(w.csi1000_daily_rate * 100, 2), '%')     AS 中证1千收益率,
    -- 日超额收益：策略当日收益率 − 中证1000当日收益率
    CONCAT(ROUND((w.strat_daily_rate - w.csi1000_daily_rate) * 100, 2), '%') AS 超额收益,
    CONCAT('W', w.week_num)  AS 周,
    ROUND(w.week_cum_return_amount, 0)   AS 周收益,
    CONCAT(ROUND(w.week_cum_strat_rate * 100, 2), '%')    AS 周收益率,
    CONCAT(ROUND(w.week_cum_csi1000_rate * 100, 2), '%')  AS 中证1千周收益率,
    -- 周超额收益：策略周累计收益率 − 中证1000周累计收益率
    CONCAT(ROUND((w.week_cum_strat_rate - w.week_cum_csi1000_rate) * 100, 2), '%') AS 周超额收益,
    ROUND(w.buy_slippage_amount  / NULLIF(w.buy_trade_amount,  0) * 10000, 2) AS 买入滑点,
    ROUND(w.sell_slippage_amount / NULLIF(w.sell_trade_amount, 0) * 10000, 2) AS 卖出滑点,
    ROUND(w.total_slippage_amount / NULLIF(w.total_trade_amount, 0) * 10000, 2) AS 总滑点
FROM windowed w
ORDER BY w.trade_date;
