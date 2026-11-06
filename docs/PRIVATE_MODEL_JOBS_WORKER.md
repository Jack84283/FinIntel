# Issue #58 — Isolated local model worker (Phase 2B1)

This increment builds on the non-executing job ledger from PR #57. It is an
**opt-in managed-desktop process**, not a trading system, generalized
Python execution facility, or validated backtesting engine.

## Execution and ownership

- FastAPI lifespan starts one supervisor thread only when a managed desktop
  process token of at least 32 characters is present. That thread owns the
  queue and starts a separate Python child process for each claimed job;
  model computation is never performed on a FastAPI request or event loop.
- A cross-process exclusive worker lock prevents a second owner from
  reconciling or claiming jobs. Exclusive startup marks previously running
  attempts interrupted (or cancelled if cancellation was requested).
  Queued entries remain durable and are not auto-resumed.
- The only executable model is builtin.sma_crossover version 1.0.0. Requests
  have typed source parameters and a mandatory SHA-256 snapshot pin. No
  Python module, command, source filepath, arbitrary provider, executable,
  URL, or dynamic model identifier is accepted for these jobs.
- Child requests use bounded stdin envelopes, a reduced environment without
  provider tokens or process credentials, and a fixed executable selected
  by the backend. Computation uses read-only local data; network socket
  operations are disabled inside the child.
- The model engine checks the expected snapshot before computing. The child
  reads the source again after computation, and the supervisor verifies
  the snapshot before calling the ledger completion transition. The ledger
  validates claim generation, request identity, source and lineage hashes,
  output schema and retention limit. Source changes fail closed.
- All state writes are fenced by the monotonically increasing claim attempt;
  a late worker cannot overwrite a cancelled or retried attempt.

## Limits and cancellation

- Default one child at a time, 32 queued/running jobs maximum, 2,000
  observations per job, 4 MiB maximum retained model output.
- A 60-second wall-clock limit; POSIX processes have 2 GiB virtual-memory
  and 50/55-second CPU limits. Windows children request a 2 GiB Job Object
  process-memory limit with kill-on-close behavior.
- A running cancellation enters cancel_requested; the child checks this
  between computation phases. The supervisor allows two seconds for
  cooperation and then hard-stops the process. Shutdown terminates the
  child and joins the supervisor.
- Running progress is indeterminate (0) until actual completion (100);
  intermediate percentages are never invented.
- Failures retain only allowed safe codes (interrupted, invalid_input,
  quality_blocked, source_changed, storage_unavailable, resource_limit,
  worker_failure). Exception text, raw inputs, credentials, private paths
  and stderr are never persisted as failure diagnostics.

## Managed local API

All routes require the existing desktop process token, including reads;
bad request validation does not echo private or unrecognized input fields.

| Route | Operation | Output |
|---|---|---|
| POST /api/v1/models/jobs | Enqueue source-pinned built-in SMA request | 201 and summary |
| GET /api/v1/models/jobs?limit=10&offset=0 | Newest-first list, 10 by default | total and items |
| GET /api/v1/models/jobs/{uuid} | Read status and attempt metadata | summary |
| GET /api/v1/models/jobs/{uuid}/result | Read completed result with lineage | summary and result |
| POST /api/v1/models/jobs/{uuid}/cancel | Cancel queued or request running cancel | status |
| POST /api/v1/models/jobs/{uuid}/retry | Requeue identical pin, max three attempts | summary |

A derived status reports cancel_requested when state is running and the
cancel flag is set. The native shell uses only dedicated fixed model_job
IPC commands and the managed loopback transport; the typed React client
does not expose browser HTTP access or caller-selected localhost URLs.

## Validation and rollout

Synthetic tests cover source changes before execution and just before
persistence, separate-process completion, price-data preservation, restart
reconciliation, two independent queue owners, cancellation, timeouts,
attempt fencing, managed API validation, native IPC route security,
and typed desktop transport.

Run CI for Ruff/pytest, React lint/typecheck/tests and the macOS, Windows
and Linux native builds. Installed manual desktop testing on all three
platforms remains a separate gate. Do not merge the PR automatically
or close parent issue #32. Phase 2B2 will add bilingual visible job
management; Phase 2C may introduce separate verified backtesting contracts.

**Data preservation:** No automatic provider requests, historical price
changes, destructive migrations, model source imports, secret logging,
auto backups, or overwrite of existing job attempts.
