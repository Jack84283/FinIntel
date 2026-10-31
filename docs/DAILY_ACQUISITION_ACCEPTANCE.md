# Daily acquisition production acceptance

This is the repeatable acceptance procedure for issue #26. It supplements the
deterministic unit tests with an explicitly opt-in provider run and installed-
desktop checks. A report is evidence from one run, not a claim that unavailable
provider credentials or platforms passed.

## Safety rules

- Automatic daily acquisition stays disabled. The harness does not enable it.
- Every run uses newly created SQLite databases. Live mode always gets a
  database separate from synthetic checks; the default is a temporary
  directory, and explicit paths must not already exist.
- The harness refuses `data/finintel.sqlite3` and does not read the normal user
  database, backups, or downloaded snapshots.
- Provider keys are read only through the existing credential adapters. They
  are never printed, stored in the report, or included in source URLs.
- Live mode is never run by CI. Synthetic mode is the safe default.

## Disposable harness

Install the backend development dependencies and run the deterministic check:

```sh
python scripts/daily_acquisition_acceptance.py \
  --output /private/tmp/finintel-daily-acquisition-acceptance.json
```

The synthetic run verifies the disabled-by-default setting, validated equity
and close-only index imports, idempotent re-imports, provider/session
uniqueness, SQLite integrity, pause/resume/cancel, retry-failed, and persistence
through independent database connections. It does not contact a provider.

## Opt-in live-provider run

Use a disposable path outside the repository and a small approved fixture. The
path must not exist before the command starts:

```sh
python scripts/daily_acquisition_acceptance.py --mode live \
  --db-path /private/tmp/finintel-live-acceptance.sqlite3 \
  --provider alpaca_sip --provider tiingo --provider alpha_vantage \
  --symbol AAPL --index SP500 \
  --output /private/tmp/finintel-live-acceptance.json
```

Use `--synthetic-db-path` and `--live-db-path` when both databases must be
retained for inspection. `--db-path` is a compatibility shorthand for the
selected mode and is the live database when `--mode live` is used.

Omit providers that are not configured. If a provider has no credentials, an
invalid entitlement, a local budget exhaustion, an upstream quota response, or
a temporary outage, the report records `blocked` and the command exits with
status 2 for manual review. The command never loops after a quota response.

For each successful provider check, inspect the report for:

- provider identity and sanitized source URL;
- actual first and last stored sessions;
- request usage and local budget state;
- first import counts and second-import `inserted: 0` evidence;
- SQLite integrity and duplicate `(provider, session_date)` checks.

FRED has no credential flag in the harness. Request only the approved index
codes needed for the acceptance window, beginning with `SP500`; use the same
`--index` option for `DJIA`, `NASDAQCOM`, `NDX`, or `VIX` when entitlement and
time allow it. FRED results remain close-only index levels and retain the FRED
series URL as provenance.

## Desktop acceptance matrix

Record these checks separately from synthetic CI and live-provider results.

| Platform | Required evidence |
| --- | --- |
| macOS first | Clean packaged app launch, sidecar readiness, process-token rejection, loopback port ownership, graceful shutdown, offline launch, Data Acquisition, Index History, Action History, notifications, loading/error/recovery states, English and Simplified Chinese |
| Windows | Same packaged sidecar and UI smoke checks; record installer and OS credential-vault results |
| Linux | Same packaged sidecar and UI smoke checks; record installer and available secret-service behavior |

CI's native jobs build the sidecar, run its authenticated local smoke test, and
package each platform. Those checks do not replace clean-machine installed-app
acceptance. Until a platform is exercised manually, record it as `not_run`.

## Report interpretation

The JSON report contains `pass`, `fail`, `blocked`, or `not_run` for each check.
Synthetic success is reported as `synthetic_pass` and does not certify
production acceptance. A live report is `PASS` only when every provider and
packaged-desktop matrix entry passes; omitted or unperformed entries keep it at
`REVIEW`, while a defect produces `FAIL`. Keep reports containing live evidence
outside Git unless they have been reviewed for private data and provider terms.
