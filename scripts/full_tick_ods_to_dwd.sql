-- ODS -> DWD full-tick dedup/sync for a single trading day.
-- Replace {date} with the target 'YYYY-MM-DD' before running.
-- Dedup rule: keep the row with the latest ingest_time per (trade_date, symbol).

CREATE TABLE IF NOT EXISTS `dwd_stock_full_tick_snapshot` (
    `trade_date` Date,
    `symbol` String,
    `ingest_time` DateTime,
    `time_ms` Int64,
    `last_price` Float64,
    `open` Float64,
    `high` Float64,
    `low` Float64,
    `last_close` Float64,
    `amount` Float64,
    `volume` Int64,
    `pvolume` Int64,
    `open_int` Int64,
    `stock_status` Int32,
    `last_settlement_price` Float64,
    `transaction_num` Int64,
    `ask_price` Array(Float64),
    `bid_price` Array(Float64),
    `ask_vol` Array(Int64),
    `bid_vol` Array(Int64)
)
ENGINE = ReplacingMergeTree(ingest_time)
PARTITION BY trade_date
ORDER BY (trade_date, symbol);

-- Idempotent per-day replace: drop then reload the day's partition.
ALTER TABLE `dwd_stock_full_tick_snapshot` DROP PARTITION '{date}';

INSERT INTO `dwd_stock_full_tick_snapshot` (trade_date, symbol, ingest_time, time_ms, last_price, open, high, low, last_close, amount, volume, pvolume, open_int, stock_status, last_settlement_price, transaction_num, ask_price, bid_price, ask_vol, bid_vol)
SELECT
    trade_date,
    symbol,
    max(ingest_time) AS ingest_time_max,
    argMax(time_ms, ingest_time) AS time_ms,
    argMax(last_price, ingest_time) AS last_price,
    argMax(open, ingest_time) AS open,
    argMax(high, ingest_time) AS high,
    argMax(low, ingest_time) AS low,
    argMax(last_close, ingest_time) AS last_close,
    argMax(amount, ingest_time) AS amount,
    argMax(volume, ingest_time) AS volume,
    argMax(pvolume, ingest_time) AS pvolume,
    argMax(open_int, ingest_time) AS open_int,
    argMax(stock_status, ingest_time) AS stock_status,
    argMax(last_settlement_price, ingest_time) AS last_settlement_price,
    argMax(transaction_num, ingest_time) AS transaction_num,
    argMax(ask_price, ingest_time) AS ask_price,
    argMax(bid_price, ingest_time) AS bid_price,
    argMax(ask_vol, ingest_time) AS ask_vol,
    argMax(bid_vol, ingest_time) AS bid_vol
FROM `ods_stock_full_tick_snapshot`
WHERE trade_date = '{date}'
GROUP BY trade_date, symbol;

