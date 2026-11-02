# FinIntel Index Intelligence: common-session multi-index comparisons (PR #47)

This increment extends merged PR #46 without modifying its single-index or pairwise flows.

## Architecture and source safety

- **Read-only** local GET /api/v1/analytics/basket, restricted to 2–4 distinct FRED price indices selected from SP500, DJIA, NASDAQCOM, NDX. VIX remains a volatility *level* and is not mixed into equity/price-index returns.
- All series and quality metadata are read in one SQLite transaction/snapshot. Exact session-date **intersection** is computed before any metrics. No synthetic sessions, provider blending, price updates, migrations, forward fill or market-provider network requests.
- For each series, normalized 100, simple return, drawdown, annualized rolling sample volatility are computed **on the same aligned dates**. Relative strength and rolling correlation against the first selected baseline are computed on the same aligned series. The first return and warm-up metrics are null, not zeros.
- The first shared session is the common normalization baseline. Consecutive common dates can skip market sessions; no claim of strictly one-day consecutive trading returns is made. Dividends are omitted from these price-index levels.
- Every selected source retains its original provider identity, source snapshot SHA-256, stored session count and excluded non-shared observations. A missing series or fewer than two shared sessions is an explicit error, never a manufactured comparison.
- Quality requires a current complete scan with no unresolved findings by default. A user can choose labeled unverified research explicitly; it must not appear as verified.

## Native and UI

- Tauri command **analytics_basket** accepts only allowlisted index codes (no VIX), 2–4 unique indices, real dates, bounded rolling windows and the recognized quality policy. It forwards only fixed request parameters to the local analytics route using existing private desktop authorization.
- The new expandable **Multi-index comparison** section on Index Intelligence allows 2–4 selections, preserves baseline ordering and locally saves configurations. The existing PR #46 pairwise interface stays intact.
- Common data is displayed in a single multi-series SVG and synchronized 20-row table with metric switching for normalized performance, drawdown, rolling volatility, relative strength and correlation. Chart decimation selects existing points for rendering only and never alters calculations.
- The date-envelope selector uses local catalog bounds, not guessed shared dates. Actual common coverage appears only after a successfully validated backend response. In-flight stale responses are discarded on selection changes.
- Source hashes, excluded session counts, quality status and calculation version are visible; English/Simplified Chinese translations are included. No new provider access, user-data exports or automatic backups.

## Automated and manual acceptance

Backend tests use fabricated local SQLite observations with deliberately disjoint holes and prove deterministic results, shared-date-only rolling calculations, unchanged stored data, no VIX/duplicates, quality fail-closed behavior and the typed API.

Frontend tests check native-only IPC versus same-origin browser access, offline errors, response identity and hash validation, date envelopes, chart-to-grid numerical parity and Chinese strings. Existing CI still builds on macOS, Windows and Ubuntu and runs Python and React suites.

Manual packaged-desktop UX, screen-reader layout, 18M-row-scale performance, missing provider entitlements, and real-data coverage remain **not run** until independently tested. Issue #31 should remain open for constituent history and other remaining roadmap tasks. **Do not auto-merge PR #47.**
