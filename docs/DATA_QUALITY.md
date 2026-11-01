# Historical data quality

FinIntel's local Data Validation page includes a durable quality scan. The scan
stores candidate findings in SQLite schema v14 and never changes an observation.
Each finding includes the symbol, provider, session, rule, severity,
confidence, normalized evidence, and a stable fingerprint. The fingerprint
also includes the destination series identity (series ID, frequency, and
adjustment), so separate historical series cannot overwrite one another.
Re-running a scan updates the evidence for the same candidate without
resetting its review state, including after a backup merge remaps series IDs.
After a successful, non-stale full scan, active findings that are absent from
the complete candidate set are superseded in place. Their IDs, evidence,
review state, and review history remain queryable; incomplete or stale scans
never perform this reconciliation.

The current deterministic rules cover:

- invalid OHLCV values and candle ordering;
- unusual close changes, reported as candidates rather than confirmed
  corporate actions, with raw/unadjusted series explicitly marked for
  corporate-action review;
- liquid-baseline volume spikes and suspicious zero-volume price moves;
- repeated source payloads, duplicate source observations, and provider
  revision metadata that is stale or arrives out of order;
- a stale latest observation candidate when the most recent retrieval or
  provider revision exceeds the configured age;
- calendar-aware missing-session candidates classified as expected closures,
  pending publication, suspected missing, or unresolved; and
- overlapping provider closes that differ by more than one percent.

Advanced anomaly rules use the documented `anomaly-detection-v1` defaults:
two-times close changes, ten-times volume spikes with a minimum current volume
of 10,000 and a baseline of 100, a ten-percent zero-volume price move with a
baseline of 100, and a 30-day stale age. The scan API accepts a `thresholds`
object for explicit local overrides; the selected thresholds are included in
the scan result, evidence, and checkpoint identity. Low-volume baselines do
not produce volume-spike findings, and all anomaly results remain candidates
for review. Time-dependent results record the validation reference date; a
report becomes stale when that date changes, even when source observations are
unchanged.

The same session-gap and level-jump checks apply to persisted FRED index
levels; index findings are labeled separately from equity series.

Weekends, exchange holidays, DST, listings, suspensions, vendor delays, and
corporate actions require review evidence before a candidate is confirmed.
Exchange calendars are selected from the symbol catalog for equities and from
the explicit FRED index mapping for index series. Calendar metadata changes
invalidate a completed scan so finding classifications are recomputed.
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
key for that phase plus the cumulative candidate fingerprints seen so far. The
next run seeks directly to that key instead of
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

Legacy fingerprint collisions retain each duplicate finding and its original
review events. The canonical finding receives an explicit reconciliation event
that records the superseded finding ID and state, so conflicting review
provenance is not silently moved or flattened.

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

## Provider reconciliation (Issue #29 — Task 1)

Historical reconciliation runs as the `cross_provider` phase of the same
durable quality scan, including its bounded source-key cursor, freshness checks,
completed-scan supersession, and preservation of reviewed candidate identities.
No separate mutable "canonical price" is created. A provider-preference ranking
is **advisory**, never a choice to write or display one provider's close as truth.

The v1 eligible sources are Alpaca SIP, Tiingo, and Alpha Vantage **US equity**
daily `raw_unadjusted` observations, compared as USD/share price-series closes.
This contract is deliberately narrow: unknown feeds, weekly bars, alternative
currencies, units, split/dividend-adjusted histories, and price-index versus
total-return-index observations are *not compared*. The current SQLite daily
equity schema has no explicit currency column; the USD/share contract is based
on the scope of these three US equity adapters, not inferred currency conversion.
Only FRED index-level series are currently persisted for most indexes, so no
cross-provider index-level equivalence is claimed. A later migration should
store fully explicit currency/unit/return semantics for broader eligibility.

Each significant overlap disagreement retains the two observed prices, their
series IDs and import/retrieval revisions, comparison rule version, absolute and
relative discrepancy, provider names, source policy, severity and confidence.
The existing `overlapping_close_differs_over_1pct` fingerprint remains for
backwards-compatible review continuity; its actual threshold is recorded in
evidence and may differ when the caller overrides the policy. In-range missing
provider sessions produce `provider_session_missing_within_coverage_v1` findings,
but missing leading/trailing history is not automatically labeled defective.
Revision arrival and stale/repeated provider batches are also reported by the
existing independent anomaly rules, not interpreted as cross-provider defects.

`POST /api/v1/quality/scan` accepts these optional threshold fields inside
`thresholds`: `reconciliation_relative_tolerance` (0–1, default 0.01),
`reconciliation_absolute_tolerance` (nonnegative, default 0), and
`reconciliation_provider_priority` (a permutation of `alpaca_sip`,
`tiingo`, `alpha_vantage`). The complete rule-policy identity participates
in scan checkpoints and resumability. Existing review decisions are retained
under changes to thresholds or source ranking.

Read-only endpoints:
- `GET /api/v1/quality/reconciliation/policy` exposes documented defaults and
  eligibility, with no upstream requests.
- `GET /api/v1/quality/reconciliation/summary?code=AAPL` reports existing
  reconciliation findings and per-provider stored series counts, or omit
  `code` for a database-wide summary.
- Existing `GET /api/v1/quality/findings?finding_type=provider_divergence`
  provides paginated, source-backed evidence and review states.

These APIs never correct values, merge series, make provider requests, or export
raw feed payloads. Corrections and corporate-action confirmation remain separate
Task 2 work. This phase also does not substitute for live-provider tests on a
disposable database or final whole-database performance acceptance.
