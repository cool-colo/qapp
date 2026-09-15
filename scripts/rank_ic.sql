-- Rank IC for one signal date, matching the "normal" logic of web/signal_quality_report.py
-- (信号质量 tab). Real-time / trailing-edge / benchmark / LS10 / TopN logic stripped out.
--
-- Edit to run:
--   * Date  : the three toDate('2025-09-10') below (adjpx x2, preds x1).
--   * Table : daily_model_predictions in the preds CTE.
--   * Holding days h : currently h=3 -> exit offset 4 (h+1), window 4 FOLLOWING (h+1),
--                      gate avail = 5 (h+2). For h=1 -> 2,2,3 ; for h=5 -> 6,6,7.
--
-- Label = adjusted open-to-open forward return: buy t+1 open, sell t+(h+1) open,
--         adjusted price = open * adj_factor (dws_stock_factor_wide).
-- RankIC = per-day Spearman rankCorr(score, label), NULL when the day has < 3 names.
WITH
adjpx AS (
    SELECT source_code AS code, trade_date, open * adj_factor AS aopen
    FROM dws_stock_factor_wide
    WHERE trade_date >= toDate('2025-09-10') AND trade_date <= toDate('2025-09-10') + 20
      AND open > 0 AND adj_factor IS NOT NULL
),
lab AS (
    SELECT
        code, trade_date,
        leadInFrame(aopen, 1) OVER w AS entry_px,   -- buy t+1 open
        leadInFrame(aopen, 4) OVER w AS exit_px,    -- sell t+(h+1) open; h=3 -> 4
        count()               OVER w AS avail
    FROM adjpx
    WINDOW w AS (PARTITION BY code ORDER BY trade_date
                 ROWS BETWEEN CURRENT ROW AND 4 FOLLOWING)   -- h+1 = 4
),
preds AS (
    SELECT concat(substring(stock_code, 3), '.', substring(stock_code, 1, 2)) AS code,
           pred_date, score
    FROM daily_model_predictions
    WHERE pred_date = toDate('2025-09-10')
),
joined AS (
    SELECT p.score AS score, l.exit_px / l.entry_px - 1 AS label
    FROM preds p
    INNER JOIN lab l ON p.code = l.code AND p.pred_date = l.trade_date
    WHERE l.avail = 5 AND l.entry_px > 0    -- full_window h+2 = 5
)
SELECT
    if(count() < 3, NULL, rankCorr(score, label)) AS rankic,
    count() AS sample_count
FROM joined
