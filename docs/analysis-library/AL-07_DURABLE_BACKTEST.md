# AL-07 · Durable, source-pinned SMA backtests

AL-07 integrates the Phase 2C1 simulator with the existing local model-job ledger, worker supervisor, managed FastAPI transport and Tauri bridge. AL-08 owns desktop configuration, inspection and export.

## Eligibility and assumptions

A backtest references a completed, integrity-verified `builtin.sma_crossover@1.0.0` job with the `sma-crossover-observed-close-v1` implementation. Eligible FRED daily price indexes are SP500, DJIA, NASDAQCOM and NDX. The source job supplies the original verified identity and observed closes. Callers supply identifiers and digests. The API accepts neither research result JSON nor executable content.

Durable jobs require the original SMA request to use `require_complete`, its historical source evidence to be verified, and the current scan to be complete and current with no unresolved findings for the index. This rule excludes both verified-source and unverified-source runs created with the override. The pure Phase 2C1 contract retains its original eligibility and numerical behavior.

Training ends on an observed session adjacent to the first of at least two observed test sessions, with completed SMA warm-up. A post-close signal can affect hypothetical long/flat exposure at the next observed close. The model applies fixed fraction sizing, commission and slippage on changes in modeled exposure, and explicit annual risk-free assumptions. The benchmark is frictionless buy-and-hold of the same price index. [Phase 2C1 assumptions](../BACKTEST_PHASE_2C1.md)

## Durable contracts and persistence

- `backtest-job-request-v1` pins the source job ID, request SHA-256, output SHA-256, lineage SHA-256 and implementation revision. The nested `simulation` is a Phase 2C1 request with the index code, selected-row snapshot SHA-256, training/test dates and execution assumptions. The request pins engine revision `sma-next-observed-close-v1`. Decimal assumptions are fixed-point strings with up to six fractional digits.
- `backtest-job-result-v1` contains the durable request hash, an audit timestamp, and the original `local-backtest-research-v1` report with its deterministic input and output hashes. The ledger retains the complete request alongside the report. The pure report's `persisted: false` flag describes its original engine contract. Its outer envelope and ledger record are durable.
- Canonical request and report hashes cover sorted compact JSON. The report output hash excludes its own field. The audit timestamp does not affect reproducibility. A separate ledger envelope digest covers the timestamp and all other envelope fields.
- Schema v3 adds a request discriminator with the SMA model identity. The ledger transactionally rebuilds the job table for v1 or v2, checks row counts and preserved column values, then replaces the table and recreates the indexes. Reads remain compatible before an upgrade. The market database schema stays unchanged.
- The existing worker, attempt fencing, cancellation, restart recovery and immutable successful results apply. The shared limits are one isolated worker, 32 active jobs, three attempts, a 90-second runtime, 2,000 selected observations and a 4 MiB retained envelope.

## Source revision boundary

Before enqueue and execution, the backend reads the exact SMA job and verifies its stored hashes, then compares its dated closes to a single read-only market SQLite snapshot. The selected-row pin covers the original SMA window, including history outside the test interval. The digest covers source identity, observed dates, closes and each selected row's retrieval timestamp. An excluded date outside the original SMA window does not affect the pin. Current quality is checked independently.

Before completion, the worker reserves a market SQLite writer slot using `BEGIN IMMEDIATE` and enables `query_only`. With the reservation held it verifies the original job, source rows and quality again, then commits the result to the separate ledger. Lock contention fails closed. Retry uses the same pins and checks the observed attempt generation. Archived results remain readable from the ledger after source updates without a live market read.

## Managed transport

The five fixed operations use `/api/v1/models/backtests/jobs` and `/{job_id}`, `/{job_id}/cancel`, and `/{job_id}/retry`. Creation returns 202. Listing defaults to ten items with status filtering before pagination. Details can request the verified result through `include_result=true`.

The managed desktop process token is required for every route. The `backtest_jobs_*` Tauri commands accept fixed payloads and use bounded, authenticated loopback requests. The TypeScript client validates response structure, chronology and request/report hashes independently and uses native IPC exclusively.

Stable error codes include `managed_required`, `invalid_input`, `source_changed`, `quality_blocked`, `resource_limit`, `job_not_found`, `job_conflict`, `queue_full` and `storage_unavailable`. The clients can also report `invalid_response` and `backend_unavailable`. No private paths or exception text are exposed.

All results represent hypothetical research exposure to price levels. They do not describe actual fills, tradable index instruments or dividend-inclusive returns.
