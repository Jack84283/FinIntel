# PR #13 — Historical coverage and eligibility audit

This is a **read-only, local** audit of stored daily Stock/ETF prices.
It complements the five-check Data Validation screen; its eligibility
policy is **not** applied to the older `xnys_internal_gap_candidates` count.
A market-calendar gap alone is not proof of a provider error.

## Run

From the repository root, with the backend Python environment activated:

```bash
python -m finintel.market_data.coverage_audit \
  --years 10 --through 2026-10-23 \
  --json data/coverage-audit.json

# Optional: supply independently sourced, licensed security listing history:
python -m finintel.market_data.coverage_audit \
  --years 10 --through 2026-10-23 \
  --listing-evidence /private/listing-evidence.csv \
  --json data/coverage-audit.json \
  --manifest-json data/coverage-review-only.json
```

Omit `--through` to audit through the previous UTC day. `--db` selects
another local database. `--years` accepts 1–30 years. Report files must
not point at the SQLite database. This command **never** calls providers,
modifies stored price rows or triggers a backfill.

## Optional listing evidence

CSV format (the **synthetic example is not actual listing evidence**):

```csv
symbol,listed_on,delisted_on,source,verified_on,calendar
DEMO,2024-01-02,,licensed-listing-source-2026-10-23,2026-10-23,XNAS
```

- `listed_on`: earliest documented listing date (ISO 8601).
- `delisted_on`: optional documented last listing date.
- `source`: required source identifier for independently verified evidence.
- `verified_on`: evidence verification date, no later than the audit date.
- `calendar`: optional **sourced** `XNYS` or `XNAS` calendar override,
  useful for starter-catalog rows that have a blank exchange.

Do **not** infer historical listing from the first imported price, a current
active-listing feed or present-day index membership. Invalid dates, unsourced
records and duplicate symbols fail validation. Keep proprietary evidence CSVs
out of this public repository.

## Status interpretation

| Status | Meaning |
| --- | --- |
| `eligibility_unknown` | Exchange known; listing bounds unknown. Only observed-span internal gaps can be counted. Leading/trailing completeness is unknown. |
| `calendar_unresolved` | Cannot safely map stored venue to XNYS/XNAS. |
| `unsupported_instrument` | Not a catalogued daily stock or ETF. |
| `outside_verified_listing_window` | No eligible sessions inside the requested period. |
| `stale_listing_evidence` | Active listing verification predates the report's through date. |
| `needs_review` | Missing calendar-eligible sessions or unexpected off-calendar/out-of-listing observations. |
| `calendar_coverage_present` | All evidenced eligible sessions have observations, not a certificate of price accuracy. |

`eligible_sessions_present_pct` remains `null` without a sourced
listing window and a recognized exchange calendar. A percentage, including
100%, does **not** certify vendor accuracy, corporate-action treatment, or
consistent adjusted/unadjusted prices.

The audit counts off-calendar observations separately. The XNYS/XNAS
calendars describe scheduled market sessions, **not** ticker-specific trading
halts, venue transfers or provider coverage. These are explicitly unresolved
until independent evidence is reviewed.

## Candidate repair manifest for PR #14

`--manifest-json` exports grouped missing-session candidates with symbol,
provider, adjustment, exchange calendar, date range, evidence source and
`safe_to_auto_fetch: false`. Every candidate needs manual verification of
trading halts, venue, listing dates, source entitlements and provider range.
There is no automatic fetching or repair in this PR.

Without historical listing evidence, the audit deliberately generates no
auto-fetch candidates. Gather trustworthy evidence rather than inventing
coverage boundaries.

## Regression checks

```bash
python -m pytest backend/tests/test_coverage_audit.py -q
ruff check backend
ruff format --check backend
```

Tests cover exchange holidays, internal gaps with missing eligibility data,
IPO/delisting boundaries, grouping of review-only ranges, missing exchange
metadata, sourced calendar overrides, stale evidence, suspicious weekend
observations and read-only database behavior. Fixtures use synthetic prices.
