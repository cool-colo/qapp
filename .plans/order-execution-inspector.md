# Order Execution Inspector

## Goal

Add a read-only dashboard view that explains an order against RiceQuant minute/tick
market data, displays every order lifecycle event for a stock, and compares the
broker-authoritative order/trade records with the locally observed Nautilus stream.

The strategy remains database-free. Persistence is infrastructure owned by
`SnapshotRecorder` and `LiveSnapshotWriter`; visualization is owned by `web/`.

## Source authority

The inspector never silently merges records from different sources:

| Source | Meaning |
| --- | --- |
| `live_order` | Broker-authoritative order snapshot |
| `live_trade` | Broker-authoritative fills |
| `live_order_realtime` | Latest locally observed Nautilus order state |
| `live_trade_realtime` | Locally observed Nautilus fills |
| `live_order_event` | Append-only local lifecycle event audit |
| `ricequant_stock_minute` | Independent minute OHLC/volume context |
| `ricequant_stock_tick` | Independent tick last price and five-level book context |

Broker records drive headline price, quantity, filled quantity, and final-status
diagnostics. Local events provide the lifecycle interval and observation latency.
Disagreements are returned as explicit diagnostics, never resolved implicitly.

## Asynchronous event persistence

The Nautilus message-bus callback must not perform MySQL I/O.

1. On callback entry, materialize immutable event/order/trade records and capture the
   local receive timestamp.
2. Submit one bundle to a FIFO asynchronous persistence worker and return.
3. A single worker processes bundles in submission order. This prevents an older
   state from committing after a newer state for the same order.
4. In one MySQL transaction, insert `live_order_event`, update
   `live_order_realtime`, and, for a fill, insert `live_trade_realtime`.
5. Retry failures with bounded exponential backoff. The event UUID is the idempotency
   key, so retrying a committed bundle is safe.
6. Graceful actor shutdown drains the queue. Queue depth, oldest queued age, last
   error, and failed-attempt count are inspectable for health reporting.

The queue is intentionally in process: order-event volume is small, submission is
non-blocking, and the existing SQLAlchemy pool remains the connection owner. A hard
process crash can lose records that have not committed; broker snapshots remain the
authoritative recovery source, but cannot reconstruct every intermediate local event.

## Event timestamp contract

`live_order_event` records all clocks without substituting one for another:

- `broker_event_time` / `broker_event_ns`: Nautilus `ts_event`, when positive.
- `broker_time_kind`: `order_time`, `trade_time`, `event_time`, or `unavailable`.
- `local_event_time` / `local_event_ns`: Nautilus `ts_init`, when positive.
- `local_received_time`: recorder clock captured before enqueue.
- `persisted_at`: worker commit-attempt time.

QMT currently uses broker `order_time_ms` as `ts_event` for several order status
events. Such rows are labeled `order_time`; the UI must not claim that timestamp is
the broker's precise cancellation/rejection transition time. Fill events with
`traded_time_ms` are labeled `trade_time`.

## `live_order_event` schema

The append-only table contains:

- Account/trader/trading-date identity.
- Nautilus event UUID, event type, latest local status, reconciliation flag.
- Client/venue order IDs, instrument, stock code, side, type and limit price.
- Quantity, filled quantity and average fill price at observation time.
- Optional fill trade ID, last fill price and last fill quantity.
- The broker/local/receive/persist timestamps above.
- Signal/target lineage, reason, book snapshot and raw broker information.

The unique key is `(account_id, trader_id, event_id)`. Events are queried by the
stock/date index and ordered by local receive time, local event nanoseconds, then ID.

## Dashboard API

`GET /api/order-execution` accepts:

- `source`, `account`, `trader`
- `stock_code`, `trade_date`
- optional `start`, `end` ISO timestamps
- optional `tick_limit` (bounded server-side)

When both timestamps are omitted, the backend derives a bounded window beginning five
minutes before the earliest order/lifecycle/strategy-decision timestamp and ending at
least thirty minutes later or five minutes after the latest relevant timestamp. With
no records it uses the full session.

It returns source-separated data:

```json
{
  "broker": {"orders": [], "trades": []},
  "local": {"orders": [], "trades": [], "events": []},
  "market": {"minutes": [], "ticks": []},
  "diagnostics": [],
  "coverage": {}
}
```

ClickHouse queries always filter exact `trading_date`, `order_book_id`, and timestamp
range. QMT symbols are translated explicitly: `.SZ -> .XSHE`, `.SH -> .XSHG`;
unsupported exchanges fail rather than guessing a RiceQuant identifier.

Diagnostics include broker/local discrepancies, fill ratio, lifecycle latency,
order price versus best bid/ask, first marketable time, marketable tick count, last
price crossing count, and active-window market range. A marketable/crossing tick is
evidence of price opportunity, not proof that an order should have filled because
queue priority is unavailable.

## Dashboard UI

Add an `委托执行` page with stock, date, optional time range, and a checkbox
multi-select named `图层`. Available layers:

- Minute candles
- Tick last price
- Best bid/ask
- Five-level order book
- Broker orders and trades
- Local order state and fills
- Local lifecycle events

Defaults show minute candles, tick last, best bid/ask, broker orders/trades, and local
events. Broker lines/markers are solid; local equivalents are dashed/hollow. The page
contains synchronized price and lifecycle charts, diagnostic cards, and chronological
broker/local tables. Every tooltip identifies its source and shows both event clocks.

## Verification

- Writer tests prove event insertion + summary/trade upserts share one transaction and
  are idempotent.
- Worker tests prove callback-side submission is non-blocking, FIFO ordering, retry,
  metrics, and graceful drain.
- Recorder tests use real construction paths and verify both timestamp clocks are
  captured before enqueue.
- API tests mock MySQL/ClickHouse boundaries and cover source separation, symbol
  conversion, range validation, buy/sell marketability, missing timestamps, and data
  caps.
- Run focused live-writer/recorder/web tests, then the repository suite while
  reporting its known pre-existing failures separately.
