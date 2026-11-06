# Issue #58 — Isolated local model worker and managed job API (Phase 2B1)

This phase connects the Phase 2A source-pinned SQLite model-job ledger to an explicitly
queued, **local-only** research calculation. It does **not** implement a trading
backtest, a cloud service, a proprietary-code loader, or a desktop job dashboard.

## Execution and safety

- The managed FastAPI sidecar creates a **single supervisor thread**, which
  runs the actual built-in `builtin.sma_crossover@1.0.0` indicator in a separate
  Python subprocess. The API thread performs only bounded read-only source
  inspection and records the queued request. It does not execute the indicator.
- The worker reads its exact queued ID and attempt generation from the isolated
  model-jobs SQLite database and refuses stale, cancelled, or unpinned claims.
  The source revision is checked before evaluation, by the evaluator, and again
  before committing the result. All result hashes, dates, semantics and lineage
  are verified by the existing strict ledger.
- No dynamic Python path, custom model ID, uploaded code, provider acquisition,
  network AI call, browser-supplied host or arbitrary command is accepted.
  Child environment variables are allowlisted to exclude process tokens and
  provider/API secrets; outbound Python socket methods are disabled in the worker.
  Logs do not emit source observations or exception arguments.
- Only the owning supervisor can recover interrupted jobs. It holds a
  cross-process OS file lock for startup reconciliation, queue polling and
  child lifetime. A second sidecar cannot claim/recover the same jobs.
- Exactly one model subprocess is launched at a time. The existing 2,000-row
  bound, 32 active-job limit, three-attempt limit and 4 MiB retained-result cap
  remain enforced. A worker receives a 90-second wall-clock limit; cancellation
  and shutdown terminate the child, then use a bounded hard kill fallback.
- Linux restricts child virtual address space to 2 GiB with RLIMIT_AS.
  Windows assigns the process to an OS Job Object with a 2 GiB committed-memory
  ceiling and kill-on-job-close, failing closed if assignment is unavailable.
  macOS polls the child's resident footprint with /bin/ps once per second and
  terminates it above 2 GiB or if monitoring fails. macOS polling is **not a
  strict kernel-enforced limit**; short-lived spikes between polls are possible.
  The 90-second timeout and 2,000-observation cap apply to all platforms.
- No jobs execute until explicitly queued by an authenticated managed desktop.
  Startup creates neither the model-jobs SQLite database nor a supervisor lock
  file until a persisted ledger actually exists (important for abrupt Windows
  sidecar shutdown). A second supervisor waits to acquire ownership.
  In-progress jobs are marked interrupted/cancelled after a supervisor restart
  rather than silently resumed. Queued jobs persist. Recoverable ledger failures
  release ownership, back off and retry, including safe reconciliation of any
  previously claimed attempt.

## Managed routes (sidecar-only)

All routes require the private `x-finintel-process-token` header, even on
developer mode. The webview only obtains fixed native Tauri commands and never
the token or an arbitrary URL proxy.

| Method | Route | Operation |
|---|---|---|
| POST | `/api/v1/models/jobs` | Validate the built-in model input, pin current bounded local source, enqueue; HTTP 202 |
| GET | `/api/v1/models/jobs?limit=10&offset=0` | Paginated summaries; 1–100 rows, offset <=10,000; explicit `cancel_requested` presentation status |
| GET | `/api/v1/models/jobs/{uuid}?include_result=true` | Detail and optional local result, with read-time observation/lineage hash verification |
| POST | `/api/v1/models/jobs/{uuid}/cancel` | Cancel queued immediately or request running child termination |
| POST | `/api/v1/models/jobs/{uuid}/retry` | Queue same snapshot-pinned request, with attempt/active-job caps |

The corresponding Tauri commands are `model_jobs_create`,
`model_jobs_list`, `model_jobs_get`, `model_jobs_cancel`, and
`model_jobs_retry`. They validate date/index/window selections and job UUID
before routing to fixed localhost endpoints. Native responses are checked
against deny-unknown-field job summaries, bounded result contracts, states,
source identity and hashes before they reach the webview. The ledger independently
recomputes observation and lineage hashes when a completed result is read,
and responds with a safe storage error if persisted data is corrupted.
Managed model-job
request validation errors are redacted without echoing rejected private input. **No new UI is included**; the
next Phase 2B2 adds bilingual history, filtering and 10-row page controls.

## Regression tests and acceptance

The synthetic suite covers managed token failures, invalid dynamic inputs,
source-pinned queueing, bounded list/filter inputs, cancellation/retry,
actual source-backed completion, revision changes before/during computation,
stale attempt rejection, supervisor ownership, transient ledger-error recovery,
explicit pending-cancel status, read-time detection of corrupted output and lineage,
child timeout and cancellation, secret-free child environment, and safe failure-code
mapping. Each native-platform CI job also starts the packaged one-file sidecar,
seeds a disposable index series, submits a pinned job through HTTP, waits for the
frozen child to execute, and checks actual result hashes. No downloaded data is used.

Full CI and installed-desktop testing are distinct gates. Multi-platform
packaging smoke tests do not prove actual production-memory isolation, data
quality sign-off or user-interface accessibility. Keep parent #32 open.

**Local data invariants:** The existing market-data SQLite schema and original
observations are never modified by the model worker; the model-job ledger
remains a separate local file. There are no automatic backups or provider calls.
