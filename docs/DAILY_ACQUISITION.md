# Daily acquisition operations

Daily acquisition is local-first and disabled by default. It stores job state,
instrument progress, provider provenance, request usage, and safe error
summaries in SQLite. The request ledger reserves a provider slot before each
HTTP request, applies provider-specific per-minute pacing, and enforces the
configured daily request budget. A zero daily budget means unlimited for that
provider. Reservations are conservative: a process that exits after reserving a
slot does not get to reuse it during the same UTC day.

## Automated validation

The backend test suite uses disposable SQLite databases and synthetic provider
responses. It covers calendar-aware scheduling, idempotent job creation,
restart-safe leases, pause/resume/cancel/retry controls, incomplete results,
provider fallback provenance, request budgets, history filters, and API write
authorization.

Run from the repository root:

```sh
UV_CACHE_DIR=/private/tmp/finintel-uv-cache uv run --project backend pytest -q
UV_CACHE_DIR=/private/tmp/finintel-uv-cache uv run --project backend ruff check backend
npm run typecheck
npm run lint
npm test
npm run build
cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml
```

If the environment cannot reach the package index, install the dependencies
from an approved local mirror before running the backend commands. Tests never
read the user's normal SQLite database or provider credentials.

## Optional real-provider smoke test

Use a disposable `FININTEL_DB_PATH` and a test-only environment or OS keychain
entry. Never paste credentials into logs, issue comments, or committed files.

1. Configure exactly one provider and set a small daily budget in the Daily
   Acquisition settings. Keep automatic acquisition disabled.
2. Run one manual job for a small locally stored symbol set. Verify that the
   job records the selected provider, source provenance, request usage, and a
   completed or explicitly incomplete instrument result.
3. Run the same job again. Verify no duplicate `(series, session_date)` rows,
   unchanged counts are reported, and the source provider series is unchanged.
4. Stop the backend during a job, restart it, and verify the durable job
   resumes from pending/running items without corrupting committed rows.
5. Exercise pause, resume, cancel, and retry-failed. Confirm that quota and
   authentication failures are not retried repeatedly and that recovery actions
   are visible in Action History.

Repeat the provider-specific portion for:

- Alpaca SIP: confirm the account has historical SIP entitlement.
- Tiingo: confirm the token can access the requested ticker and date range.
- Alpha Vantage: use a key with enough quota for the test; the default local
  budget is intentionally conservative.
- FRED: use Data Acquisition → Index History to request SP500, DJIA,
  NASDAQCOM, NDX, and VIX, then verify all five are stored as close-only index
  series with the FRED source URL.

Record pass/fail evidence, provider entitlement, actual first/last session,
and any unavailable manual checks in the PR description. Do not claim a real
provider smoke test was run when credentials or entitlement were unavailable.
