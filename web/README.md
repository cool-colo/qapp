# Live Trading Dashboard (`web/`)

A local, read-only dashboard for the live trading data this repo persists:

- **按日快照** — asset / positions / target portfolio / orders / trades for a chosen
  date + phase. Each stock row links to its **个股 K 线** view.
- **随时间** — time series: pick a metric (asset columns or return-report columns) and
  a date range; x = date, y = the selected column.
- **对比** — overlay multiple series on one chart, each = (source, account, metric).
  A preset plots 策略周累计收益率 vs 中证1000周累计收益率 for the current account.
- **个股 K 线** — daily candlestick from ClickHouse `dws_stock_factor_wide`, with this
  account's **buy (red ↑) / sell (green ↓)** fills overlaid as markers.

It is a self-contained consumer — nothing here is imported by the strategy / backtest
/ live trading code.

## Data sources

- **MySQL** `live_*` tables — asset/position/target/order/trade snapshots, plus
  `live_daily_stock_return` and `index_eod_price` used by the return report.
- **ClickHouse** (HTTP) — daily OHLCV bars.

实盘 (real) and 模拟盘 (simulation) live in **separate MySQL databases**; each is a
named entry under `sources` in the config. Multiple accounts within one database are
distinguished by the `(account_id, trader_id)` columns and auto-discovered — you do
not list accounts in the config.

## Return / slippage report

`web/returns_report.py` is a parameterized, numeric-output port of
`scripts/weekly_slippage_return_report.sql`. The CTE logic is kept identical; only the
`SET @var` lines (now query params) and the final presentation SELECT (now raw numeric
columns) differ. **If you change the report logic, update both files in lockstep.**

## Setup

```bash
# 1. install the web extra (adds fastapi, uvicorn, pymysql, pyyaml)
pip install -e '.[web]'

# 2. create the config from the template and edit it (or set the env vars it references)
cp web/config.example.yaml web/config.yaml
$EDITOR web/config.yaml     # web/config.yaml is gitignored

# 3. run (binds 127.0.0.1:8080)
bash web/run.sh
# then open http://127.0.0.1:8080
```

Config values may be literals or `${ENV}` / `${ENV:-default}` placeholders. A `.env`
file at the repo root is loaded automatically. Override the bind with
`DASHBOARD_HOST` / `DASHBOARD_PORT`.

## API

All endpoints return JSON (Decimals as floats). Interactive docs at `/api/docs`.

| Endpoint | Params |
|---|---|
| `GET /api/sources` | — |
| `GET /api/accounts` | `source` |
| `GET /api/dates` | `source, account, trader, table` |
| `GET /api/asset` | `source, account, trader, start, end, snapshot_type` |
| `GET /api/positions` | `source, account, trader, date, snapshot_type` |
| `GET /api/target` | `source, account, trader, date, snapshot_type` |
| `GET /api/orders` | `source, account, trader, date?, stock_code?` |
| `GET /api/trades` | `source, account, trader, date?, stock_code?` |
| `GET /api/returns` | `source, account, trader, start, end, instrument_suffix?` |
| `GET /api/kline` | `stock_code, start, end` |
| `GET /api/kline_with_trades` | `source, account, trader, stock_code, start, end` |

## Security note

This tool has **no authentication**. Keep it on `127.0.0.1`. Database credentials must
come from `web/config.yaml` / env / `.env` — do not hardcode them. Note the repo
currently contains committed credentials in `.envs/` and `start_*.sh`; treat those as
compromised and rotate them.
