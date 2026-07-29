#!/bin/bash

set -euo pipefail

source /data/flc/code/quant/qapp/start_calculate_daily_returns.sh

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 START_DATE END_DATE" >&2
  echo "Example: $0 2026-07-01 2026-07-27" >&2
  exit 2
fi

start_date="$1"
end_date="$2"

# Load the same MySQL/conda configuration as the single-day runner, then use
# the authoritative market calendar so weekends and exchange holidays are not
# submitted to the daily-return calculator.
setup_calculate_daily_returns_environment

trade_dates_output="$(python - "$start_date" "$end_date" <<'PY'
from datetime import date
import os
import sys

try:
    start = date.fromisoformat(sys.argv[1])
    end = date.fromisoformat(sys.argv[2])
except ValueError as exc:
    raise SystemExit(f"dates must use YYYY-MM-DD: {exc}") from exc

if start > end:
    raise SystemExit("START_DATE must not be later than END_DATE")

try:
    import pymysql
except ImportError as exc:
    raise SystemExit("pymysql is required to read trade_calendar") from exc

connection = pymysql.connect(
    host=os.environ["MYSQL_HOST"],
    port=int(os.environ["MYSQL_PORT"]),
    user=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASSWORD"],
    database=os.environ["MYSQL_DATABASE"],
    charset="utf8mb4",
)
try:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT `cal_date` FROM `trade_calendar` "
            "WHERE `is_open` = 1 AND `cal_date` BETWEEN %s AND %s "
            "ORDER BY `cal_date`",
            (start.isoformat(), end.isoformat()),
        )
        for (trade_date,) in cursor.fetchall():
            print(trade_date.isoformat())
finally:
    connection.close()
PY
 )"
mapfile -t trade_dates <<< "$trade_dates_output"

if [[ -z "${trade_dates_output}" ]]; then
  echo "No open trading days in trade_calendar between ${start_date} and ${end_date}" >&2
  exit 1
fi

for trade_date in "${trade_dates[@]}"; do
  echo "Calculating daily returns for ${trade_date}"
  calculate_daily_returns "$trade_date"
done
