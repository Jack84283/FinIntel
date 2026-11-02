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

Each significant overlap disagreement retains the two observed prices and stable provider/symbol/session/retrieval metadata,
comparison rule version, absolute and
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


## Corporate-action evidence and reversible corrections (Issue #29 — Task 2)

Schema v15 is additive: it creates `corporate_action_evidence`,
`quality_corrections`, and append-only `quality_correction_events`.
Existing equity `daily_prices` and index `index_daily_closes` are not rewritten,
even on approval or reversal. Revisions, proposals, and audit records remain in
local SQLite backups and are imported conservatively on database merge.

**Financial semantics:** Equity corrections are presently restricted to
`1d/raw_unadjusted` close in USD/share. Index corrections use the
`index_series_catalog.series_type` contract and `daily` observations:
`price_index` is different from `volatility_index`; neither is assumed to
be a total-return index. Split-adjusted, dividend-adjusted, other currencies,
other units, and synthetic returns must not be silently reinterpreted as these
series. V1 overlays change the *explicitly resolved close* for one instrument,
source, and session, not OHLCV as a whole and not the regular historical APIs.

**Workflow:** Operators must first confirm a current quality finding using the
existing finding review API. A proposed close is captured with the source value,
retrieval timestamp, return semantics, linked stable finding fingerprint and
rationale. Preview reports delta, staleness and any approved overlay conflict.
Validation checks source and finding. Approval rechecks both and requires a note.
A rejected proposal is not applied. Reversal removes the effective overlay and
appends a permanent audit event; it does not delete the approval. Repeated
transitions to the same state are idempotent. Only one approved overlay is allowed
for an observation key. A newer provider revision automatically makes any
old approved overlay stale in the explicit resolved view; it never masks the
new source value. Review state `accepted_correction` alone does **not** apply an
overlay.

**Managed desktop APIs:**
- `POST /api/v1/quality/corrections`: `{finding_id,provider,proposed_close,rationale}`
- `GET /api/v1/quality/corrections/{id}/preview`: read-only comparison and
  eligibility, including stale-source and collision flags.
- `POST /api/v1/quality/corrections/{id}/{validate|approve|reject|reverse}`:
  explicit state transitions (`{note}` is required for approve/reject/reverse).
- `GET /api/v1/quality/corrections` and `/{id}`: paginated status and
  immutable event history for review.
- `GET /api/v1/quality/corrections/resolve` with
  `instrument_kind,code,provider,adjustment,frequency,session_date`: opt-in
  effective close, original value, and stale-overlay status.
- `POST /api/v1/quality/corporate-actions/verify` with `{symbol}`:
  explicit Tiingo reference fetch, requiring configured provider credentials.
  It persists only redacted split/dividend facts, retrieval/source identity and
  screening checks, never credentials or raw provider payloads.
- `GET /api/v1/quality/corporate-actions?code=AAPL`: distinct persisted
  **sourced** split/dividend evidence and **suspected** price-jump candidates.
  A split screening check is not a proof of a bad price.

Protected write endpoints require the managed desktop process token.
Read-only endpoints do not contact providers. Database merge remaps finding
fingerprints using the existing finding mapping, imports UUID-keyed proposals
and their audit events, and rejects divergent same-proposal histories. A
source revision after merge prevents an old overlay from silently applying.
Manual native desktop testing and live Tiingo verification must be documented
separately; CI synthetic tests do not establish provider entitlement.


## Data Quality investigation workspace (Issue #29 — Task 3)

The desktop **Data & Operations → Data Quality** screen is a separate
investigation workspace; Data Validation remains available. It starts with
**S&P 500 (SP500)** and supports DJIA, NASDAQCOM, NDX, VIX plus stock/ETF
ticker drilldowns. A single instrument is queried at a time to keep source
matching explicit and avoid loading the entire historical database.

The overview uses a bounded server-side SQL aggregate for active findings,
including severity, review state and anomaly type. Scan metadata explicitly
distinguishes completed, stale, partial, resumed and unknown scans. The
**Run / resume quality scan** action runs the existing durable scan without
making any automatic provider request; the UI indicates that completion is
reported when the request resolves. The stored checkpoint phase and cursor
are visible. A failed or incomplete scan is never labeled complete.

Findings are paginated on the backend (20 per desktop page), with scope,
finding type, review state and exact provider filters. Selecting **Inspect**
shows an allowlisted, redacted evidence projection, durable review history
(including superseded finding audit), and an explicit review form. Proposing
a correction requires an already confirmed active finding and a rationale.
The separate correction panel presents the original and proposed close,
approval conflicts, stale source notices and append-only audit transitions.
Approval requires explicit acknowledgment of the preview and a decision
note; reversal restores the original effective-close view. This screen
does **not** write to historical equity/index observations or implicitly
apply overlays to Market charts or model calculations.

A scoped corporate-action section distinguishes locally sourced Tiingo
split/dividend references from suspected price-jump candidates. Verification
is an explicit opt-in desktop action requiring the configured provider key;
there is no hidden network refresh. The same page shows stored provider
series coverage counts when available.

**Local-only exports:** JSON and CSV generate on the user's device from
at most 5,000 filtered findings, using backend pagination. Each row contains
only instrument code, provider, session, rule, severity, confidence,
review state and an **explicit scalar evidence allowlist**. Unknown evidence
fields, nested provider payloads, credentials, URLs and review notes are
excluded. CSV output escapes formulas to reduce spreadsheet injection risk.
The UI warns when the matching set exceeds the bounded export cap. Neither
format uploads any user data.

The Tauri layer adds an allowlisted quality API bridge with fixed loopback
routes, managed process-token forwarding and strict ID/query validation;
the webview cannot select arbitrary URLs or read the token. Browser-only
development can read local responses through the Vite proxy but protected
writes still require managed desktop authorization.

The existing `GET /api/v1/quality/findings/{id}/history` and
`GET /api/v1/quality/overview?code=SP500` endpoints are read-only. The
former returns historic decisions for a finding, including superseded
records; the latter aggregates active scope counts plus scan freshness.
Frontend and backend tests cover both workflows and local redaction.
Native macOS/Windows/Linux and manual desktop behavior must be verified
before merging; the separate Issue #29 Task 4 still owns final end-to-end
acceptance and large-database benchmarks.
