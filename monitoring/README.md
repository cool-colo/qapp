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
