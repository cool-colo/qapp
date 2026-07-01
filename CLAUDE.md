# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository conventions (from AGENTS.md)

- Business logic and trading strategy behavior must go through Nautilus (`nautilus_trader`) first.
- Do **not** call QMT proxy APIs or QMT adapter internals directly for business logic or strategy behavior unless the user explicitly asks. Treat direct proxy/adapter access as infrastructure plumbing, never strategy logic.

## Environment

- Python 3.12. There is no `requirements.txt`, `pyproject.toml`, or test suite in this repo — it depends on an out-of-tree Nautilus checkout.
- **`nautilus_trader` is not installed as a package.** Every runnable script prepends `PROJECT_ROOT` and `NAUTILUS_TRADER_PATH` (default `/data/flc/code/quant/nautilus_trader`, overridable via the `NAUTILUS_TRADER_PATH` env var) to `sys.path`. Run scripts from the repo root so `PROJECT_ROOT` resolves and `backtests`/`strategies`/`lives` import as top-level packages.
- Data comes from **ClickHouse** (bars, predictions, index membership, ST/suspension calendars). Backtest results are optionally persisted to **MySQL**. Live trading optionally uses **Redis** for the Nautilus cache.
- Nearly every argument has a corresponding environment-variable default (e.g. `--start` ↔ `BACKTEST_START_DATE`, `--clickhouse-url` ↔ `CLICKHOUSE_URL`, `--max-positions` ↔ `MODEL_MAX_POSITIONS`). Prefer CLI flags when running ad-hoc; env vars are the deployment path.

## Common commands

Run everything from the repo root.

```bash
# Backtest the model-prediction strategy (main strategy)
python -m backtests.model_predictions.run_backtest --start 2025-01-02 --end 2025-12-31 --all-stocks

# Inspect selected signals without running the engine
python -m backtests.model_predictions.run_backtest --print-signals --all-stocks
# Load data / build only, no engine run (fast smoke test of DB wiring)
python -m backtests.model_predictions.run_backtest --load-only --all-stocks
# Persist results to MySQL + write CSV reports
python -m backtests.model_predictions.run_backtest --all-stocks --write-results --report-dir output/run1

# MACD smoke backtest (secondary / example)
python -m backtests.macd_smoke.run_backtest

# EMA-cross backtest (standalone script, not a package module)
python backtests/qmt_ema_cross_clickhouse.py

# Live trading against the QMT venue (the same strategy as the backtest)
python lives/live_qmt_model_predictions.py --account-id <id> --all-stocks
# Validate live wiring without connecting (build the node, then exit)
python lives/live_qmt_model_predictions.py --build-only --all-stocks

# Liquidate all sellable positions (operational tool)
python lives/sell_all_sellable.py --account-id <id>
```

There is no lint/format/test tooling configured. Do not invent a `pytest`/`make`/`ruff` invocation — none exists here.

## Architecture

The central design principle: **one strategy class runs unchanged in both backtest and live.** All database access, venue-symbol conversion, and result persistence are kept *outside* the strategy so the same trading logic is wired into a `BacktestEngine` and a live `TradingNode`.

### The strategy (`strategies/model_predictions.py`)

`ModelPredictionsStrategy` (a Nautilus `Strategy`) is the heart of the system (~1300 lines). It is a pure, deterministic consumer of pre-loaded reference data passed in via `ModelPredictionsStrategyConfig` — signals, trading dates, listed dates, ST/suspended calendars, last closes. It never touches ClickHouse/MySQL/QMT itself.

Key behaviors to understand before editing:

- **Bar-driven daily loop.** `on_bar` detects the first bar of a new trading date and calls `_process_trading_day`, which runs the full pipeline: seed positions from the portfolio → prepare exits → prepare entries → trim to `max_positions` → set equal-weight targets → submit orders. This runs once per date regardless of how many instruments report bars.
- **Trades on the *previous* day's predictions.** `_resolve_signal_date` prefers the prior trading day's signals but falls back to the most recent signal date ≤ the prior day (never forward), so live trading still fires when the predictions table lags. This fallback is deliberately shared with `subscription_signal_date` in the live runner.
- **Restart-safe.** `_seed_active_positions_from_portfolio` rebuilds `_active_positions` from the Nautilus cache (including names that dropped out of today's universe, so the book keeps rotating). The resubmit reconciler is intentionally inert across restarts — it only cancels stale orders and lets the next bar rebuild state.
- **Cash-aware order submission (China A-share / QMT specifics).** Sells are submitted before buys to free cash. Buys are gated on `balance_free()` (not total equity) with a `cash_buffer_percent` haircut; buys that don't fit are parked in `_deferred_buys` and drained by a resubmit timer as sells fill. Rejected orders (QMT "废单" / error 260200 可用资金不足) are terminal and un-cancellable — tracked in `_rejected_order_ids` / `_insufficient_funds` and backed off until a fill frees cash. See `_is_insufficient_funds` and `_submit_buys_within_cash`.
- The strategy records `signal_events` / `target_events` / `order_events` in memory for the backtest runner to persist; it does not write them anywhere itself.

### Backtest path (`backtests/`)

- `backtests/model_predictions/run_backtest.py` is the orchestrator: parse args → load prediction bundle + bars from ClickHouse → build a `BacktestEngine` with the QMT venue → run → reconstruct daily portfolio → report/persist. This is the template to copy for a new strategy backtest.
- **`reconstruct_daily_portfolio` computes the equity curve independently** by replaying fills against close prices, rather than trusting the engine's account report. New reporting logic usually belongs here or in the shared processor.
- `backtests/base.py` (`BaseBacktest`) + `backtests/reporting.py` (`BacktestReportProcessor`) hold shared, strategy-agnostic helpers: symbol conversion (`qmt_symbol`/`data_symbol`), env parsing, ClickHouse connection building, benchmark/tearsheet wiring, and generic report formatting. Strategy-specific portfolio reconstruction stays in each runner.
- `backtests/common.py` handles benchmark loading and excess-return enrichment.
- `backtests/data_providers/` — `ClickHouseModelPredictionDataProvider.load()` returns a `PredictionDataBundle` (the single object carrying signals, universe, calendars, listed dates). `ClickHouseBarDataProvider` loads and prepares bars into Nautilus `Bar` objects. `model_base.py` defines the request/bundle/signal dataclasses and the provider ABC.
- `backtests/result_writers/` — `MySQLResultWriter` persists experiments, signals, targets, orders, trades, and daily metrics. Record shapes live in `records.py`; the `ResultWriter`/`NullResultWriter` interface is in `writer.py`. Only active with `--write-results`.

### Live path (`lives/`)

- `lives/live_qmt_model_predictions.py` wires the *same* `ModelPredictionsStrategyConfig` into a live `TradingNode` with QMT data/exec clients. `LiveModelPredictionsStrategy` subclasses the strategy to add a periodic **reference-data refresh timer** (`refresh_reference_data`) that reloads signals/universe hourly without restarting — so the running strategy picks up new predictions.
- Symbol conversion goes through `nautilus_trader.adapters.qmt.common` (`qmt_symbol_to_instrument_id`). Symbols map between ClickHouse form (`stock:000001.SZ`), QMT form (`000001.SZ`), and Nautilus instrument IDs (`.QMT` venue).
- `lives/sell_all_sellable.py` is a standalone operational tool to flatten positions; it is one of the few places that legitimately reaches closer to QMT plumbing.

### Symbol formats (easy to get wrong)

- ClickHouse bar symbol: `stock:000001.SZ` (via `data_symbol`)
- QMT symbol: `000001.SZ` / `.SH` / `.BJ` (via `qmt_symbol`, which also normalizes `.XSHE`/`.XSHG`/`SZ000001` forms)
- Nautilus instrument id: `<symbol>.QMT` venue

## Data model notes

- The instrument universe is China A-shares. Filters in the strategy and providers cover ST/*ST/delisting name prefixes, suspensions, new-listing minimum days (`min_listed_days`), and optional BJ-exchange exclusion.
- Weights are equal-weight capped at `max_position_percent`; position count capped at `max_positions`; holdings rotate on a `holding_days` rebalance cadence with per-position `stop_loss` and optional trailing take-profit.
