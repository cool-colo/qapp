# AGENTS.md

Guidance for AI coding agents working in this repository.

## Repository Instructions (unchanging)

- Business logic and trading strategy behavior must use Nautilus (`nautilus_trader`) first.
- Do not call QMT proxy APIs or QMT adapter internals directly for business logic or strategy behavior unless the user explicitly requests it.
- Treat direct proxy/adapter access as infrastructure plumbing only, never strategy logic.
- Do not add fallback or legacy-compatibility behavior unless the user explicitly requests it.
- Do not use `getattr` for expected internal interfaces; depend on explicit APIs and fail fast.
  - Related: tests must not use `object.__new__` tricks to bypass `__init__`; use real construction paths (e.g. dedicated `for_testing` classmethods).

## Project overview

`qapp` is a China A-share **daily model-prediction trading strategy** built on
[Nautilus Trader](https://nautilustrader.io), with both a backtest path and a live
trading path. The design principle: **one strategy class runs unchanged in backtest
and live.** All database access, venue-symbol conversion and result persistence live
*outside* the strategy, so the same trading logic is wired into a `BacktestEngine`
and a live `TradingNode`.

- The core strategy is `TargetModelPredictionsStrategy`
  (`strategies/model_prediction_targets.py`, the main strategy; ~1350 lines). It is
  a deterministic consumer of pre-loaded reference data (signals, trading dates,
  listed dates, ST/suspension calendars, last closes) passed in through its config.
  It never touches ClickHouse/MySQL/QMT itself.
- Data comes from **ClickHouse** (bars, model predictions, index membership,
  ST/suspension calendars). Backtest results and live daily snapshots are persisted
  to **MySQL**. The live Nautilus cache can be backed by **Redis** (`--use-redis`).
- Two venue adapters are supported for live trading: QMT (`quant-qmt-proxy`, HTTP/WS)
  and Big QMT (大QMT, a Redis RPC bridge). Both build the *same* node through
  `lives/_target_model_node.py`; only the venue client configs/factories differ.

## Environment

- Python **3.12** (the deployment conda env is named `quant`). The repo runs from a
  `miniconda` env (`source /home/fanglicheng/miniconda3/etc/profile.d/conda.sh && conda activate quant`).
- **`nautilus_trader` is NOT installed as a package.** It is an out-of-tree checkout
  (default `/data/flc/code/quant/nautilus_trader`, overridable via `NAUTILUS_TRADER_PATH`).
  Every runnable script prepends `PROJECT_ROOT` and `NAUTILUS_TRADER_PATH` to `sys.path`
  before importing Nautilus. Run scripts from the repo root so `backtests`/`lives`/
  `strategies` import as top-level packages (there is no `src/` layout).
  **Do not add nautilus_trader to `pyproject.toml` dependencies** — pip would resolve a
  PyPI version that does not match the local checkout.
- Project dependencies (`pyproject.toml`): only `numpy` + `pandas` are hard deps.
  Optional extras: `mysql` (`pymysql`, `sqlalchemy` — used lazily by the result
  writers), `monitoring` (`prometheus_client` — the exporter no-ops with a warning if
  absent), `all` (both).
- `pyyaml` is used lazily by `strategies/strategy_params.py` to load the config file.
- Nearly every CLI argument has an environment-variable default (e.g.
  `--start` ↔ `BACKTEST_START_DATE`, `--clickhouse-url` ↔ `CLICKHOUSE_URL`,
  `--max-positions` ↔ `MODEL_MAX_POSITIONS`, `--config` ↔ `STRATEGY_CONFIG_FILE`). Prefer
  CLI flags for ad-hoc runs; env vars are the deployment path (see `start_*.sh`).

## Common commands (run from the repo root)

```bash
# Main backtest (target model-prediction strategy); --config is required
python -m backtests.target_model_predictions.run_backtest \
  --config configs/strategy.yaml --start 2026-07-01 --end 2026-07-31 --all-stocks

# Faster variants: inspect signals only / build only (no engine run)
python -m backtests.target_model_predictions.run_backtest --config configs/strategy.yaml --print-signals --all-stocks
python -m backtests.target_model_predictions.run_backtest --config configs/strategy.yaml --load-only --all-stocks

# Persist results to MySQL + write CSV reports
python -m backtests.target_model_predictions.run_backtest --config configs/strategy.yaml \
  --all-stocks --write-results --report-dir output/run1 --benchmark-code 399300.SZ

# Live trading (Big QMT / QMT venue; the same strategy)
python lives/live_bigqmt_target_model_predictions.py --config configs/strategy.yaml --account-id <id> --all-stocks
python lives/live_qmt_target_model_predictions.py --config configs/strategy.yaml --account-id <id> --all-stocks
# Validate wiring without connecting
python lives/live_bigqmt_target_model_predictions.py --config configs/strategy.yaml --build-only --all-stocks --account-id <id>

# Tests
python -m pytest -q

# Operational scripts
python -m scripts.sync_trade_calendar            # ClickHouse calendar -> MySQL + DingTalk (cron)
python -m scripts.full_tick_snapshot_to_clickhouse  # whole-market full tick -> ClickHouse ODS
python -m scripts.calculate_daily_returns --trade-date 2026-07-27
python bin/check_live_status.py                  # standalone health poll (alerting is mocked)
```

Secondary/example strategies, independent of the main strategy: the MACD smoke
backtest (`python -m backtests.macd_smoke.run_backtest`) and the standalone EMA-cross
script (`python backtests/qmt_ema_cross_clickhouse.py`).

There is **no build step, no linter/formatter config** (no `[tool.ruff]`/`[tool.black]`
in `pyproject.toml`) and **no CI** (no `.github/`). Code style follows the surrounding
files (see Code Style below).

## Repository layout

- `strategies/` — the trading logic (Nautilus `Strategy` classes and helpers).
  - `model_prediction_targets.py` — **main strategy** `TargetModelPredictionsStrategy`
    (planner-driven daily planning). `TargetModelPredictionsStrategyConfig` inherits
    `TargetQuantityStrategyConfig`.
  - `target_quantities.py` — `TargetQuantityStrategy` (base executor: target
    quantities, convergence loop, order submission, cash gating, full-tick handling,
    QMT `open_int` → `MarketStatusAction` mapping). 2500 lines; the most intricate file.
  - `model_target_planners/` — target weight planning abstraction:
    `base.py` (dataclasses: `ModelTargetPlan`, `TargetInfo`, `CurrentHolding`,
    `ModelTargetPlanningRequest`; `ModelTargetPlanner` interface),
    `factory.py` (`build_model_target_planner`), `risk_manager.py`
    (`RiskManagerModelTargetPlanner` — HTTP POST to an external risk-optimizer service,
    default `http://127.0.0.1:8000`, risk model id
    `cn_a_basic_constraints_integer_lots`).
  - `pricing/` — stateless limit-price policies (`PriceStrategy` ABC,
    `OpenOffsetSellPriceStrategy`/`OpenOffsetBuyPriceStrategy`, `PriceContext`).
    Sells anchor to today's open minus a bps offset; after N cancels the price escalates
    by walking the book.
  - `order_splitter/notional_order_splitter.py` — slices large orders by CNY notional
    (`order_slice_notional` in the YAML).
  - `strategy_params.py` — `StrategyParams.from_yaml()`; the shared YAML config loader
    (unknown keys **raise**, missing keys fall back to the dataclass defaults).
  - `model_common.py` — shared signal-event dataclasses + normalization helpers.
  - `execution_reconciliation.py`, `async_scheduling.py` — execution-state reconciliation
    and cross-thread asyncio scheduling helpers.
  - `macd_smoke.py`, `emac_cross.py` — secondary/example strategies.
- `lives/` — live trading entrypoints and node wiring.
  - `live_qmt_target_model_predictions.py` — QMT proxy entrypoint (+ helpers reused by
    BigQMT: env pre-parse, daily log-file naming, snapshot args).
  - `live_bigqmt_target_model_predictions.py` — Big QMT (Redis RPC bridge) entrypoint.
  - `_target_model_node.py` — **shared node builder** `build_target_model_node()`:
    `LiveTargetModelPredictionsStrategy` (adds the daily 09:10 reference-data refresh,
    execution reconciliation, DingTalk event reporting) + `TradingNode` wiring +
    `SnapshotRecorder` + Prometheus exporter + status server. `VenueClients` dataclass
    carries venue client configs/factories.
  - `live_common.py` — shared CLI parsing (all `--*` args and their env defaults),
    `LivePredictionDataLoader` (ClickHouse loading + venue broker snapshots),
    symbol helpers (`qmt_symbol`, `stock_code_from_instrument_id`), Redis cache config.
  - `broker_data_source.py` — `BrokerDataSource` abstraction: `QmtBrokerDataSource`
    (HTTP proxy) vs `BigQmtBrokerDataSource` (Big QMT RPC) returning the same
    normalized full-tick/position/order/trade shapes.
  - `snapshot_recorder.py` — `SnapshotRecorder` Nautilus `Actor`: persists daily
    before/after-trading snapshots + order/trade lifecycle into MySQL `live_*` tables.
  - `monitoring.py` — `PrometheusExporter` actor (`prometheus_client`, optional dep).
  - `status_server.py` — stdlib HTTP `/health` server (node `health_status()` JSON).
  - `live_reconciliation.py` — triggers venue execution-state reconciliation at
    strategy start / data refresh / pre-open (09:15).
  - `sell_all_sellable.py` — standalone operational tool to flatten positions (one of
    the few places that legitimately reaches toward QMT plumbing).
- `backtests/` — backtest path.
  - `target_model_predictions/run_backtest.py` — **main backtest runner**. Builds a
    live-like deterministic daily event sequence (`DailyPlanningEvent` at 09:30:01,
    heartbeat `QuoteTick` at 09:30:00, execution bars at 09:31:00), runs the same
    strategy, reconstructs the daily portfolio independently from fills vs close prices
    (`reconstruct_daily_portfolio`), reports and (optionally) persists to MySQL.
  - `base.py` (`BaseBacktest`), `reporting.py` (`BacktestReportProcessor`) — shared,
    strategy-agnostic helpers (symbol conversion, report CSV export, benchmark
    enrichment, result-writer records). `common.py` — benchmark loading.
  - `data_providers/` — `ClickHouseBarDataProvider`, `ClickHouseModelPredictionDataProvider`
    (signals + universe + calendars → `PredictionDataBundle`),
    `ClickHouseDailyStockDataProvider` (`DailyStockData` EOD + price-limit data),
    `model_base.py` (request/bundle/signal dataclasses + provider ABC), `clickhouse.py`
    (connection + bar schema + SQL helpers).
  - `result_writers/` — `MySQLResultWriter` (experiment/signals/targets/orders/trades/
    daily metrics), `writer.py` (`ResultWriter`/`NullResultWriter` ABC), `records.py` +
    `live_records.py` (record dataclasses incl. the `live_*` snapshot tables),
    `live_writer.py` (`LiveSnapshotWriter`, thread-safe SQLAlchemy pool used by the live
    recorder).
  - `macd_smoke/`, `qmt_ema_cross_clickhouse.py`, `data_providers/clickhouse_daily_stock.py`
    — secondary/example paths.
- `market_data.py` — `DailyStockData` + `index_daily_stock_data()` + `latest_closes()`
  (small shared module at repo root).
- `scripts/` — operational tools (`calculate_daily_returns.py`,
  `sync_trade_calendar.py`, `full_tick_snapshot_to_clickhouse.py`, `full_tick_ods_to_dwd.sql`).
- `bin/` — standalone scripts (`check_live_status.py`); not an importable package.
- `monitoring/` — `dingtalk_alert.py` (DingTalk custom-robot signed webhook + minimal
  `.env` loader; stdlib + `requests` only, no python-dotenv). See `monitoring/README.md`.
- `configs/strategy.yaml` — strategy-tuning config template (annotated).
- `.plans/` — design/plan documents for prior changes (read before editing related code).
- `logs/` — runtime logs (gitignored).
- `tests/` — unittest-style test suite (see Testing).

## Runtime architecture

### Strategy flow (shared by live and backtest)

`TargetModelPredictionsStrategy` extends the `TargetQuantityStrategy` executor:

1. A daily trigger calls `_process_trading_day_once(trading_date, trigger)`:
   seed active positions from the portfolio → build the daily plan
   (`compute_daily_target_plan`) → convert the plan to target quantities → feed
   `update_target_quantities` on the executor.
2. **Planning** gathers signal candidates + current holdings, then delegates to the
   configured `ModelTargetPlanner` (default `risk_manager` — an external optimizer HTTP
   service) to produce a `ModelTargetPlan` (per-stock target weights/quantities).
   Local business rules run as *exclusion filters* over candidates/holdings, e.g.
   `_holding_exclusion` (stop-loss / trailing take-profit keep a position out of
   `current_holdings` so the optimizer unwinds it), `_entry_skip_reason`
   (`min_listed_days`, name prefixes `*ST`/`ST`/`退市`, consecutive up-limit days,
   `max_open_gap_up`, suspensions).
3. **Execution** (`TargetQuantityStrategy`): a `_converge_to_target` loop reconciles the
   portfolio toward target quantities. Sells are submitted before buys; buys are gated
   on free cash (`balance_free()` with `cash_buffer_percent` haircut) and parked in
   `_deferred_buys` when cash is short, drained as sells fill. Rejected orders for
   insufficient funds (QMT 废单/error 260200) go into a backoff set; sellable-exhaustion
   denials stop retrying that instrument for the day; unfilled orders are cancelled
   after `unfilled_timeout_secs` and repriced. Limit stops: when a symbol sits at its
   up/down price limit (or is suspended — QMT full-tick `open_int` status), the symbol
   is frozen for the day (`limit_stop_mode: freeze_symbol`).
4. The strategy records `signal_events`/`target_events`/`order_events` in memory; the
   backtest runner / snapshot recorder persist them — the strategy itself never writes
   anywhere.

### Live node

`build_target_model_node()` builds a single `TradingNode` containing:
the strategy (`LiveTargetModelPredictionsStrategy` — daily refresh timer at 09:10
Asia/Shanghai that reloads signals/universe from ClickHouse and re-subscribes bars),
a `SnapshotRecorder` actor (before/after-trading MySQL snapshots + live target-plan
persistence), a `PrometheusExporter` actor (if `--metrics-port > 0`), and an external
`LiveStatusServer` (`/health`). Key knob: `inflight_check_threshold_ms=20_000` — the
QMT confirmation path is slow (~6s), and a smaller threshold caused duplicated inferred
fills/overfill rejections; do not lower it casually.

Note: `--build-only` is the canonical fast way to validate live wiring (build the node,
then dispose) without connecting to the venue.

### Backtest

The runner builds a deterministic, live-like event sequence per trading day from
ClickHouse bars: `DailyPlanningEvent` at 09:30:01 carrying a full-tick-equivalent
snapshot (open from the daily bar; suspended stocks get `open_int=1`), a heartbeat
`QuoteTick` at 09:30:00, and execution bars stamped 09:31:00. The strategy is the same;
`BacktestTargetModelPredictionsStrategy` only replaces bar/L2 subscriptions with
`on_data(DailyPlanningEvent)`. The equity curve is computed by `reconstruct_daily_portfolio`
(replay fills against close prices) rather than trusting engine account reports.

## Config conventions

- `configs/strategy.yaml` is the annotated template; pass it with `--config` (or
  `STRATEGY_CONFIG_FILE`). It is **required** by the main backtest and both live
  entrypoints. Unknown keys raise (typos fail loudly); omitted keys use defaults.
- The YAML holds only **strategy-tuning** knobs: position sizing
  (`max_position_percent`, `stop_loss`, `trailing_take_profit`,
  `min_listed_days`, ...), order lifecycle (`unfilled_timeout_secs`,
  `resubmit_interval_secs`, `cash_buffer_percent`, `order_slice_notional`,
  `limit_stop_mode`, `stop_time`), planner/risk manager
  (`target_weight_planner`, `risk_manager_*`), name filters, trading windows, full-tick,
  order id tag, log sample rates.
- **Live vs backtest differ only in `risk_manager_mode`** (`live` vs `backtest`;
  `simulation` also allowed). Keep a separate YAML per context or edit that one key.
- Node/infra params (account id, redis, clickhouse, logging, metrics ports) and the
  data-universe params (`--all-stocks`, `--stock-codes`, `--top-frac`,
  `--max-positions`, `--exchange-timezone`, `--history-days`) stay on CLI/env —
  notably `max_positions` and `exchange_timezone` are consumed *before* the strategy
  is built, so they cannot live in the YAML.

## Symbol formats (easy to get wrong)

- ClickHouse bar symbol: `stock:000001.SZ` (via `data_symbol`).
- QMT symbol: `000001.SZ` / `.SH` / `.BJ` (via `qmt_symbol`, which also normalizes
  `.XSHE`/`.XSHG`/`.BJSE` and bare `SZ000001` forms).
- Nautilus instrument id: `<symbol>.QMT` (QMT venue) or `<symbol>.BIGQMT` (Big QMT).
- `stock_code_from_instrument_id` strips the `.QMT`/`.BIGQMT` suffix.

## Data model notes

- Universe: China A-shares (SH/SZ/BJ). Strategy filters cover ST/*ST/退市 name
  prefixes, suspensions, new-listing minimum days (`min_listed_days`), optional BJ
  exclusion, consecutive up-limit (`consecutive_up_limit_days`) and open gap-up
  (`max_open_gap_up`) entry guards.
- Weights are set by the external risk manager (equal-weight capped at
  `max_position_percent` was the previous default planner); `max_positions` caps the
  book; holdings rotate on `holding_days` (backtest cadence; the live target-timer path
  replans daily from the optimizer).
- ClickHouse: predictions table default `daily_model_predictions`
  (override with `--predictions-table`, e.g. model experiment tables in the launcher
  scripts), daily bars (schema in `backtests/data_providers/clickhouse.py`),
  `dwd_index_eod_price` for benchmarks, `dwd_trade_calendar` (synced to MySQL
  `trade_calendar` by `scripts/sync_trade_calendar.py`).
- MySQL: backtest experiment tables (`experiment`, `experiment_param`, `signal`,
  `target_portfolio`, `order`, `trade`, daily account/performance/position, summary
  metrics) written by `MySQLResultWriter`; live `live_*` tables
  (`live_asset_snapshot`, `live_position_snapshot`, `live_target_portfolio`,
  `live_stock_tick_snapshot`, `live_order`, `live_trade`) written by
  `SnapshotRecorder` via `LiveSnapshotWriter`; `daily_returns`
  (from `scripts/calculate_daily_returns.py`).
- Redis: Nautilus cache Redis (`--use-redis` + `QMT_REDIS_*`; URL-encode username/
  password before passing to Nautilus, see `live_common.build_cache_config`) and a
  **separate** Big QMT RPC-bridge Redis (`BIGQMT_REDIS_HOST`/`_PORT`/`_DB`/`_PASSWORD`).
  The two are unrelated; do not conflate them.

## Code style guidelines

- Match the surrounding file: this repo uses `from __future__ import annotations`,
  `dataclasses` heavily, `# noqa: E402` after the sys.path preamble, and lazy imports
  inside functions for optional/venue-specific dependencies.
- Keep business logic Nautilus-first (see Repository Instructions). Venue-specific
  client wiring belongs in the entrypoints / node builder, not the strategy.
- Fail fast: validate config/args eagerly (`parser.error`, `ValueError` with the known
  keys), raise on ambiguous data (`index_daily_stock_data` raises on duplicate keys),
  do not silently tolerate unknown YAML keys.
- Comments/docstrings are predominantly English, with Chinese used where the domain
  terms are Chinese (停牌, 废单, 集合竞价). Use English by default.
- Follow the existing event-dataclass + strategy-in-memory-records pattern when adding
  strategy observability rather than writing from the strategy.
- Do not reformat/rename unrelated code; keep diffs scoped.

## Testing instructions

- Run: `python -m pytest -q` from the repo root (208 tests collected, no config in
  `pyproject.toml`).
- Style: mostly `unittest.TestCase` classes with `MagicMock` stubs of the Nautilus
  `Strategy`/`Cache`; some pytest-style plain functions exist (`test_bigqmt_live_config.py`)
  but do not add new dependencies (no pytest plugins used).
- Heavy use of `unittest.mock.patch` for ClickHouse/MySQL/HTTP boundaries — tests never
  touch real databases or the venue.
- **Current state (2026-08-17): 192 pass / 16 fail.** The failures are pre-existing
  staleness between tests and the implementation, e.g.
  `tests/test_live_common_full_tick.py` (expects the pre-`broker_data_source` refactor
  proxy-style `_post_full_tick` API), `tests/test_target_live_config.py` (expects old
  `live_order` column DDL / env-arg behavior) and
  `tests/test_model_target_planners.py` (expects `can_buy`/`can_sell` in the risk
  manager payload, which are currently commented out). When you change the
  implementation, update these tests deliberately rather than assuming a clean suite.
- The working tree currently carries uncommitted changes in
  `backtests/result_writers/live_records.py`, `backtests/result_writers/live_writer.py`,
  and `lives/snapshot_recorder.py` (Big QMT order/trade timestamp normalization to
  `order_time`/`trade_time`); keep them in mind when writing those files.

## Deployment & operations

- Deployment scripts at repo root (`start_*.sh`, `restart_*.sh`, `start_qapp.sh`) are
  shell launchers: activate conda `quant`, export infrastructure env vars, then
  `exec python lives/...`. Live nodes run inside tmux sessions (`bigqmt`, `bigqmt2`)
  and are restarted with C-c + relaunch via the `restart_*` scripts. There are two
  BigQMT instances (accounts 86008933 / 66623901, metrics ports 9110/9120, status
  ports 9210/9220) plus a legacy QMT node (account 86904088).
- Secrets live in `.envs/dev/*.env` (git-ignored) and inside the launcher/restart
  scripts; credentials are passed via env vars or `--env-file` (resolution precedence:
  explicit `--env-file` → `<script dir>/.env` → `<cwd>/.env`).
- Alerting: DingTalk custom robot (加签/signed mode) via
  `monitoring/dingtalk_alert.py`; env `DINGTALK_ACCESS_TOKEN`/`DINGTALK_SECRET`.
  Missing credentials disable delivery without stopping the node.
- Monitoring: in-process Prometheus exporter (`--metrics-port`, metrics prefixed
  `qapp_`; see `monitoring/README.md`) and the `/health` status server
  (`--status-port`), polled by `bin/check_live_status.py`.
- Cron-style jobs: `scripts/sync_trade_calendar.py` (every morning),
  `scripts/full_tick_snapshot_to_clickhouse.py` (intraday market-data capture),
  `bin/check_live_status.py` (every 5 min during trading hours).

## Security considerations

- `.envs/` and `.env.*` are git-ignored; never print, copy, or commit their contents,
  and never paste live DB/Redis credentials into code, logs, or docs.
- Seeded launcher scripts (`start_*.sh`) contain production credentials — treat any
  output of those scripts (log lines echoing env) as sensitive.
- DingTalk access tokens/secrets are credentials; keep them out of test output.
- Do not log full order/trade payloads at debug level in normal runs; the snapshot
  recorder stores raw QMT payloads JSON in MySQL by design (that is the persistence
  contract, not a logging habit).