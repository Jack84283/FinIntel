# Issue #32 — Versioned built-in research model foundation (Phase 1)

This is the **first focused foundation** for FinIntel's private model framework.
It does **not** introduce arbitrary Python execution, proprietary model loading,
durable jobs, trading, simulation or return backtesting.

## Registry and execution contract

A static registry at `GET /api/v1/models/registry` exposes one allowlisted
model: `builtin.sma_crossover` version `1.0.0`, implementation revision
`sma-crossover-observed-close-v1`. Registration describes:

- Strict typed `fast_window` (2–100) and `slow_window` (3–252),
  requiring `fast_window < slow_window`.
- FRED daily `price_index` index levels only: SP500, DJIA, NASDAQCOM, NDX.
  VIX volatility levels, corporate-action-adjusted equities, unsupported data
  sources, and historical constituent assumptions are not promoted to valid
  financial returns. Member history is not used.
- At least `slow_window + 1` actual observations and at most 2,000
  observations per evaluation. No silent truncation.
- A `long` / `flat` *research signal* is calculated after each observed
  close where a complete trailing slow SMA exists. A signal cannot be used as
  an order filled at that same close. Nothing forecasts future prices.
- Fixed source and model assumptions: no dividends, adjustment, dynamic code,
  new downloads, cloud execution or correction of stored observations.

A managed desktop calls `POST /api/v1/models/evaluate` only through the
fixed-route native `model_research_evaluate` Tauri command. The backend
requires its per-process token, rejects extra JSON keys, accepts only the
explicit built-in version, and never loads filesystem-specified code. An
ordinary browser without a managed native process cannot trigger evaluation.

The source is read by existing `analytics_store.run_analytics`, which
provides an immutable read-only SQLite snapshot, explicit FRED identity,
source-observation hash (including revision timestamps), no network, and the
existing default strict quality gate. Researchers can explicitly opt in to
`allow_unverified`, but results then carry an unverified warning and remain
ineligible for any verified backtest.

## Reproducibility and lineage

Results capture:

- Model ID, model version and separately versioned implementation revision.
  **Any change to indicator math or signal timing must bump the revision.**
- Parameters; requested and actually observed date limits; provider,
  adjustment, return semantics, frequency, units; analytics calculation
  version; and the original immutable source snapshot SHA-256.
- Input quality policy/status/verification. `expected_snapshot_sha256`
  allows optimistic pinning: a changed source revision fails rather than
  silently recomputing with a different observation set.
- Stable `reproducibility_sha256` over canonical execution lineage and stable
  `output_sha256` over all chronologically ordered signals. The separate
  `evaluated_at_utc` audit timestamp is intentionally excluded from both
  hashes, so an identical source/version/parameter request is reproducible.

The desktop shows an expandable local research panel, parameter selectors,
read-only latest-25 signal grid and source hashes. It uses explicit button
execution only and discards async results after selection changes. English and
Simplified Chinese translations include the timing, quality and backtest
limitations.

## Remaining Issue #32 work

This PR **does not satisfy Issue #32 in full**. Required future increments:

1. Durable, separately isolated model jobs with a local-only job database,
   progress, cancellation, retry, crash/restart reconciliation, quotas and
   deterministic result retention. Do not run private code on the API thread.
2. Safe user-owned proprietary model extension boundary, no public code/secret
   submission or CI artifacts, and explicit permissions/sandbox controls.
3. A distinct backtesting engine with train/test partitions, next-session
   execution timing, rebalance policy, commissions and slippage, corporate
   actions, trading-calendar alignment, and strict survivorship/point-in-time
   membership evidence. No hypothetical equity curve is shown before this
   contract is defined and verified.
4. Results comparison, local exports, real-data scale tests and installed
   macOS/Windows/Linux desktop and accessibility acceptance.

Offline synthetic tests establish deterministic math, no-future leakage,
source-revision pinning, input validation and read-only behavior; they do not
validate current historical provider values or investment performance.

No database migrations, model source uploads, cloud calls, automatic backup
schedules or modifications to downloaded market history are included. Keep
Issue #32 open and do not auto-merge.
