# Index Analytics v1 — Local deterministic calculations

Issue #30 foundation, intended as the calculation layer for Issue #31 Index Intelligence.

**Scope:** Pure deterministic Python calculations over persisted and uniquely identified source observations, plus bounded **read-only** HTTP access. This branch does not fetch, reconcile, rewrite, invent, forward-fill, cache, or automatically correct market prices. No database migration or new private-data export.

## API contract

`GET /api/v1/analytics/series?start=2026-10-01&end=2026-10-31&code=SP500&provider=fred`

- Defaults: index `SP500` / `fred`, `window=20`, `quality_policy=require_complete`, no comparison.
- `kind=index|equity`; indexes must match local FRED catalog and use **daily closing levels in index points**, equities require an exact provider and **daily raw_unadjusted close**. No provider merging, fake OHLCV, adjusted-to-raw conversion or synthetic total-return series.
- Required inclusive start/end dates bound the data; more than 10,000 actual observations per series is an explicit error, not silent truncation.
- With `comparison_code=DJIA`, `comparison_kind=index`, `comparison_provider=fred`, the comparison uses actual intersected session dates. `missing_policy=strict` refuses mismatched sessions; `intersection` drops unmatched observations without filling or peeking ahead.
- `quality_policy=require_complete` requires a **current-date complete quality scan** matching the local source version with no active unresolved findings (including candidate warnings) in selected codes. A stale/partial/missing scan fails closed. `allow_unverified` is explicit opt-in and returns `unverified_data=true` with quality status—not a false green label.
- **VIX** is a volatility-level series by default: it has raw levels and moving average but no investment price-return/drawdown metrics unless `allow_volatility_returns=true`. Pairwise VIX-versus-equity/index return comparisons always refuse. Equity-vs-price-index comparison explicitly labels the omitted cash dividends.
- Typed response includes instrument/provider/return semantics, coverage, source snapshot SHA-256 computed from row dates, values and revisions, calculation version, settings, observations, warm-up nulls and source quality flags. Snapshot hash contains no credentials, URLs or raw payloads. Read-only queries never create the database.

## Mathematical conventions

| Metric | Formula / convention |
| --- | --- |
| Daily simple return | P[t] / P[t-1] - 1; first is null |
| Daily log return | ln(P[t] / P[t-1]); first is null |
| Normalized performance | 100 × P[t] / first observed P |
| Cumulative price return | P[t] / first observed P - 1 |
| Weekly / monthly / annual return | Last observed close in period / previous period's last observed close - 1; incomplete leading period has no prior-period return |
| Drawdown | P[t] / max(P[0..t]) - 1; maximum drawdown is minimum; underwater duration counts only observed sessions, not calendar days |
| Moving average | Arithmetic mean of trailing W closes; null until W prices |
| Momentum | P[t] / P[t-W] - 1; null until W+1 levels |
| RSI | 100 × rolling W gains / (gains + losses), using simple W-difference gains/losses; flat window = 50; **not** Wilder-smoothed RSI |
| Volatility | Sample standard deviation of trailing W daily simple returns (ddof=1) × sqrt(252); null until W+1 levels |
| Pairwise rolling correlation | Sample covariance of W **paired** daily returns divided by sample standard deviations; null for zero variance |
| Pairwise rolling beta | cov(left return, benchmark return) / var(benchmark return); null for zero variance |
| Pairwise tracking error | Sample stddev(left return - benchmark return) × sqrt(252) |
| Relative strength | (P_left[t]/P_left[0]) / (P_right[t]/P_right[0]) using the first common session as baseline |

Use immutable decimal input and decimal simple returns at price boundaries; convert explicitly to binary64 only for logarithms and statistical moments. Results are serialized to stable decimal strings, with at most 15 significant digits for float-based calculations; do not treat them as infinite-precision trading outputs. Rolling statistics use **observed sessions** rather than a made-up exchange calendar. Holidays/non-trading days are omitted, not represented with zero returns. Pairwise consecutive common-date returns may cover a longer-than-one-market-session interval when sources have gaps; this is disclosed rather than labeled exact daily synchronization.

## Verification and boundaries

`backend/tests/test_analytics.py` uses offline synthetic independent numerical reference fixtures for returns, log, drawdowns, moving averages, RSI, rolling covariance/beta/tracking error, missing sessions, VIX semantics, snapshot revision invalidation, quality scan gate and read-only API. Existing CI runs Ruff, full backend pytest, frontend typecheck/Vitest and macOS/Windows/Linux native builds.

**Next scope:** Issue #30 still needs broader financial semantics (adjusted and total-return sources once validated), cross-market calendar alignment and additional scale testing. Issue #31 will render the Index Intelligence UI using these contract and provenance fields. PR #45 is an intentionally focused foundation; do **not** close Issue #30 or #29 solely from green synthetic CI. The manual Issue #29 acceptance gates remain explicitly outstanding after PR #44.


## Recovery episodes (Issue #30 follow-up, calculation v2)

The v1 analytics contract reported only maximum drawdown and the longest observed
underwater run. Version `index-analytics-v2` adds **read-only recovery episodes**
without changing source observations, standard return formulas, provider choice,
or existing response fields.

- A peak is the most recent observed high-water mark. Equal closes at the high
  complete any existing drawdown and can become the next peak.
- An episode begins when an observation falls **strictly below** that peak. Its
  trough is the first observed lowest close within the episode.
- **Recovered:** the first actually observed close at/above the prior peak.
  `peak_to_recovery_observed_sessions` counts observation intervals after the
  peak, **including the recovery observation**, while
  `underwater_observed_sessions` excludes the recovery observation.
  `trough_to_recovery_observed_sessions` counts intervals from the actual
  trough to recovery. These are not calendar days.
- **Ongoing / right-censored:** if the requested end date arrives while prices
  remain below the earlier peak, the recovery date and both recovery durations
  are **null**, not zero and never projected from later sessions.
  `ongoing_drawdown_observed_sessions` reports only known underwater samples
  through the last observed row.
- `longest_completed_recovery_observed_sessions` is the maximum **completed**
  peak-to-recovery duration among episodes within the selected input range.
  It is null when none complete, even if an episode remains underwater.
- Calculations are **relative to the requested range**: the initial observation
  establishes the first peak. History before the selected start cannot be
  inferred. Holidays, gaps, non-trading days and missing vendors are not filled.
- Default VIX volatility-level metrics continue to exclude drawdown/recovery
  unless returns are explicitly enabled; no other asset/return semantics are
  promoted.
- Every episode includes actual peak/trough/recovery dates and Decimal-based
  trough drawdown; the desktop receives typed source-backed values, validates
  observed-session duration arithmetic, and displays completed and ongoing
  durations separately, with English/Simplified Chinese explanations.

Offline regression tests exercise multiple episodes, weekend/date gaps, tied
highs, unresolved tails, no history and VIX gating. They do **not** establish
historical provider accuracy, unrestricted index-level return comparability or
production-scale UI latency. Issue #30 remains open for expanded semantics,
market calendars and scale/manual acceptance.
