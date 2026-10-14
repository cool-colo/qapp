# Pipe/proxy worktrees

This qapp branch pairs with `../nautilus_trader_pipe` (`quant_pipe_bridge`) and
`../xtquant_big_convert_dev` (`quant_pipe_bridge_dev`, BigQMT client 0.3.61).
The existing qapp, Nautilus and editable quant_v21 installation are unchanged.

Run from this worktree, with your existing Python environment activated:

```bash
./start_bigqmt_pipe.sh --check-runtime
./start_bigqmt_pipe.sh --env-file /path/to/deployment.env --config /path/to/strategy.yaml --account-id YOUR_ACCOUNT
```

The launcher contains no account credentials. Select a dedicated environment's
interpreter with `PIPE_PYTHON_BIN=/path/to/environment/bin/python` if available.
`NAUTILUS_TRADER_PATH` and `BIGQMT_CLIENT_PATH` override the paired source paths.
Without an override, the BigQMT live entrypoint and full-tick snapshot script also
select these paired worktrees before importing their clients. Verify the printed
module paths before starting a deployment.

The launcher selects Redis RPC to the Windows proxy, disables FormulaServer
routing and the Redis full-tick demand cache. Whole-quote push is disabled in the
node configuration; quotes/depth poll at `--poll-interval-secs` (default 1 second),
and execution reconciliation polls at 1 second alongside execution callbacks.
Tune market polling for the symbol count and measured QMT throughput.

The adapter expects the new MiniQMT cancellation result (`0` succeeds, `-1`
fails). Dividend events are extracted from the DataFrame's `time` and `dr`
columns, preserving ex-dividend dates and adjustment coefficients.

Compiled Nautilus extensions are separate copies from the original quant_v21
checkout at the same base commit; Python adapter changes do not require a native
rebuild. Rebuild in this worktree if Cython/Rust code or Python ABI changes.
The copied extensions require Python 3.12 and the same platform.
Source selection isolates package code per process. Using the same interpreter
still shares dependency versions; these launchers do not install or upgrade any
shared package. A dedicated environment is required to isolate dependencies.

The current shared `quant` environment contains redis-py 8.1.0, outside the new
client's declared `redis>=5,<8` range. Before a live launch, create a dedicated
Python 3.12 environment and install the new client's Redis extra there; keep the
shared `quant` installation intact. `PIPE_PYTHON_BIN` selects that interpreter.
The source-path check confirms module selection, not dependency compatibility.

Use separate trader IDs and infrastructure ports for concurrently running nodes,
and give each account one proxy owner. This setup does not start trading or
restart any existing process automatically.

## Validation

- 78 focused Nautilus BigQMT adapter tests passed.
- 15 qapp node configuration, dividend and full-tick snapshot tests passed.
- Runtime paths, shell syntax, both CLI help paths, Ruff checks and git diff
  whitespace checks passed.
- An offline probe delivered changing quotes and depth through the actual client,
  proxy and pipe code with in-memory I/O, including subscribed-only QMT warm-up
  and recovery from a transient RPC error.
- New client requests were decoded by quant_v21 server code in an offline probe,
  including simulated submit/cancel; all requests carried positive
  `timeout_seconds`. An old server lacks `get_request_outcome`, so timeout
  resolution remains limited until the matching bridge/proxy version is deployed.
- `make security-audit` could not run because `cargo-audit` is missing.

No live Windows pipe, QMT server, Redis or database was contacted. The original
worktrees and shared editable package remain unchanged; no process was restarted.
