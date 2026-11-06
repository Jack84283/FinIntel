# Issue #32 — Durable local model-job ledger (Phase 2A)

This phase adds an **internal, non-executing** SQLite job-state ledger for the existing
allowlisted `builtin.sma_crossover@1.0.0` indicator. It intentionally **does not**
start a job worker, expose API routes, accept user code, claim simulated progress,
execute backtests, or modify the historical market database. Those capabilities
require separate reviewed increments.

## Isolation and storage

- Job state defaults to `data/finintel-model-jobs.sqlite3`, alongside the
  configured market-data SQLite file (`FININTEL_DB_PATH`). A local override is
  available through `FININTEL_MODEL_JOBS_DB_PATH`; the market-data database
  itself is explicitly refused as a job ledger. Symlink files are refused.
- The ledger is created only on an explicit write (enqueue/worker transition);
  list/get calls do not create it. The new file uses owner-only permissions
  where the OS honors creation modes.
- It uses its own user-versioned schema and never increments or migrates the
  price database's version, rewrites prices, imports provider data, or enables
  backup/scheduling. Back up the separate model-jobs file explicitly if job
  history needs to travel with a database backup.
- Stored requests contain only typed, allowlisted model inputs **and a required
  64-character source snapshot SHA-256 pin** before enqueueing. Unpinned jobs
  are rejected, so retries cannot silently change historical inputs. No custom
  source path, Python code, API token, cloud URL, or arbitrary model identifier.
  Stored results remain device-local and are never written to CI artifacts.

## State transitions

| Operation | From | To | Guarantee |
|---|---|---|---|
| `enqueue` | — | queued | At most 32 queued/running jobs; canonical request SHA-256 |
| `claim_next` | queued | running | One atomic claimant; returns a monotonic attempt generation; at most 3 attempts |
| `set_progress` | running | running | Requires matching attempt generation; monotonic 0–99%; rejected after cancellation |
| `complete` | running | completed | Requires matching attempt generation, strict allowlisted result/lineage schema, verified hashes and source pin |
| `fail` | running | failed | Requires matching attempt generation; allowlisted error code only, never raw exceptions |
| `cancel` | queued | cancelled | Immediate, no execution |
| `cancel` | running | running (cancel requested) | Worker must acknowledge via `finish_cancelled` using its claim generation |
| `retry` | failed/cancelled | queued | Same request and expected source SHA; no retries after attempt 3 |
| `reconcile_interrupted` | running | failed/cancelled | Explicit restart gate; no automatic resumption of old computations |

The ledger records stable job IDs, attempts, status, progress, timestamps,
request hashes, mandatory source pins, completed source/output hashes, and
(for completed jobs) bounded result payloads. Stale worker attempts cannot update
retried jobs even when they retain the same job ID. Results must match the queued request's model, source code,
date range, quality policy and parameters, and an explicitly pinned source hash.
The displayed `persistent_job=false` flag in the existing synchronous indicator
API remains unchanged: **this phase does not pretend synchronous execution is
already a durable background job**.

## Tests and remaining work

Synthetic tests cover enqueue/claim/completion, monotonic progress, cancellation,
interrupted-worker reconciliation, reproducible pinned retries, stale-attempt
fencing, relative-path read-only access, strict result/lineage field allowlisting,
quotas, source/result contract checks, unallowlisted paths and unsafe storage locations.
The tests use disposable temporary SQLite files only.

**Phase 2B:** a separate isolated worker process, safe startup/shutdown and
cooperative cancel/timeout; fixed-route managed-desktop job endpoints; and
frontend job history/progress with English and Simplified Chinese localization.
The worker must never run proprietary modules submitted through API JSON and
must verify source revision before evaluating a queued job. Phase 2C follows
with backtesting contracts and point-in-time coverage tests.
