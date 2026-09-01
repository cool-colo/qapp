#!/usr/bin/env python3
"""
Probe the BigQMT full-tick RPC directly for one or more codes.

This is a throwaway inspection tool used to answer a single question before a
feature change: does BigQMT's ``get_full_tick`` return usable data for the
index code ``000985.CSI`` (中证全指) the same way it does for a normal stock?

It connects to the same BigQMT Redis RPC bridge the live node uses
(``BigQmtXtTrader`` / ``BigQmtXtData`` from ``bigqmt_signal_trader``), reads the
connection settings from the ``BIGQMT_*`` environment variables (set by
``start_big_qmt_live_trading_2.sh``), calls ``get_full_tick`` for the requested
codes, and prints the raw tick per code.

Run from the repo root with the BigQMT env exported::

    python -m scripts.probe_bigqmt_full_tick 000985.CSI 000001.SZ
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BIGQMT_SRC = Path(
    os.environ.get(
        "BIGQMT_SRC_PATH",
        "/data/flc/code/quant/xtquant_big_convert/src",
    ),
)
XTQUANT_PATH = Path(
    os.environ.get("XTQUANT_PATH", "/data/flc/code/quant/xtquant"),
)

for path in (PROJECT_ROOT, BIGQMT_SRC, XTQUANT_PATH):
    if path.exists() and str(path) not in sys.path:
        sys.path.insert(0, str(path))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    codes = argv or ["000985.CSI", "000001.SZ"]

    from bigqmt_signal_trader.xtquant_compat import BigQmtXtData
    from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader

    account_id = str(os.environ.get("BIGQMT_ACCOUNT_ID", "") or "").strip()
    redis_config = {
        "host": os.environ.get("BIGQMT_REDIS_HOST", "127.0.0.1"),
        "port": int(os.environ.get("BIGQMT_REDIS_PORT", "6379") or "6379"),
        "db": int(os.environ.get("BIGQMT_REDIS_DB", "5") or "5"),
        "username": os.environ.get("BIGQMT_REDIS_USERNAME", "") or "",
        "password": os.environ.get("BIGQMT_REDIS_PASSWORD", "") or "",
        "transport": os.environ.get("BIGQMT_RPC_TRANSPORT", "redis"),
    }
    timeout = float(os.environ.get("BIGQMT_RPC_TIMEOUT_SECONDS", "30") or "30")

    print(
        f"connecting: account_id={account_id!r} "
        f"host={redis_config['host']} port={redis_config['port']} db={redis_config['db']}",
        file=sys.stderr,
    )
    trader = BigQmtXtTrader(
        account_id=account_id,
        redis_config=redis_config,
        timeout_seconds=timeout,
    )
    xtdata = BigQmtXtData(trader.client)

    print(f"requesting get_full_tick for: {codes}", file=sys.stderr)
    data = xtdata.get_full_tick(codes, timeout_seconds=timeout) or {}

    print(f"\nget_full_tick returned {len(data)} entr(y/ies)")
    for code in codes:
        tick = data.get(code)
        if tick is None:
            # BigQMT may echo the code in a normalized form; try a loose match.
            for key, value in data.items():
                if str(key).strip().upper() == code.strip().upper():
                    tick = value
                    break
        status = "MISSING" if tick is None else "OK"
        print(f"\n[{status}] {code}")
        if tick is not None:
            print(json.dumps(tick, ensure_ascii=False, default=str, indent=2))

    extra = [k for k in data if k not in codes and k.strip().upper() not in {c.strip().upper() for c in codes}]
    if extra:
        print(f"\nother keys returned: {extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
