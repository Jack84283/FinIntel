# Issue #31 — Historical sourced index-membership snapshot trends

This increment follows merged PRs #46–#48 and the deterministic local analytics
work. It does **not** reconstruct daily constituents from historical index prices,
ETF holdings, current tickers, or missing dates.

## Contract

`GET /api/v1/market-data/indexes/snapshot-trend?code=SP500&from_as_of=2026-10-01&to_as_of=2026-10-20&limit=25&offset=0`

- Supported constituent indexes: SP500, DJIA, NDX and RUT. NASDAQCOM and VIX
  are price/level series without a sourced membership catalog in FinIntel.
- Returns **only dates actually imported** into `index_memberships`, sorted
  chronologically. The range and 1–100 row page size are bounded.
- The first snapshot in the requested range has **null** additions/removals:
  there is no earlier in-range observation against which to measure changes.
  On later pages, the prior in-range snapshot is read solely as a baseline.
- `added_members` and `removed_members` are real differences of the two
  stored symbol sets. For each compared pair:
  `previous_member_count + added_members - removed_members = member_count`.
  These values are not claims about *when* changes happened between snapshots.
- Every response carries source reference, imported snapshot date, underlying
  source-as-of date, source kind, validation/completeness/availability/confidence
  and expected versus observed member counts.
- `verified_snapshot` requires complete, available official/verified-public
  source evidence with underlying source date **equal to the snapshot date**.
  Proxies, incomplete, unverified and date-mismatched sources have explicit
  lower-confidence labels. Pairs are verified only when **both** snapshots
  satisfy the strong conditions; otherwise changes are marked indicative.
- Unknown history is represented as `total_snapshots: 0` and an empty
  `snapshots` array, **not** zero constituents or zero turnover.
- An existing read-only SQLite connection and one read transaction are used.
  No provider requests, database creation, writes, migrations, automatic backups
  or transformations of historical observations occur.

The desktop panel is expandable within **Index Intelligence**. It follows
the main analysis date range by default, supports a deliberate override,
and requires explicit constituent-index selection when the selected price
index lacks a supported membership catalog. Each snapshot has an isolated
bar and exact table values; **no line interpolation or daily continuity is
claimed**. Controls use the managed Tauri bridge in packaged builds and a
same-origin read-only API in browser development. In-flight responses from
older selections cannot replace current results. English and Simplified
Chinese evidence labels are required.

## Automated and manual gates

Offline backend tests verify source-quality classification, date gaps,
bounded pages, cross-page set differences, no lookahead, unknown/absent data
and no storage mutation. Frontend tests verify typed response evidence,
count arithmetic, no-interpolation flags, native/browser route identity,
offline errors and bilingual UI text. The regular CI pipeline covers Ruff,
pytest, TypeScript, ESLint, Vitest, native Rust checks and packaging on
macOS, Windows and Ubuntu.

**Still not established by synthetic tests:** real source authority,
provider completeness, historical continuity between imported snapshots,
production-database performance, packaged-device keyboard/screen-reader
inspection, or installed desktop operation. Do not close Issue #31 until
those acceptance gates and broader roadmap requirements have been reviewed.
