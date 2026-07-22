# Plan: sync ClickHouse trade calendar → MySQL + reusable DingTalk alert module

## Goal
1. Extract DingTalk alerting from `bin/check_live_status.py` into a reusable module `monitoring/dingtalk_alert.py` that both scripts and live-trading can import.
2. New crontab-driven sync script `scripts/sync_trade_calendar.py`: copy ClickHouse `dwd_trade_calendar` → MySQL table `trade_calendar` (primary-key upsert/overwrite). After sync, check whether MySQL has today's row; send a DingTalk **alert** if missing, and a **normal** DingTalk message on success.
3. `.env` loading precedence: explicit `--env-file` → script-dir `.env` → cwd `.env`.

## New file: `monitoring/dingtalk_alert.py`
Self-contained, stdlib + `requests` only (matches repo style; `python-dotenv` is NOT installed).

Provides:
- `load_env(explicit: str | None = None, script_dir: Path | None = None) -> dict`
  - Precedence: explicit path → `<script_dir>/.env` → `<cwd>/.env`. First existing file wins.
  - Minimal `KEY=VALUE` parser (ignores blanks/`#` comments, strips quotes). Does **not** overwrite already-set `os.environ` unless caller opts in; returns the parsed dict and also injects into `os.environ` (setdefault) so downstream `os.environ.get` keeps working.
- `DingTalkAlerter` dataclass/class built from `access_token` + `secret` (read from env `DINGTALK_ACCESS_TOKEN` / `DINGTALK_SECRET`), reusing the existing signing logic:
  - `_signed_url()` — 加签 mode (copied from check_live_status: hmac-sha256, base64, urlencode).
  - `send_text(title, content, at_all=False, at_mobiles=None) -> bool` — posts `msgtype=text`, returns success bool, logs on failure. No-op (logged) when credentials missing.
  - `from_env(env: Mapping | None = None)` classmethod.
- Keep `DINGTALK_WEBHOOK_URL` constant here.

## Refactor `bin/check_live_status.py`
- Import `DingTalkAlerter` + signing from `monitoring.dingtalk_alert` instead of the local copies.
- Replace local `_dingtalk_signed_url` / `send_dingtalk` bodies with calls into the module (keep `_format_alert_message` local since it's Alert-specific, or pass formatted text into `alerter.send_text`).
- Behavior unchanged; `--access-token`/`--secret` CLI flags still work.
- `bin/` is not a package — it already does `sys.path.insert(PROJECT_ROOT)`, so `from monitoring.dingtalk_alert import ...` resolves. Add `monitoring/__init__.py` so it imports cleanly.

## New file: `monitoring/__init__.py`
Empty — makes `monitoring` an importable package.

## New file: `scripts/sync_trade_calendar.py`
Pattern copied from `scripts/full_tick_snapshot_to_clickhouse.py` (PROJECT_ROOT/NAUTILUS path shim, argparse with env defaults, stdlib ClickHouse HTTP, `main()->int`).

Flow:
1. Load `.env` via `monitoring.dingtalk_alert.load_env(args.env_file, script_dir=Path(__file__).parent)`.
2. Read calendar from ClickHouse over HTTP (reuse `_clickhouse_execute`-style GET/`fetch_json_each_row`). SQL — current (non-superseded) version only:
   ```sql
   SELECT exchange, cal_date, is_open, pretrade_date
   FROM dwd_trade_calendar
   WHERE sys_to = '2299-12-31 00:00:00.000'
     AND cal_date >= {start}
   ORDER BY exchange, cal_date
   ```
   - `--exchange` (default `SSE`, env `TRADE_CALENDAR_EXCHANGE`), optional; empty = all exchanges.
   - `--start` window (default e.g. 2015-01-01 or `TRADE_CALENDAR_START`) to bound the copy; full-history by default is fine since it's small.
3. Ensure MySQL table exists (`CREATE TABLE IF NOT EXISTS trade_calendar`):
   ```sql
   CREATE TABLE IF NOT EXISTS trade_calendar (
     exchange      VARCHAR(16)  NOT NULL,
     cal_date      DATE         NOT NULL,
     is_open       TINYINT      NOT NULL,
     pretrade_date DATE         NULL,
     synced_at     DATETIME     NOT NULL,   -- write time (added per request)
     PRIMARY KEY (exchange, cal_date)
   )
   ```
4. Upsert by primary key via `INSERT ... ON DUPLICATE KEY UPDATE` (executemany, batched), setting `synced_at = NOW()` (Shanghai wall-clock passed in). This is the "按主键覆盖" requirement.
5. MySQL connection from env (`MYSQL_HOST/PORT/USER/PASSWORD/DATABASE`), same names as `backtests/model_predictions/run_backtest.py`, via `pymysql` (installed, v1.4.6).
6. **Post-sync check**: `SELECT COUNT(*) FROM trade_calendar WHERE cal_date = <today> [AND exchange=...]`.
   - today = `datetime.now(Asia/Shanghai).date()`.
   - If 0 rows → build alert text and `alerter.send_text("[qapp] 交易日历同步告警", ...)`; exit non-zero.
   - If ≥1 → send normal success message (row counts, today present) and exit 0.
   - Alert also fires on any exception (sync failure) — wrap main body, send failure alert, re-raise/exit non-zero.
7. `--dry-run` (fetch + log counts, no MySQL write, no DingTalk), `--no-create-table`, `--log-level` flags mirroring the full-tick script.

## Crontab
Add a documented example in the script docstring (not auto-installed unless asked), e.g. run after the calendar source updates each morning:
```
17 8 * * *  cd /data/flc/code/quant/qapp && python -m scripts.sync_trade_calendar >> logs/sync_trade_calendar.log 2>&1
```
Will confirm with user whether to actually install into crontab.

## Notes / non-goals
- No new third-party deps (no `python-dotenv`); minimal env parser lives in the alert module.
- ClickHouse/MySQL access here is infrastructure plumbing (data sync + ops alerting), not strategy logic — consistent with the existing `full_tick_snapshot` exception. Not routed through Nautilus.
- Success message is always sent per the request ("正常也发一个消息").
