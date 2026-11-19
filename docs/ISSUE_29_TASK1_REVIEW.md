# Issue #29 — Task 1 acceptance review, November 19, 2026

Baseline: `main` at `70ed5cc57aedebe41ce67b67c6bf9baa8b048691`, including merged
PR #66. **Decision: keep #29 open.** This review verifies disposable synthetic
behavior and improves evidence collection. No production database, real provider,
or installed desktop was used. No acceptance gate has been waived.

## Findings and changes

- `scripts/quality_acceptance.py`: require a detached copy, reject known active
  development/installed-app paths, hard links, symlinks and SQLite sidecars; use
  immutable read-only/query-only SQLite, close the connection, detect file
  replacement/change, resolve output collisions, and create private report files.
- `scripts/issue29_private_evidence.py`: apply installed-path/hard-link protection,
  enforce direct-call query bounds, detect file replacement, identify v14/current/
  unsupported version states, and return failure for failed integrity/query checks
  or unavailable requested digests while retaining diagnostic evidence. Detect
  source rows omitted from the series-based digest because of broken references.
- `backend/tests/test_quality_final_acceptance.py`: regress detached-copy safety,
  private output permissions, path aliases, unknown future schema and an abrupt
  subprocess exit between finding persistence and checkpoint persistence. Restart
  in a fresh process and compare stable IDs/reviews, raw rows and a full scan.
- `backend/tests/test_issue29_private_evidence.py`: regress nonzero failure exits,
  v14/future-version reporting, digest omissions, query bounds and schema-version
  drift against the backend constant.
- `backend/tests/test_corrections.py`: round-trip original observations, provider
  provenance, finding/review records, corporate-action evidence and reversed
  correction/audit history through both merge and replace on disposable stores.
- Acceptance documentation now starts with an owner-prepared detached baseline,
  gives executable local commands, and separates process inspection measurements
  from full-scan and installed-desktop acceptance. The blank sign-off template
  remains intentionally unexecuted.

No production schema, application UI, localization catalog, model ledger, stored
market observations or acquisition behavior changes. There is no new migration.
Schema compatibility is scoped: the quality harness checks required columns;
the standard-library private collector reports version compatibility only and
explicitly labels a matching version's full structure as unverified. Current
supported schema is v15; v14 requires migration of a separate disposable copy.

## Executed automated evidence

Local tests used Python 3.11 and cached development dependencies on macOS/arm64;
repository CI independently uses Python 3.12. Cached dependency versions and local
builds are not a substitute for the PR's actual CI conclusions.

| Check | Result | Evidence/scope |
| --- | --- | --- |
| Full backend pytest | pass | 485 passed, 1 platform-specific skip; final full-suite run after all code changes |
| Ruff check and format | pass | Backend and the four CI-listed utility scripts |
| Frontend TypeScript, ESLint and build | pass | Existing application, no frontend changes; existing bundle-size advisory remains |
| Frontend Vitest | pass | 35 files, 162 tests |
| Synthetic acceptance, 128 sessions | pass | 384 fictional source rows, 7 checks; scan elapsed 0.993 seconds |
| Synthetic acceptance, 2,000 sessions | pass | 6,000 fictional source rows, 7 checks; scan elapsed 1.056 seconds; observational, no large-data threshold claim |
| Local native compile and fixed-command tests | pass | Existing development configuration (`externalBin: []`); cargo check, 4 model-job tests and 1 Unicode-overlap test |
| Local release-config native check | blocked | Packaged Python sidecar absent in isolated source snapshot; not an installed-build test |
| PR CI, including all three native packaging platforms | not_run | Record actual workflow/head results from the PR; this static report does not claim CI passed |

Synthetic reports retain `synthetic_pass_production_unverified` and local
inspection `not_run`. Generated JSON and disposable databases remain outside the
change. Source-price digests do not cover every provenance or audit record.
Exact durations are observations on this run, not service-level guarantees.

## Requirement-to-evidence matrix

`pass` below applies only to its named automated scope. It does not satisfy the
separate private/manual gate. `blocked` means a required input/environment was
unavailable; `not_run` means no execution evidence exists. `fail` requires defect
disposition. `waived` requires an identified owner, rationale, date and approval;
none is recorded here.

| Task 1 requirement | Automated scope/status | Mandatory additional evidence/status |
| --- | --- | --- |
| Independent synthetic/private results | pass — acceptance report regression tests | blocked — owner-prepared real detached baseline unavailable to this review |
| Schema v14/v15 and unknown future version | pass — migration rollback/retry and structural/version inspection regressions | blocked — inspect and migrate real legacy copies, compare complete integrity/provenance |
| Read-only detached inspection | pass — active paths/aliases/sidecars refused, immutable/query-only connection, raw fixture bytes unchanged | blocked — owner confirms custom baseline is detached and unused |
| Original OHLCV/index observations | pass — scans, corrections/reversal and restore regressions | blocked — before/after real source digests and counts |
| Provenance, finding IDs/reviews, corporate evidence and correction audit | pass — existing tests plus merge/replace round-trip regressions | blocked — real legacy/current record identities and audit comparison |
| Full scan and bounded resumed scan | pass — full/resumed comparisons, checkpoint/volume-window tests | blocked — complete full/resumed private-scale runs with fixed reference date |
| Abrupt interruption and process restart | pass — new fault-injected subprocess exit after finding commit, fresh-process resume | not_run — installed desktop interruption/relaunch during a real long scan |
| Query/scan performance and bounded reads | pass — bounded query tests, 6,048-row streaming fixture and two synthetic measurements | blocked — real-size latency, elapsed time, CPU, RAM and disk evidence with hardware/build |
| Backup, merge/replace, rollback and restart | pass — disposable legacy/current recovery, migration rollback/retry, audit tests | blocked — detached real-world backups in each recovery scenario |
| Bilingual/offline/authentication/accessibility | pass — existing backend/frontend automated contracts | not_run — installed macOS/Windows/Linux builds and manual evidence |
| Authorized provider/corporate-action semantics | pass — mocked provider/reconciliation contracts only | not_run — entitled live-provider checks using disposable storage |
| Final acceptance matrix and sign-off | pass — this scoped matrix and executable procedure prepared | not_run — owner-completed private matrix/sign-off; no waived gates |


## P2 follow-up — fail closed on incomplete mandatory query evidence

After the initial review, the collector's CLI exit check was tightened to
require `status=measured` for every mandatory bounded query. A skipped
(`not_run`), failed, missing-status or unknown-status query now yields exit 1,
while retaining the private JSON diagnostics. Optional checks still remain
opt-in; unrequested `not_run` checks do not cause failure.

New disposable-fixture regressions cover matching-v15 missing columns, failed
and unexpected mandatory query statuses, report preservation, unchanged
SQLite bytes and successful collection with unrequested optional checks.
No production database was read. The automated test counts above describe the
original PR head; verify updated test counts and CI against the follow-up head
before merge. This change does not satisfy Issue #29 real-world acceptance.


## Reproduction and disposition

Follow [the private runbook](ISSUE_29_PRIVATE_ACCEPTANCE.md),
[the main acceptance checklist](ISSUE_29_ACCEPTANCE.md), and
[the unexecuted sign-off template](ISSUE_29_SIGNOFF_TEMPLATE.md). Do not run tests,
scans, migrations, corrections or restores on the active production database.
Retain private outputs locally and review any summary before publication.

Independent self-review found and resolved active-path access, report-sidecar
aliasing, integrity failures returning success, and orphan source rows silently
omitted from digest coverage. Regression tests exercise these failure paths.
No remaining code blocker identified in this focused diff; require current PR CI
before recommending merge. Merging this PR does not authorize closing #29.

Next proposed task: Task 2, Model Jobs mounted interaction tests/stabilization,
after review of this PR and explicit direction to proceed. Backtesting, Ollama,
issue closure and unrelated UI changes are outside this PR.
