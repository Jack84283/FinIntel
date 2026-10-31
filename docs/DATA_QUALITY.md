# Historical data quality

FinIntel's local Data Validation page includes a durable quality scan. The scan
stores candidate findings in SQLite schema v8 and never changes an observation.
Each finding includes the symbol, provider, session, rule, severity,
confidence, normalized evidence, and a stable fingerprint. Re-running a scan
updates the evidence for the same candidate without resetting its review state.

The current deterministic rules cover:

- invalid OHLCV values and candle ordering;
- unusually large close changes, reported as candidates rather than confirmed
  corporate actions;
- weekday gaps, explicitly marked as not exchange-calendar confirmed; and
- overlapping provider closes that differ by more than one percent.

The same session-gap and level-jump checks apply to persisted FRED index
levels; index findings are labeled separately from equity series.

Weekends, exchange holidays, DST, listings, suspensions, vendor delays, and
corporate actions require review evidence before a candidate is confirmed.
There are no automatic repairs or provider-preference decisions in this scan.

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
