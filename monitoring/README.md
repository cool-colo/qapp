# Metrics endpoint

Nautilus has no native metrics integration, so `lives/monitoring.py` adds an
in-process `PrometheusExporter` actor that runs inside the live `TradingNode`,
reads the live `Portfolio`/`Cache` on a timer, and exposes the data over HTTP in
Prometheus text format. Point any scraper (Prometheus, VictoriaMetrics, a plain
`curl`, etc.) at it — this repo does not ship any Prometheus/Grafana config.

## Enable

```bash
pip install prometheus_client   # optional dep; without it the exporter no-ops with a warning

python lives/live_qmt_model_predictions.py --account-id <id> --all-stocks \
    --metrics-port 9100 --metrics-interval-secs 10
```

- `--metrics-port 0` disables the exporter.
- env equivalents: `MODEL_METRICS_PORT`, `MODEL_METRICS_ADDR`, `MODEL_METRICS_INTERVAL_SECS`, `MODEL_METRICS_ACCOUNT_LABEL`.

## Read

```bash
curl -s localhost:9100/metrics | grep qapp_
```

## Exposed metrics

| Metric | Meaning |
|---|---|
| `qapp_cash_free` | Free (available) cash — the buy-gating balance |
| `qapp_cash_total` | Total cash balance |
| `qapp_equity` | Broker-reported total account assets (`AccountState.info["total_asset"]`) |
| `qapp_net_exposure` | Broker-reported total market value (`AccountState.info["market_value"]`) |
| `qapp_unrealized_pnl` | Total unrealized PnL |
| `qapp_realized_pnl` | Total realized PnL |
| `qapp_open_orders` | Number of open (working) orders |
| `qapp_open_positions` | Number of open positions |
| `qapp_deferred_buys` | Buys deferred waiting for free cash |
| `qapp_insufficient_funds_instruments` | Instruments blocked on 废单 insufficient-funds backoff |
| `qapp_rejected_orders` | Terminal rejected orders tracked |
| `qapp_exporter_up` | 1 if the last collection succeeded, 0 on failure |

All carry an `account` label (from `--metrics-account-label`) so several nodes can share one scraper.

## DingTalk fixed-time events

`lives/live_qmt_target_model_predictions.py` sends non-blocking DingTalk summaries
for daily target processing, model-data refresh, pre-open reconciliation,
full-tick prefetch, and the before/after-trading snapshot timers. High-frequency
convergence, full-tick refresh, and metrics timers are not sent.

Snapshot timer summaries are emitted per database synchronization task instead
of as one phase-level success message. Before trading, the tasks cover
`live_asset_snapshot`, `live_position_snapshot`, and `live_target_portfolio`.
After trading, they cover `live_stock_tick_snapshot`, `live_asset_snapshot`,
`live_position_snapshot`, `live_order`, and `live_trade`. Each message includes
the table name, rows written, task status, trading date, snapshot type, configured
time, and relevant source/skipped/failed counts. A phase-level failure message is
retained for unexpected timer-handler exceptions.

Configure the deployment through an explicit env file:

```dotenv
QAPP_ENV=production
DINGTALK_ACCESS_TOKEN=your_robot_access_token
DINGTALK_SECRET=your_robot_signing_secret
DINGTALK_TIMEOUT_SECS=5
```

```bash
python lives/live_qmt_target_model_predictions.py \
    --env-file /path/to/production.env \
    --environment production
```

`--env-file` defaults to `QAPP_ENV_FILE`, and `--environment` defaults to
`QAPP_ENV` (or `live` when unset). Missing DingTalk credentials disable delivery
without stopping the live node.
