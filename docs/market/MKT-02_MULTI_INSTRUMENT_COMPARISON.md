# Market multi-instrument comparison

## Frozen contract

`market-comparison-v1` is a read-only POST to `/api/v1/market-data/comparison`. The body contains `contract_version`, ordered `sources` (`kind`, `code`, `provider`), and inclusive ISO `start` and `end` dates. Use two to four distinct full source identities, an end no later than today UTC, and at most 3,660 elapsed calendar days. Each source query is bounded at 3,661 daily observations and refuses excess instead of truncating. The request body limit is 4 KiB and the transport response limit is 4 MiB.

Price indexes use FRED and allow SP500, DJIA, NASDAQCOM, and NDX. Equities (including ETFs) use explicit Alpha Vantage, Alpaca SIP, or Tiingo identities. Only their stored raw daily series are supported. VIX remains an individual tab.

The result echoes the request. `sources` are in the original order and carry the key, identity, adjustment, return semantics, frequency, units, first and last observations in the requested window, baseline/common-end/latest closes, common-end return, observation counts, and snapshot SHA-256. Each dated point has a `values` array in that same order, with either a null or an object of decimal strings (`close`, `normalized_100`, `cumulative_return_pct`). Counts include requested-window stored observations, nonshared plotted observations, and missing plotted dates. Explicit read-only, offline and no-synthetic-session flags accompany warnings and an evidence fingerprint.

The baseline and headline endpoint are the first and last dates in the intersection. At least two shared dates are required. The plot and table use the union of actual dates from the baseline through the last available date, with null at missing source positions. Stored dates before the baseline contribute to source coverage only. Lines break at nulls. Coverage is relative to the requested bounds and observed union. It does not infer expected exchange holidays or missing sessions for the entire basket.

`market-comparison-decimal-v1` uses Decimal with a 128-digit local context. Source closes must be finite, positive and representable as plain decimal text (48 characters maximum). The backend divides by the same-date source baseline and produces Base 100 and percentage returns rounded to 12 fractional places using half-even rounding, serialized as plain strings without unnecessary fractional zeros. Source closes retain their stored precision. All reads and source fingerprints refer to one SQLite read snapshot. The reproducibility fingerprint hashes canonical sorted JSON containing the calculation revision, request and ordered source hashes. The output SHA-256 separately hashes the canonical aligned points. The snapshot hashes include exact observations and retrieval timestamps. No price levels, units or return semantics are converted across providers.

## Workflow and architecture

Open **Market → Comparison**. Search by ticker or provider, select a local source, and add it. From an individual tab, **Add to comparison** adds that exact source and opens Comparison. At least two sources are needed to calculate a comparison. Use the source chips to remove instruments or clear the comparison. The 1-, 5-, and 10-year presets use today UTC and calendar years, with February 29 clamped to the last valid day. Custom dates share one inclusive window. Changing the display mode or series visibility reuses the same result.

- **Backend:** `market_comparison.py` reads the existing analytics storage reader on one read-only SQLite snapshot, validates source semantics, computes both display modes, and serializes ordered evidence.
- **Native:** `market_comparison.rs` validates the command and forwards only the fixed request to the loopback sidecar with managed process authorization. Redirects and HTTP proxies are disabled. Native validates the echoed request, ordered series membership, row alignment and canonical output checksum before handing off the bounded response.
- **Desktop:** `MarketComparisonPanel.tsx` owns in-flight request identity and local selection controls. `market-comparison-api.ts` verifies scope, chronology, common endpoints, evidence and hashes. `MarketComparisonChart.tsx` uses the existing SVG series primitive and ten-row table. A keyboard slider inspects exact stored values. Chart coordinate conversion is display-only.
- **Preferences:** `finintel.market-comparison.v1` stores ordered source identities, applied dates, mode, and hidden series. The existing workspace preference carries a separate view flag and still retains the last individual tab. No observations, derived results, credentials or paths are persisted.

Headline returns use the common endpoint even if another source has later prices. Coverage and hashes remain available when lines are hidden. The study compares price levels and may exclude dividends. Unadjusted prices can reflect corporate actions. The quality warning uses the existing local scan and does not infer a quality score or fill any gaps. An unavailable source remains selected so it can be inspected or explicitly removed.

## Automated validation and scale

- 18 comparison backend tests cover managed authentication, mixed source alignment, exact endpoint returns, input limits, repeatability and byte-for-byte SQLite preservation.
- 272 frontend tests passed locally, including 15 new transport and mounted interaction cases. TypeScript, ESLint, and the frontend production build passed.
- 21 macOS Rust tests passed locally, including the fixed comparison route and malformed-response checks.
- A synthetic 10-year test on an Apple Silicon macOS host returned 2,609 weekday observations for each of four sources in 0.102 seconds end to end through FastAPI TestClient. The serialized response was 1,022,951 bytes. This single run measures local query, calculation and serialization. It does not establish an interactive rendering threshold.
- Hosted macOS, Windows and Ubuntu packaging and checks are tracked in the PR.

## Manual installed-desktop checklist

Installed-desktop checks remain **NOT_RUN**. Use a disposable profile and local synthetic histories.

1. Switch between stock, ETF and index tabs, then open Comparison and add those sources. Check the original tab periods and pagination on return.
2. Verify the earliest and latest common sessions against the table. Include a source with an earlier start, a missing plotted date, and a later final observation.
3. Toggle display modes and series visibility. Compare exact keyboard-inspector values with the table and confirm that a hidden series remains available in the table.
4. Review provider, price semantics, warnings, and fingerprints. Check a missing source, fewer than two shared sessions, and the four-source limit.
5. Restart the app with networking disabled and verify the workspace restoration. Inspect keyboard focus and narrow layouts in English and Simplified Chinese with the available appearance settings.
