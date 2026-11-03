# Issue #31 — Dated constituent overlap in Index Intelligence (PR #48)

This increment connects the existing source-backed index-membership snapshots from Issue #28 to the Index Intelligence desktop workspace. It does **not** infer index constituents from current stocks, historical prices, or ETF holdings. There are no database migrations or market-data writes.

## Read-only API

GET /api/v1/market-data/indexes/overlap?left=SP500&right=NDX&as_of=2026-10-20&limit=50&offset=0&q=

- **Two distinct constituent indexes** (SP500, DJIA, NDX or RUT) and an explicit calendar date are required. NASDAQCOM has price history but no supported constituent series; VIX is not a constituent index.
- The database reader opens one existing read-only SQLite snapshot. For each index, it selects its most recent imported source snapshot with snapshot_as_of **on or before** the requested date. A newer snapshot is never projected backward.
- The return includes each index's actual snapshot date, source reference, official/public/proxy classification, validation/completeness/confidence status, expected/source count and observed count.
- The response is **unavailable** with null counts when either snapshot is absent. This never becomes a false zero overlap. A *true zero* is possible only with both actual dated source snapshots.
- For two present snapshots, the response returns the intersection count, union count, exact-decimal Jaccard overlap, filtered total and a page of alphabetically sorted common ticker symbols with names and instrument types. The search is parameterized, literal-wildcard escaped, and bounded.
- **verified_snapshot** requires both sources to be official licensed or verified public, both complete/available, and both snapshot dates **and their underlying source-as-of dates exactly** match the requested date. The snapshot statement does not establish continuous membership for dates between imported snapshots.
- **earlier_snapshot** explicitly warns that older verified source snapshots do not prove continued membership on the requested date. **proxy_only**, **incomplete**, and **unverified** states are indicative and cannot be promoted to verified by a chart or table.
- No API credentials, full private portfolios, sourced raw provider payloads or local database files are returned or exported. The API never initiates provider downloads.

## Desktop

The new expandable **Dated constituent overlap** card appears on Index Intelligence after multi-index comparisons, preserving PR #46/#47 behavior. The first index follows the selected price index only when a supported sourced membership catalog exists. NASDAQCOM and VIX have no supported constituent catalog: the panel explains this and requires **explicit** selection of a membership index rather than silently substituting SP500. The second defaults to NDX. The requested date follows the main analysis end date; a visible date override and reset are provided.

The card shows both the stored snapshot date and the underlying source-as-of evidence date *before* members and treats unavailable and indicative results distinctly. Search and 50-row pagination use the same requested date and source pair. In-flight stale responses never overwrite newer selections. The fixed native Tauri IPC command forwards only supported codes, calendar date and bounded search/pagination to the same local API.

## Acceptance evidence and limitations

- Synthetic backend fixtures verify exact date snapshots, no lookahead, proxy and incomplete labeling, missing vs. zero overlap, search/pagination, unchanged SQLite rows and the FastAPI schema.
- Frontend tests verify source identity, strict status consistency, no lookahead, exact union arithmetic, native IPC vs same-origin browser, offline failures, explicit selection for unsupported price indices, both source dates and Simplified Chinese evidence translations. Python/API and native Rust regression checks enforce an 80-Unicode-character query limit rather than an 80-UTF-8-byte limit.
- Existing CI covers Ruff/pytest, TypeScript/ESLint/Vitest and macOS/Windows/Linux native packages.

**Not established:** Real source completeness or licensed constituents, continuity of membership between dated snapshots, historical trends beyond available snapshots, large production-database latency, installed-device UX or accessibility. Do not use indicative overlap for backtests as if it were authoritative. Those remain separate manual and roadmap acceptance gates. Issue #31 stays open; do not auto-merge PR #48.
