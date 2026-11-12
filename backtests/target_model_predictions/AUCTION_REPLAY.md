# Opening auction execution and tick replay

Opening auction execution is opt-in in `configs/strategy.yaml`. It uses the same
Nautilus strategy, target quantities, and order reconciliation as continuous trading.
The auction is the day's first trading operation, so starting Nautilus position
quantities are sellable; continuous execution retains the existing T+1 rules.
No launcher or running account is changed automatically.

## Live configuration

Set `auction_enabled: true` in the account's strategy YAML. Preparation begins at
09:24:01, orders are submitted from 09:24:50 until 09:24:59, and no order is canceled
before 09:30. Subsequent submissions and replacements obey both
`execution_windows` and `exchange_execution_windows`. Cancellations independently
obey `cancel_windows` and `exchange_cancel_windows`, which default to
`09:30-11:30,13:00-14:57`. Cancel windows include their start and exclude their end,
so 14:56:59 can cancel and 14:57:00 cannot. Automatic cancel checks continue after
`stop_time`, while replacement orders still require a submission window.
Local and exchange timestamps must be on the same day and in the same continuous
session; stale books still cannot trigger automatic cancellation. Resting exchange orders can
still fill outside these windows.

Preparation subscribes candidate and held instruments to Nautilus depth. A positive,
equal best bid/ask during the opening auction is interpreted as the indicative
matching price; unequal, missing, stale, future, or prior-day levels are rejected.
Confirm this feed convention and timely delivery for each venue before enabling
the feature. Provisioned broker reconciliation must complete before execution.
The existing 09:27 full-tick prefetch remains available for the actual open; periodic
full-tick refresh also runs during auction preparation.

The optimizer receives the indicative price through its existing `price` field.
The resulting fixed daily quantities are persisted before execution, with reference
timestamps and prediction ranks in target `extra` JSON. The 09:27 snapshot reuses
this plan. A restart restores it and reconciles active orders before submitting.
An existing target without auction provenance is used only for normal execution.
Missing mandatory holding references, optimizer failure, persistence failure, or a
missed cutoff skips auction participation. Suspended holdings retain their existing
previous-close valuation and cannot trade.

Live price limits continue to come from the Nautilus instrument. Daily historical
price limits are used only by the tick-backtest subclass, independently of whether
opening auction execution is enabled. Missing historical limits do not overwrite
the live instrument's limits.

Auction allocation has its own `AuctionOrderSplitter`; continuous execution keeps
the existing `NotionalOrderSplitter` and `order_slice_notional`. Buys use Nautilus
available account cash with the configured `cash_buffer_percent`, bounded by target
deficits and ordered by original prediction rank, then instrument code. The budget
is available cash, not 5% of total account assets. A fixed budget and local commitments
reserve this strategy's submitted orders even before balance updates arrive.
Existing in-flight buys postpone auction allocation. Estimated sell proceeds never
fund auction buys.

Both sides reserve the greater of `auction_minimum_fee` (default CNY 5) and
`auction_commission_rate * notional` (default 0.000085, 万 0.85), rounded up to cents
for each physical order. Notional uses the submitted limit price and legal quantities.
Targets are inspected in rank order:

1. Complete target deficits that fit the remaining budget including commission are
   submitted whole, including small targets below CNY 58,000.
2. Otherwise, find the largest affordable legal partial quantity, accounting for
   every physical order's minimum fee. Submit it only when its notional is strictly
   greater than `auction_partial_min_notional` (default CNY 58,000) **and** the
   remaining target notional is at least that threshold. Do not reduce this quantity
   merely to manufacture a qualifying remainder.
3. Skip disqualified targets and continue inspecting later targets. In particular,
   a remaining budget below CNY 58,000 can fund only complete smaller targets.

For example, a CNY 100,000 target with CNY 60,000 cash is skipped because the
affordable partial would leave a target below CNY 58,000. A CNY 200,000 target may
qualify for that cash budget. CNY 40,000 cash cannot partially fund a CNY 100,000
target but can fully fund a later CNY 20,000 target including its minimum fee.
Exchange maximum order quantities can still require multiple physical orders,
even for a complete target, with commission reserved separately for each.

Buys are submitted first. With no submitted auction buys, no auction sells are
submitted, even if the plan reduces holdings. The gross limit-price notional of
actual submitted buy quantities supplies the sell budget. Sell orders and their
commission together must fit this budget. Sell deficits are inspected from the
worst prediction rank upward, using the same whole/partial/remainder rules.
Holdings absent from the full prediction ranking are sold first; the full ranking
is retained even for stocks excluded from the buy candidate list. Auction sellable
quantity comes from starting Nautilus holdings, less this strategy's own pending
sells, without reading broker frozen/sellable reports.

These rules can leave cash unused because of lots and thresholds. The exact
minimum-commission crossover is `5 / 0.000085 = 58,823.53`, slightly above the default
58,000 threshold; set the threshold higher if desired. Small whole targets still
incur minimum commission. Partial exchange fills can create small filled or
remaining amounts regardless of allocation thresholds. The sell cap uses submitted
limit-price amounts, not guaranteed clearing proceeds: buys and sells may fill in
different proportions, and clearing prices can change their actual cash amounts.
Unfilled or unallocated target deficits remain for ordinary convergence and its
existing splitting logic during configured continuous execution windows.

Buy/sell limits use the fresh indicative price plus/minus the configured offsets
(50 bps, or 0.5%, by default),
at least one price tick, clamped to the published daily price limits. Missing limits
skip that instrument. Indicative prices are stored separately from the actual open.
Open-dependent entry filters and stop-loss decisions explicitly use the planning
reference. Actual-open changes do not re-optimize the frozen daily target; auction
buys are subject to T+1 and cannot necessarily be corrected the same day.

## Existing ClickHouse RiceQuant ticks

Use `--tick-table ricequant_stock_tick` instead of `--tick-file`. The loader uses
`order_book_id` (`.XSHE` / `.XSHG`), `datetime`, `a1/b1`, `a1_v/b1_v`, `prev_close`,
`open`, and cumulative `volume`. The current inspected table contains SH/SZ data.
Queries are limited to selected signal candidates and requested daily partitions.
Selecting BJ symbols fails explicitly because this source has no verified BJ
mapping; choose a SH/SZ universe or supply a CSV with those instruments.

Before 09:25 equal `a1/b1` supplies the indicative price. The single 09:25 result's
`open` and `volume` supply the final uniform auction price and volume; later
cumulative volume is never substituted. During continuous trading, displayed
one-level prices and sizes supply finite simulated liquidity. The five-level data
is ordinary tick data, not a Level-2 order feed. One-sided/invalid continuous
quotes are excluded conservatively.
Orders execute against newly received continuous observations only; between
ticks, the simulator does not assume old displayed liquidity is still available.
Continuous matching requires both exchange and receive time in the same continuous
session: 09:30–11:30 or 13:00–14:57, with 14:57 excluded. RiceQuant observations at
14:57–15:00 are `closing_indicative` and cannot fill resting orders. This table's
cumulative daily volume cannot supply verified final closing-auction volume, so
tick-table replay does not infer closing fills. An explicit CSV `closing_auction`
result can supply them separately at the final uniform closing price, capped by
the configured auction participation rate. It uses only orders accepted before
15:00. Resting limit prices and residual order quantities still apply.
An order accepted at or after the 09:25 clearing time cannot join that auction,
even when its historical result arrives with an assumed data delay.

`_ingest_time` is database capture/import time and is deliberately ignored for
historical receive timing. `--tick-data-latency-ms` (default 0) adds an explicit
assumed receive delay to exchange `datetime`; test this assumption together with
submission/cancel delays. The inspected 2026-10-14 `000001.XSHE` sample had 4,821
usable records, including a 09:25 open of 11.57 and 361,200 auction shares.
Auction indicative observations in that sample were nine seconds apart, so fresh
quotes do not necessarily exist at every second. Submission can occur within
the configured 09:24:50–09:24:59 window once fresh references arrive.

## Historical data contract

Use an exported historical CSV with exactly these columns (column order is free):

```csv
symbol,kind,ts_event,ts_init,bid_price,ask_price,bid_size,ask_size,previous_close,open_price
000001.SZ,indicative,2026-07-08T09:24:01+08:00,2026-07-08T09:24:01.050+08:00,10,10,10000,10000,10,0
000001.SZ,indicative,2026-07-08T09:24:50+08:00,2026-07-08T09:24:50.050+08:00,10,10,10000,10000,10,0
000001.SZ,auction,2026-07-08T09:25:00+08:00,2026-07-08T09:25:00.050+08:00,10.01,10.01,100000,100000,10,10.01
000001.SZ,quote,2026-07-08T09:30:00+08:00,2026-07-08T09:30:00.050+08:00,10.01,10.02,1000,1000,10,10.01
```

- `symbol` is the normalized exchange stock code, e.g. `000001.SZ`.
- Both timestamps include a timezone. `ts_event` is exchange time; `ts_init` is
  historical receive time. Replay runs in receive-time order and rejects duplicates
  and future event times. Samples must cover preparation, submission, and normal
  execution windows with sufficient density for configured freshness checks.
- Prices are CNY and sizes are **shares**, not QMT hands. Convert hands explicitly
  when exporting. `previous_close` is the prior session's close.
- `indicative` rows have equal best prices, zero `open_price`, and event time before
  09:25. They convey information and cannot fill orders.
- Exactly one `auction` observation per instrument/day gives the **final** uniform
  auction price and **final auction成交量**, with both sizes equal. Do not substitute
  indicative matched volume, daily volume, or accumulated intraday volume.
- `quote` rows occur during continuous sessions and include the known actual open.
  Bid/ask sizes are displayed liquidity; replay cannot reconstruct queue priority.
- `closing_indicative` rows cover 14:57–15:00. Their prices and displayed sizes are
  informational and cannot trigger simulated fills. `open_price` remains today's open.
- An optional `closing_auction` row at 15:00 supplies equal bid/ask final closing
  prices and equal sizes containing **final closing-auction shares**, with today's
  open in `open_price`. Exactly one result per instrument/day is permitted for each
  auction phase. Multiple final results fail even if their timestamps differ by
  milliseconds. Repeated depth/quote delivery of one validated observation does
  not create additional liquidity. Fractional result timestamps never extend the
  fixed 09:25 or 15:00 venue acceptance cutoff.

The existing DWD full-tick snapshot table retains one final row per symbol/day and
cannot provide this stream. ODS can supply observations only if it actually retained
sufficient history. Import genuine final auction results and continuous quotes from
a suitable historical source. A date lacking final auction volume produces no
auction fills, and reports explicitly mark the missing coverage.

## Running and interpreting a replay

```bash
python -m backtests.target_model_predictions.run_backtest \
  --config configs/auction_backtest.yaml --all-stocks \
  --start 2026-10-14 --end 2026-10-14 \
  --tick-table ricequant_stock_tick --compare-auction \
  --auction-participation-rate 0.01 \
  --tick-data-latency-ms 100 --submission-latency-ms 100 --cancel-latency-ms 100 \
  --report-dir output/auction_replay
```

Create the backtest YAML from the strategy template, set `auction_enabled: true`
and `risk_manager_mode: backtest`, supply the matching `alpha_model_id`, and retain the desired continuous execution
windows. Existing ClickHouse signal/reference loading and daily closing valuation
are reused; daily bars never enter tick-mode matching. Auction execution with daily
synthetic matching is rejected.
`--load-only` also validates and loads the requested tick source, without running
the engine. `--compare-auction` requires an enabled auction configuration.

No historical L2 subscription or individual order stream is required. Ordinary
ticks with best bid/ask prices and displayed sizes suffice. Those level-one quotes
are converted into a one-level book; `L2_MBP` is only the Nautilus engine setting
used to enforce finite book liquidity.

The simulated venue uses this reconstructed one-level book with a custom fill model: no pre-09:25
fills, one uniform auction price, shared per-side auction-volume participation caps,
and bounded quote liquidity afterward. This deliberately prevents the default L1
engine from assuming unlimited liquidity after a partial fill. Brokerage defaults
to the YAML's `auction_commission_rate` (0.0085%) and `auction_minimum_fee` (CNY 5
per physical order), with 0.05% sell stamp duty. CLI brokerage overrides apply to
both the allocation reserve and the replay charges, including continuous fills.
Sell stamp duty is charged from actual fills and does not consume the allocation's
sell quota. Transfer fees and historical fee-regime changes require adjusted fee
assumptions; the model does not automatically infer them.

Reports include per-order `auction_execution.csv`, actual fill events for daily
cash/position reconstruction, and (with `--compare-auction`)
`auction_comparison.csv` plus the continuous-only equity curve. The comparison
runs the same data and continuous windows with auction execution disabled; its
target plan is generated when the actual open becomes available. It therefore
measures the complete change in planning and execution timing.
The tick-backtest subclass waits until actual opens have arrived for every eligible
signal candidate and nonsuspended holding, even if delivery times differ. Timer
and refresh triggers obey this same readiness check. Known excluded candidates do
not block readiness; a required open missing for the whole day leaves planning
deferred with an explicit warning, instead of freezing an incomplete baseline.

Repeat the comparison with participation rates such as `0.005`, `0.01`, `0.02` and
with different submission/cancel delays, using separate report directories. These
are assumption sensitivity checks, not measured queue-fill probabilities. Snapshot
replay cannot reconstruct exact order priority or the effect of your orders on the
auction clearing price.
