# AL-09 acceptance report

**Release recommendation: INCOMPLETE_EVIDENCE**

Baseline: main at `dfbc4d0` (#86). The history from #73 through #86 covers the architecture audit, registry, six analysis models, durable ledgers/workers, managed transports, desktop workflows, backtest presentation, and filesystem export policy. AL-09 changes tests, reporting and CI, plus a focused SQLite handle-lifetime fix found by Windows acceptance. The CI evidence records the exact commit tested.

## Automated checks

| Check | Status | Evidence |
| --- | --- | --- |
| Synthetic acceptance | PASS | 170 mandatory Python cases, including the SQLite handle-lifetime regression. The JSON evidence is generated from pytest outcomes. |
| Python lint and formatting | PASS | Ruff lint and formatting checks |
| Frontend | PASS | 249 tests, TypeScript, ESLint and production build |
| Full backend suite | PASS | 738 passed; Windows-only Job Object regression skipped locally |
| macOS native | PASS | 19 Rust tests with loopback access |
| macOS packaged sidecar | PASS | Baseline sidecar startup, token rejection, isolated job and shutdown exercised against a disposable database |
| Initial hosted Windows | FAIL | Backup staging file remained open, blocking the rename. See the compatibility fix below. |

## Final hosted CI evidence

[Bootstrap CI run 45551093483](https://github.com/Jack84283/FinIntel/actions/runs/45551093483) passed all five jobs for implementation commit [`937cd8819d8843c43d14aa737d5af34836cace7f`](https://github.com/Jack84283/FinIntel/commit/937cd8819d8843c43d14aa737d5af34836cace7f).

| Hosted job | Status |
| --- | --- |
| Backend / Python (Ubuntu) | PASS |
| Frontend / React (Ubuntu) | PASS |
| Native / Ubuntu 24.04, including DEB packaging | PASS |
| Native / macOS, including app bundle packaging | PASS |
| Native / Windows, including the backup restore regression and installer packaging | PASS |

The backend job generated and archived the [`finintel-al09-synthetic-acceptance` artifact, ID 14994530166](https://github.com/Jack84283/FinIntel/actions/runs/45551093483/artifacts/14994530166). The JSON records 170 mandatory checks passed with zero failures or skips. The artifact is subject to GitHub Actions retention.

Generate the redacted Python evidence with `python scripts/al09_synthetic_acceptance.py --output artifacts/al09-synthetic-acceptance.json` from a checkout with the backend development dependencies. Any mandatory failure, skip, collection error or missing prerequisite returns nonzero. The JSON includes stable synthetic test IDs, outcomes, commit and environment context, with no database or report content. The hosted frontend and native jobs provide separate evidence for their respective stacks.

## Integration, hashes and preservation

- The managed routes and isolated child process execute all six approved analysis models. Tests parse completed analysis results against their evidence/output digests and check exact dated observations and source order.
- An isolated SMA child produces strict-quality eligible research. A durable isolated backtest consumes its original pins. The completed output matches the Phase 2C1 engine, and the separate shared golden fixture checks the frozen historical report. The test confirms that the boundary signal fills on the next observed session, that a changed source rejects a new submission, that the archived output remains readable, and that a corrupt retained report fails closed.
- Market schema version and a SHA-256 of the complete logical SQLite dump are compared before and after execution. This covers source closes, dates, retrieval timestamps, counts, schema, audit state, and quality records. Temporary, synthetic SQLite files are the only databases used.
- Backup checks restore the market database into an empty disposable destination, simulate failure after a destination write, and verify recovery of the pre-restore state. The completed ledger's logical digest and its reopened result remain unchanged. Existing tests cover corrupt backups and failed schema migration rollback. A market backup excludes the model ledger. Abrupt process termination during restore and standalone ledger recovery remain NOT_RUN.
- Managed authentication, pinned revisions, attempt fencing, no inherited provider keys and resource limits retain regression coverage. The isolated workers completed with outbound networking restricted by the execution environment. The installed app's offline workflow remains NOT_RUN.
- Verified JSON/CSV content, atomic writes and filesystem classifiers have automated coverage. Actual native chooser behavior on live network mounts remains NOT_RUN. Windows and Linux filesystem policy results are platform-specific and tracked in hosted CI.

## Compatibility defect found

On Windows, creating a market backup during the disposable restore test failed with WinError 32. The backup and validation routines in `backend/finintel/storage/backup.py` used SQLite connection context managers, which finish transactions but leave connections open. The focused fix closes the handles before the caller renames or removes the verified staging file. A regression deliberately retains each connection object and verifies that all handles have closed after snapshot and preview. Backup format, schema and restore rules stay the same.

## Scale measurements

On an Apple Silicon host (Darwin 27.2.0, arm64), rounded pytest call times were 1.53 s for four-source Relative Strength over 2,000 shared sessions, 1.19 s for a 2,000-session Market Regime result, and 1.86 s for strict SMA plus durable backtest using a 2,000-session source and 1,980 test sessions. These include synthetic storage setup, queueing, child execution, retrieval and assertions. They are observations from one run with no performance threshold. Worker peak memory, interactive rendering and cancellation latency were not measured separately.

## Remaining owner gates

- Installed macOS Apple Silicon, Windows and Linux walkthroughs, including an external-network-disconnected run, native save dialogs, live mounts, restart and worker cleanup. All are NOT_RUN pending the [desktop checklist](AL-09_DESKTOP_ACCEPTANCE.md).
- Private-history validation and recovery rehearsal using the owner's approved process. No private evidence belongs in hosted CI.
- Verify the remaining private-model extensibility requirements of issue #32 separately. Issues #31 and #32 remain open. The Windows backup handle defect is addressed by the focused fix described above.
