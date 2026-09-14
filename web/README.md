# Live Trading Dashboard (`web/`)

A local, read-only dashboard for the live trading data this repo persists:

- **实时信息** — the live node's 持仓 table plus a 资产 sub-page: the broker 资金
  key/value card next to a **资产构成** pie of 持仓市值 / 可用资金 / 冻结资金.
- **按日快照** — asset / positions / target portfolio / orders / trades for a chosen
  date + phase. Each stock row links to its **个股 K 线** view.
- **随时间** — time series: pick one or more metrics from a checkbox list (asset
  columns or return-report columns) and a date range; x = date, one line per selected
  column. Rate columns move to a right-hand % axis when mixed with magnitude columns,
  so both stay readable. Default range = one month ago → today. Checking/unchecking a
  metric, switching 数据类型, or changing either date redraws immediately; **绘制** stays
  as an explicit re-fetch of the same parameters.
- **对比** — overlay multiple series on one chart, each = (source, account, metric).
  A preset plots 策略周累计收益率 vs 中证1000周累计收益率 for the current account.
- **个股 K 线** — daily candlestick from ClickHouse `dws_stock_factor_wide`, with this
  account's **buy (red ↑) / sell (green ↓)** fills overlaid as markers. Changing a date
  or the 显示买卖点 option redraws right away (as it does in **对比**, where a date change
  re-plots whenever at least one series is added).

Every table goes through one shared renderer, which prefixes a leading **序号**
row-number column: it numbers the *visible* rows, so it follows the current sort/filter
and stays pinned to the left edge while a wide table scrolls sideways.

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
| `GET /api/config` | — |
| `GET /api/accounts` | `source` |
| `GET /api/dates` | `source, account, trader, table` |
| `GET /api/asset` | `source, account, trader, start, end, snapshot_type` |
| `GET /api/positions` | `source, account, trader, date, snapshot_type` |
| `GET /api/target` | `source, account, trader, date, snapshot_type` |
| `GET /api/orders` | `source, account, trader, date?, stock_code?` |
| `GET /api/trades` | `source, account, trader, date?, stock_code?` |
| `GET /api/returns` | `source, account, trader, start, end, instrument_suffix?` |
| `GET /api/signal_quality` | `account, trader, start, end, holding_days=3, predictions_table?` |
| `GET /api/kline` | `stock_code, start, end` |
| `GET /api/kline_with_trades` | `source, account, trader, stock_code, start, end` |
| `GET /api/realtime/positions` | `account, trader` |
| `GET /api/strategy/targets` | `account, trader` |
| `GET /api/control/state` | `account, trader` |
| `POST /api/control/suspend` | `account, trader` |
| `POST /api/control/resume` | `account, trader` |
| `POST /api/control/sell_all` | `account, trader` |
| `POST /api/control/sell` | body: `{account, trader, stock_code}` |

The `realtime/*` and `control/*` endpoints proxy to the **per-account** live-node
control API. Each account (`account_id/trader_id`) runs its own live node (qapp) with
its own `--control-port`, so the node URL is configured under the top-level `node_api:`
map keyed by `"account_id/trader_id"` (**not** per source) — see `config.example.yaml`.
The node's `X-Control-Token` stays server-side. `GET /api/accounts` returns a
`has_node_api` flag per account so the UI enables the 实时信息 / 交易管理 tabs only for
accounts that have a node configured.

`GET /api/signal_quality` also reads the node's read-only `GET
/realtime/whole_market_ticks` (whole 京沪深A full-tick snapshot: `stock_code, open,
last_price, last_close`) best-effort, to approximate the label for the newest signal
date that has no offline forward window yet (`label_source = realtime_intraday`); a node
error just drops those live rows. Skipped when `predictions_table` is overridden.

**Selling is opt-in.** `sell_enabled` in the config (**default false**, also settable as
`WEB_SELL_ENABLED=true`) gates every sell action: `GET /api/config` exposes the flag so
the UI renders the per-row **卖出** and 交易管理's **全部卖出** buttons grayed-out, and
`POST /api/control/sell` / `POST /api/control/sell_all` answer **403** while it is off —
the grayed buttons are courtesy, the 403 is the gate. Suspend / resume are unaffected.

## Security note

This tool has **no authentication**. Keep it on `127.0.0.1`. Database credentials must
come from `web/config.yaml` / env / `.env` — do not hardcode them. Note the repo
currently contains committed credentials in `.envs/` and `start_*.sh`; treat those as
compromised and rotate them.
