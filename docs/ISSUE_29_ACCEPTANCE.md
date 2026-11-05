# FinIntel — Issue #29 Final Acceptance

**Workstream:** Historical Data Quality, Validation and Reconciliation. **Final gate:** PR #44, after merged PRs #38–#43. **Scope:** private, local-first market intelligence; no automatic modification of stored prices.

## Evidence policy

A successful synthetic acceptance report **does not** establish production acceptance. The harness never calls live providers, reads account credentials, or includes portfolios, proprietary models, or raw market-price samples in its report. Its positive result is deliberately named **synthetic_pass_production_unverified**.

The existing CI runs pytest + Ruff, TypeScript + ESLint + Vitest + React build, packaged Tauri builds on macOS/Windows/Ubuntu and sidecar smoke tests. PR #44 also generates and archives a redacted synthetic JSON report as the **finintel-issue29-synthetic-acceptance** artifact. Check actual CI results before recording any test as passed.

## Traceability and acceptance matrix

| Requirement | Automated evidence | Additional acceptance | Current scope |
| --- | --- | --- | --- |
| Raw equity OHLCV and index levels unchanged by scans | backend/tests/test_quality.py; scripts/quality_acceptance.py | Real DB copy inspection | Synthetic CI; real unverified |
| Holiday/DST, pre-IPO, suspensions, false positives | test_calendar_quality.py | Live spot-check | Provider unverified |
| Stable findings, review IDs, supersession, older fingerprints | test_quality.py; test_reconciliation.py | Real legacy DB copy | CI; real unverified |
| Truncated scan resume, partial/stale is not complete | test_quality.py; quality_acceptance.py | Restart long scan | CI; manual unverified |
| Compatible provider return semantics only | test_reconciliation.py; test_corrections.py | Independent authorized provider comparison | Provider unverified |
| Explicit confirmed finding, approval, source immutability, reversal | test_corrections.py; quality_acceptance.py | UI trial on disposable DB | CI; manual unverified |
| Provider revision invalidates stale overlay | test_corrections.py | Provider revision smoke test | CI; live unverified |
| v15 backup merge/replace, audit history, legacy v14 rollback | test_corrections.py; test_database_backup.py | Real old and new backup copies | CI; real unverified |
| Index-first bilingual/accessibility; redacted CSV/JSON | quality-api.test.ts; quality-acceptance.test.ts; data-quality-ui.test.tsx | UI accessibility, light/dark and offline | CI; manual unverified |
| Managed desktop authorization and offline read behavior | test_quality.py; quality-acceptance.test.ts | Native packaged desktop | CI; manual unverified |
| 1M+ row query latency, sustained scan and memory | test_quality.py realistic fixture; optional read-only inspection | Actual large database copy and hardware metrics | Large-load not run |
| Live Tiingo/Alpaca/Alpha Vantage/FRED entitlement | Existing provider contract tests | Authorized live requests on disposable DB | Not run |

**Closing Issue #29:** Require actual evidence for each mandatory gate or a clearly approved exception. Never silently treat *not_run*, *blocked*, *partial*, or *synthetic_pass_production_unverified* as a production pass.

## Synthetic acceptance — safe default

From the repository root with Python 3.12 and the backend development dependencies installed:

    mkdir -p artifacts
    python scripts/quality_acceptance.py --output artifacts/quality-acceptance.json

The script creates temporary, fictional price and index observations, performs a bounded/resumed scan, verifies source immutability, confirms a synthetic finding, approves a single-session overlay, merges a v15 backup, reverses the overlay, and destroys the temporary databases. It never opens the configured production database. Output must be a **new** filename.

For an observational larger disposable fixture (not an 18M-row production performance claim):

    python scripts/quality_acceptance.py --sessions 2000 --output artifacts/quality-acceptance-large.json

The 128-session default has a generous 30-second CI guard; larger synthetic runs record elapsed time without claiming a production threshold.

## Opt-in local metadata inspection — read-only

Prefer a separate backed-up **copy**, not the actively used database:

    python scripts/quality_acceptance.py --inspect-db /path/to/finintel-copy.sqlite3 --output artifacts/quality-inspection.json

The script uses SQLite mode=ro and query_only=ON, checks schema and counts, performs a scoped SP500 finding-query-plan probe, and records elapsed time. It does not migrate, correct, scan, download or export underlying market observations; never logs credentials. It refuses to overwrite an existing report, the inspected DB, or its SQLite journal/WAL/SHM sidecars.

Production performance acceptance requires independent measurements of per-index UI latency, scan/resume completion, CPU, peak memory, and peak disk on the actual private-data size. This metadata probe is *not* a substitute.

For reproducible local-only measurements and an explicit closure checklist, see **docs/ISSUE_29_PRIVATE_ACCEPTANCE.md** and **docs/ISSUE_29_SIGNOFF_TEMPLATE.md**. The private evidence script never migrates or writes to the source DB; it cannot verify active installed desktop state or perform a full scanner benchmark.

## Manual acceptance — explicitly NOT RUN by this PR

1. Packaged **macOS desktop**: cold start existing DB, S&P 500 default, index/equity filters, English/Chinese translations, keyboard and screen-reader behavior, dark/light layout; disconnect network and confirm offline read.
2. Review and correction: choose a synthetic confirmed finding; inspect evidence/audit; preview original versus proposed values; require acknowledgement and notes; approve, reject, reverse; confirm source OHLCV/index series remain unchanged. Verify that unreviewed findings do not automatically alter Market charts or model data.
3. Scanner: interrupt a running scan, restart the desktop, resume without losing finding IDs or review decisions; verify partial/stale badges and coverage.
4. Backups: on disposable copies only, run manual backup, merge, replace and recovery, including legacy v14; compare SHA/digests, sqlite integrity/foreign keys, original series and correction audit events. **Never restore to primary production for testing.**
5. **Windows/Linux desktop**: cold launch native bundles, repeat offline reads, token authorization, focus/responsive layout and correction preview.
6. **Live providers when authorized**: on disposable DB, compare provider source rights, raw-versus-adjusted semantics, corporate actions, and date coverage. An absent API key or entitlement is **blocked/not_run**, not pass.
7. **Large-data local copy**: when available, record database size, daily price row count, source data availability, scoped query latency, full/resumed scan timing, peak memory and exact OS/hardware. Do not invent measurements.

## Decision record

For final review, record: merged baseline SHA; workflow URL and five green job conclusions; pytest/Vitest totals; synthetic artifact; any authorized local-inspection result; tested device and build IDs; source and historical coverage counts; provider entitlements; real performance timings; defects/waivers; and accept/reject decision.

**Status:** synthetic implementation PR #44 has merged. Real-data, manual desktop, provider, and sign-off gates remain unverified until completed on the owner's machine. Keep Issue #29 open.
