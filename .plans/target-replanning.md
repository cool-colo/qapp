# Target replanning

## Accepted behavior

- A strategy with an accepted target replans when an outstanding buy or sell touches
  either daily price limit. Use the latest full-tick last price; do not wait for an
  order rejection. There is no attempt cap. A continuous identical condition is
  latched; a known opening rearms it. A newly blocked instrument can trigger another
  attempt after the current attempt finishes.
- Triggers received before snapshot capture can join that attempt. Triggers received
  once capture starts are kept separately and rearmed after completion; the next
  check uses the latest targets and prices, so a stock that has opened again does
  not force another attempt.
- Automatic replanning respects trading/target-update pauses and
  `auto_replan_enabled`. Manual replanning ignores the automatic switch and may run
  while the user has paused trading or target updates. Both require execution and
  cancellation windows. They preserve the independent pause settings.
- Manual sell/sell-all target versions suppress automatic replanning until the next
  ordinary model target refresh replaces them. An explicit manual replan may still
  replace those targets; no additional pause or resume control is required.
- `ReplanningCoordinator` reserves a temporary trading gate before scheduling work.
  The gate blocks target updates, submissions, modifications, normal cancellations
  and manual sells. Only the coordinator's cancellations and target acceptance bypass
  it. Initial planning and replanning cannot overlap.
- Cancel strategy-owned active and inflight orders, wait for terminal status, await
  fresh Nautilus execution reconciliation and a quiet callback interval, then capture
  the planning snapshot. A cancellation/reconciliation failure aborts planning.
  Reject a result if execution activity changed during optimization or the execution
  window/date has ended. Restore ordinary execution after completion or failure.
- Cancellation completion also finalizes requests canceled before coroutine entry.
  Cleanup is idempotent and scoped to the request, so a delayed completion cannot
  release another request's gate. For an entered coroutine, cancellation of the
  cross-thread future retains the gate until the coroutine exits. Stopping does not
  schedule trading resumption.
- Infeasible/error/missing-signal outcomes retain an accepted target, including an
  empty target. Do not generate another target or reset its execution state. Only
  when no target has been accepted use the existing `_local_safety_plan`. Disabling
  replanning retains the existing execution/freeze behavior.

## Shared business rules

Initial, automatic and manual planning use the same request construction, candidate
and holding filters, result annotation and local-exit override code. The external
risk-manager service is unchanged; its existing `can_buy`/`can_sell` fields carry
direction restrictions.

- Actual same-day buys prohibit ordinary sells; actual same-day sells prohibit buys,
  including signal stocks that have been fully sold. Scope fills to the strategy.
  Exclude Nautilus reconciliation fills and orders tagged `RECONCILIATION` from
  callback/cache trade history. Claimed position-alignment orders can have no tag,
  so check the fill's reconciliation flag as well. Historical real trades come from
  MySQL; a real new fill on a restored venue order still counts.
- Stops compare the latest planning price, and are merged into final target quantities.
  When a stop requires an exit, calculate the residual as current quantity minus the
  existing sellable quantity. Send a positive residual to the planner locked in both
  directions, exclude that name from candidates and override its final target with
  the residual under `local_exit_authoritative`. Nothing sells before planning.
- Profit above `trailing_take_profit_start`, using the existing `entry_price` cost
  basis, prohibits adding to a holding in every planning path.
- Trailing highs retain the existing historical-close basis and span; intraday prices
  only change the stop comparison. Live intraday adjustment uses the existing
  corporate-action data and previous-session adjustment factor.
- Consecutive-limit filtering still excludes today. Keep `max_open_gap_up`, including
  its existing configuration, validation, open-price comparison and report parameter.
- Limits are evaluated at snapshot capture. An opened stock is no longer restricted
  by its earlier limit state.
  Shared limit detection allows an absolute CNY tolerance of `1e-6`, matching the
  existing entry-filter tolerance, so floating-point representations of the same
  quoted price do not bypass permissions or triggers. Ordinary execution uses that
  same detector while retaining its existing buy-up-limit/sell-down-limit rules.

## State and persistence

Accepted new versions reset the existing frozen-symbol, deferred-buy and
insufficient-funds state, and discard pending replacements from the canceled version.
An unsuccessful attempt does not call target acceptance and performs none of these
resets. Preserve actual fill history, sellable exhaustion, historical trailing state,
auction state, order audit/lineage and user pause settings.

Live infrastructure archives attempts in `live_target_plan`, including inputs,
triggers, source, parent version, status and output. A committed archive and the
replacement `live_target_portfolio` projection share one transaction. Empty targets
are complete committed records. Persist before acceptance; an initial result whose
parent target was superseded cannot replace a newer target.

Restart loads the latest committed version, including empty quantities and auction
metadata. Pending attempts become interrupted; attempt counts and latest attempt
state reload. Strategy-attributed same-day fills reload at startup and before every
planning request from the snapshot/realtime trade tables. Merge individual trade IDs
with callback and cache history, counting each once even after a live fill is saved.
Reconciliation remains in the event audit, but does not create realtime trade summaries.
When reading existing summaries, exclude IDs having only reconciliation fill events;
preserve actual broker snapshot trades and IDs having an actual fill event.
New broker-backfilled trades receive the strategy ID immediately under the current
one-strategy-per-account deployment. Existing target projections and unattributed
trades are migrated once under that deployment contract.
Transient local safety plans are archived as attempts and retain their existing
nonpersistent execution semantics.

Daily and opening-auction planning share the committed-target restore path. Restore
empty versions before consulting projection rows or generating targets. Auction
execution uses an accepted same-day target first, otherwise restores a committed
target or plans a new one. Target provenance and prior daily processing do not
block auction submission; existing execution gates and auction quotas still apply.

## Controls and runtime

The dashboard's 交易管理 tab exposes manual replanning and the automatic switch using
the existing token/PIN protection:

- Node `POST /control/replan`: asynchronous acceptance with request ID; busy returns
  409, outside permitted windows returns 400.
- Node `POST /control/auto_replan`, JSON `{"enabled": false}`: persist the override
  before changing memory. An unset override uses the YAML value.
- Node `GET /control/state`: includes current/last attempt and per-day counts.
- Dashboard equivalents: `/api/control/replan` and `/api/control/auto_replan`, with
  account/trader and the existing PIN, plus `enabled` for the switch.

Live execution orchestrates on the node loop and runs only optimizer I/O in a worker
thread. A synchronous backtest advances cancellation/settlement with its TestClock
instead of blocking event delivery. Both paths reuse the same business planning code.
The daily-bar replay only observes its supplied snapshots; tick replay can exercise
intraday touches and openings.

## Verification

`tests/test_replanning.py` uses real Nautilus strategy/engine construction and covers
direction limits, actual same-day fills, partial stop residuals, existing/empty-target
failure behavior, cancellation races, trading isolation, archival transactions,
restart state and a full tick-replay automatic-replanning event chain. Web controls
use the existing routing and permissions; validate the JavaScript with `node --check`.

Verified on 2026-11-16: all 26 new tests pass; the complete suite has 533 passes and
36 pre-existing failures, with no new failures compared with the recorded baseline
(506 passes / 37 failures). Python compilation, JavaScript syntax and
`git diff --check` pass. Tests ran with pytest plugin autoload disabled and Nautilus'
uvloop policy because the sandbox disallows the default test plugin's socket setup.

Review fixes verified on 2026-11-17: 11 additional regression tests cover manual
liquidation and daily takeover, triggers before/after snapshot capture and opening,
empty/auction/continuous archive restoration, cancellation before/after coroutine
entry, cross-thread cancellation and request-specific cleanup. Before the fixes,
five tests reproduced the four reported defects. The final related suite has 94
passes; the full suite has 544 passes and the same 36 existing failures as the
2026-11-16 run, with no new failing tests. Python compilation and whitespace checks
pass.

Fill-history fixes verified on 2026-11-17: 12 new regression tests cover tagged and
untagged position alignment, real fills on restored orders, persisted history with
an empty cache, per-plan history refresh, cross-source trade-ID deduplication,
late callbacks, trading dates, audit-only reconciliation persistence, broker-trade
strategy attribution and scoped SQL loading. The final full suite has 569 passes
and the same 36 existing failures as the pre-change tick-replay run (557 passes).
Python compilation and whitespace checks pass. SQL behavior is verified against
an in-memory database; no live database or venue was accessed for this change.
