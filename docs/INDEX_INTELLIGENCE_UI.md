# Index Intelligence workspace — Phase 1 (Issue #31)

PR #46 provides an index-first desktop view using the deterministic local analytics engine from PR #45. It introduces no cloud calls, migrations, provider downloads, raw price changes, or automatic backups.

## Controls and local-only workflow

The main navigation exposes **Index Intelligence** separately from Market, Quality, and Data Acquisition. Primary selection defaults to SP500 and supports DJIA, NASDAQCOM, NDX and VIX. An optional **single** benchmark can be selected for pairwise comparisons; arbitrary 3+ index groups will follow in a later PR.

Select 1Y, 5Y, all stored history, or manual start/end. The Last available control uses the earlier latest date within overlapping catalog coverage (not the computer clock); the exact last common observed session is established only by the analytics engine. Rolling windows use actual observed sessions. Selections are persisted only in localStorage; storage failures are nonblocking.

The API defaults to **require_complete** quality: missing, stale, partial, or active unresolved findings block verified analysis. Explicit **allow_unverified** permits labeled research results only. VIX is a volatility level by default, not an equity return; mixed VIX benchmark comparisons are disallowed. There is no implicit provider fallback, interpolation or invented market observation.

## Native bridge and data integrity

The Tauri command **analytics_series** allowlists index codes, calendar dates, windows and quality policy. Fixed provider **fred** and fixed local API route are used, with the managed Python backend token forwarded by the existing read-only native helper. The Vite/browser branch uses the same-origin local proxy; it does not contact FRED.

The chart and latest 25-row grid share the same parsed API response and exact metric accessor for normalized levels, drawdown, rolling volatility, relative strength, and rolling correlation. Pairwise metrics use common sessions only. A request-generation gate prevents a stale in-flight response from replacing a newer index selection. Long charts decimate existing points **only for SVG rendering**, not backend calculations or the historical grid.

Every result includes provider, observation coverage, quality status, calculation version, and source snapshot SHA-256. The frontend validates source identity, returned dates, window, nonfinite values and comparison semantics before rendering.

## Tests and outstanding work

Frontend tests cover default SP500 selection, bilingual keys, local-only native IPC, browser-proxy errors, mismatched provenance, invalid date/VIX comparisons, last-common dates and exact chart/grid equality. Existing CI includes Vitest, TypeScript, ESLint, Python and macOS/Windows/Linux Tauri builds.

Remaining Issue #31 scope includes multi-benchmark analysis beyond one optional comparison, point-in-time constituent overlap (requires Issue #28), production-scale performance, and installed-desktop manual UX and accessibility verification. Mark those **not run** unless independently verified. Do not close Issue #31 or auto-merge based only on synthetic CI.
