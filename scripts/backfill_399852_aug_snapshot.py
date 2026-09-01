"""One-off backfill: copy 399852.SZ August-2026 EOD from ClickHouse
dwd_index_eod_price into MySQL live_stock_tick_snapshot (after_trading).

Mapping follows the existing live reference row for 399852.SZ:
  last_price   <- close
  open/high/low<- open/high/low
  last_close   <- pre_close
  volume       <- vol           (手)
  pvolume      <- vol * 100      (股)
  amount       <- amount * 1000  (CH 千元 -> yuan)
  market_status='CLOSE', snapshot_type='after_trading',
  instrument_id='399852.SZ.BIGQMT', schema_version=1
"""
import datetime as dt
import urllib.parse
import urllib.request

import pymysql

TS_CODE = "399852.SZ"
STOCK_CODE = "399852.SZ"
INSTRUMENT_ID = "399852.SZ.BIGQMT"
START = "2026-08-01"
END = "2026-08-31"

CH_URL = "http://127.0.0.1:8123/?database=default"
MYSQL = dict(
    host="pc-2zey98jewo9dn8aa4.rwlb.rds.aliyuncs.com",
    port=3306,
    user="quant_user_test",
    password="3Uo*A^%I-oXec",
    database="quant_routine_test",
)


def ch_query(sql: str) -> list[list[str]]:
    req = urllib.request.Request(CH_URL, data=sql.encode("utf-8"))
    with urllib.request.urlopen(req) as r:
        text = r.read().decode("utf-8")
    return [line.split("\t") for line in text.splitlines() if line]


sql = (
    "SELECT toString(trade_date), close, open, high, low, pre_close, vol, amount "
    f"FROM dwd_index_eod_price WHERE ts_code='{TS_CODE}' "
    f"AND trade_date >= '{START}' AND trade_date <= '{END}' "
    "ORDER BY trade_date FORMAT TSV"
)
rows = ch_query(sql)
print(f"Fetched {len(rows)} ClickHouse rows for {TS_CODE} {START}..{END}")

now = dt.datetime.now()


def f(x):
    return None if x in ("", "\\N") else float(x)


records = []
for trade_date, close, open_, high, low, pre_close, vol, amount in rows:
    write_time = dt.datetime.strptime(trade_date, "%Y-%m-%d").replace(hour=15, minute=0)
    vol_f = f(vol)
    records.append(
        (
            trade_date,           # trade_date
            write_time,           # write_time
            "after_trading",      # snapshot_type
            INSTRUMENT_ID,        # instrument_id
            STOCK_CODE,           # stock_code
            "CLOSE",              # market_status
            f(close),             # last_price
            f(open_),             # open
            f(high),              # high
            f(low),               # low
            f(pre_close),         # last_close
            f(amount) * 1000 if f(amount) is not None else None,  # amount yuan
            int(vol_f) if vol_f is not None else None,            # volume 手
            int(vol_f) * 100 if vol_f is not None else None,      # pvolume 股
            None,                 # open_int (no source)
            None,                 # last_settlement_price
            now,                  # created_at
            1,                    # schema_version
        )
    )

conn = pymysql.connect(**MYSQL)
try:
    with conn.cursor() as cur:
        # guard against duplicate backfills
        cur.execute(
            "SELECT count(*) FROM live_stock_tick_snapshot "
            "WHERE stock_code=%s AND snapshot_type='after_trading' "
            "AND trade_date BETWEEN %s AND %s",
            (STOCK_CODE, START, END),
        )
        existing = cur.fetchone()[0]
        if existing:
            raise SystemExit(
                f"Aborting: {existing} August after_trading rows already exist for {STOCK_CODE}."
            )
        cur.executemany(
            "INSERT INTO live_stock_tick_snapshot "
            "(trade_date, write_time, snapshot_type, instrument_id, stock_code, "
            " market_status, last_price, `open`, high, low, last_close, amount, "
            " volume, pvolume, open_int, last_settlement_price, created_at, schema_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            records,
        )
    conn.commit()
    print(f"Inserted {len(records)} rows into live_stock_tick_snapshot.")
finally:
    conn.close()
