# Historical data quality

FinIntel's local Data Validation page includes a durable quality scan. The scan
stores candidate findings in SQLite schema v14 and never changes an observation.
Each finding includes the symbol, provider, session, rule, severity,
confidence, normalized evidence, and a stable fingerprint. The fingerprint
also includes the destination series identity (series ID, frequency, and
adjustment), so separate historical series cannot overwrite one another.
Re-running a scan updates the evidence for the same candidate without
resetting its review state, including after a backup merge remaps series IDs.

The current deterministic rules cover:

- invalid OHLCV values and candle ordering;
- unusually large close changes, reported as candidates rather than confirmed
  corporate actions;
- calendar-aware missing-session candidates; and
- overlapping provider closes that differ by more than one percent.

The same session-gap and level-jump checks apply to persisted FRED index
levels; index findings are labeled separately from equity series.

## Calendar-aware missing sessions

Missing-session rules use `exchange_calendars` rule version
`calendar-missing-session-v1` and the `XNYS` (NYSE) or `XNAS` (Nasdaq) session
schedule. Calendar sessions are date labels, so weekends, Thanksgiving,
Christmas, New Year's Day, Good Friday, DST transitions, and exceptional full
closures are excluded from the expected-session set. FRED's SP500, DJIA,
NASDAQCOM, NDX, and VIX series use their explicit underlying market-index
calendar mapping rather than a generic weekday rule.

Every calendar missing-session finding records the calendar source, rule
version, calendar name/timezone, provider publication delay, as-of date, and
the surrounding stored observations. Findings are classified as:

- `expected_closure` for a documented instrument suspension;
- `pending_publication` for a recent session within the configured provider
  publication grace period;
- `suspected_missing` for an eligible exchange session absent between stored
  observations; or
- `unresolved` when the calendar or eligibility evidence is unavailable.

Listing/delisting dates and suspension intervals are explicit evidence inputs,
keyed by series ID, `(symbol, provider)`, or symbol when calling
`run_quality_scan`. They include a source and, where available, verification
date. Leading and trailing history is not inferred from the first/last local
row without expected bounds. A missing calendar never becomes a confirmed
defect: unresolved findings have informational severity and low confidence.
No original observation is modified, fetched, or deleted by this validation.

Weekends, exchange holidays, DST, listings, suspensions, vendor delays, and
corporate actions require review evidence before a candidate is confirmed.
There are no automatic repairs or provider-preference decisions in this scan.

Quality scans report `findings_examined`, `findings_stored`, and
`total_known_findings`. When the configured `max_findings` cap is reached,
`status` is `partial`, `complete` is false, and `truncated` is true. The
scan-level result is persisted in `data_quality_scan_runs`. The report
endpoint exposes that scan completeness separately from its own pagination
metadata, while its review-state counts cover all persisted findings, not just
the returned page.

The read-only scan uses bounded keyset pages and closes each read connection
before processing the page; it does not hold FinIntel's process-wide write
lock. Only the short schema migration and candidate persistence transactions
use that lock, so daily acquisition, imports, backups, and reviews can commit
between scan pages.

When a scan reaches its finding cap, `data_quality_scan_progress` stores the
current traversal phase (`equity`, `index`, or `cross_provider`) and a source
key for that phase. The next run seeks directly to that key instead of
re-scanning earlier observations. The read stops at the cap after completing
the current source item, so a batch can include all sibling candidates from
that item without losing evidence. Partial `findings_examined` and
`total_known_findings` counts are cumulative lower bounds; they become exact
when the traversal completes, or remain exact when inherited from a completed
scan of the unchanged source revision. A source-only revision maintained by SQLite
triggers is compared at scan start and end; review, finding, and scan-metadata
writes do not invalidate a scan, while source observation changes mark it
`stale`. Cross-provider comparison reads use the series scan index and merge
per-series primary-key streams, avoiding repeated temporary database sorts.

Reports also compare the saved scan-end source revision with the current
source revision. A daily import after a successful scan therefore returns
`stale: true`, `complete: false`, and the same stale state in
`scan_completeness` until a new scan finishes.

Backup merges retain imported scan runs for audit history but invalidate all
scan progress in the merged database. The destination must be scanned again
because it may contain observations that were not part of the imported scan.

Review transitions are recorded in `data_quality_reviews` and the current
state is kept on `data_quality_findings` (`candidate`, `confirmed`, `rejected`,
or `accepted_correction`). An accepted correction is an audit decision only;
the original price remains available and is not silently rewritten.

The local API provides:

- `POST /api/v1/quality/scan` (managed desktop token required);
- `GET /api/v1/quality/findings` with type, symbol, provider, and review-state
  filters;
- `POST /api/v1/quality/findings/{id}/review` (managed desktop token
  required); and
- `GET /api/v1/quality/report` for a redacted local report.

Evidence is normalized local metadata. Credentials, provider tokens, and raw
provider payloads are not stored in quality findings or reports.
