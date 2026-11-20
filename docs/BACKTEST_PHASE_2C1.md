# FinIntel Phase 2C1 — Pure, source-pinned research backtesting

**Status:** Engine/contract prototype for review. **Not** a trading engine, an
executable financial backtest on a tradable instrument, or production sign-off.
No application endpoint, scheduler, ledger persistence, or desktop UI is added
by this phase.

## Input and source boundary

- Only the allowlisted `builtin.sma_crossover@1.0.0` indicator may be used.
  No private/custom source, arbitrary Python module, trading broker, or cloud AI.
- Input is an existing versioned *verified-quality* research result with SHA-256
  checks on its points and lineage. `expected_snapshot_sha256` must match the
  pinned input exactly.
- Source semantics are restricted to **FRED daily index price levels** for
  SP500, DJIA, NASDAQCOM, and NDX. VIX, adjusted equity prices, total-return
  claims and constituent-derived/survivorship-sensitive inputs are rejected.
  `point_in_time_membership=not_used` means no constituent-selection claims.
- The 2C1 engine is **pure**. It validates the supplied result and hashes, but
  does not independently reopen the live price database to verify a pinned
  snapshot or entitlements. **Phase 2C2 must perform live local revision
  fencing before and after durable execution**, and reject changed snapshots.
- The data-quality contract requires `quality_verified=true`. Synthetic
  fixtures intentionally exercise the deterministic engine only; they are not
  evidence that real FRED history has passed quality sign-off.

## Timing and execution assumptions

- Indicators use **only observed historical closes available through session t**.
- Signals are produced **after close(t)**. The earliest modeled change of
  exposure is at the **next observed session's close(t+1)**. Price movement
  during (t,t+1] is earned under the *previous* exposure; the new exposure
  applies **strictly after** the hypothetical close(t+1) fill.
- An index value is **not directly investable or tradable**. The fill is a
  hypothetical valuation convention, not an execution-capable instrument.
  No market open prices, executable order prices, synthetic sessions, dividends,
  financing costs, borrow, futures rolls or corporate adjustments are invented.
- Sizing is a constant long/flat exposure fraction between 0 and 1. No leverage,
  short sales, position sizing by future returns or broker order support.
- Commission and slippage are explicit basis-point costs on modeled exposure
  turnover, charged at the hypothetical next-session fill.
- The benchmark is frictionless same-index buy-and-hold of price levels. Its
  transaction costs and dividends are omitted **by design**; this is labeled in
  every report and is not a realistic investment benchmark.

## Train/test segregation

- The training endpoint is an actual observed session immediately preceding the
  first test observation. Its signal must have completed SMA warm-up.
- Test start/end are actual sessions, with at least two observed test sessions.
- This initial built-in SMA model has **fixed supplied parameters** and no
  fitted weights or hyperparameter search. Future training/optimization modules
  must not learn from any test dates.
- For deterministic validation, the result's SMA columns and post-close signals
  are independently recomputed from preceding/current closes. Future points
  cannot be used for a historical signal.

## Metrics and reproducibility

- Report: cumulative and annualized return, maximum drawdown (non-positive),
  sample annualized volatility, Sharpe ratio (null when variance is zero),
  exposure turnover, modeled transaction costs, transaction count, frictionless
  benchmark cumulative return and relative performance.
- Annualization assumes **252 observed sessions/year**, including an opening
  cost on the first test session in the daily-return series. The annual risk-free
  rate is explicit (default zero) and converted to a daily geometric rate.
  Metrics are Decimal-derived research calculations, not investment forecasts.
- Each test session retains modeled equity, benchmark, exposure, costs,
  drawdown, and the preceding signal's session date. Transactions retain
  previous/new exposure and hypothetical execution session.
- The canonical input and output hashes are deterministic; there is no
  timestamp or system-clock dependence in this pure function.
- No source data, result files, migrations, credentials, or trades are written.

## Acceptance boundaries and later phases

2C1 automated tests cover wrong SHA-256/lineage, incompatible semantics,
unverified quality, forged SMA signals, invalid windows, same-session lookahead,
future mutation invariance, skipped observed sessions, transaction cost effects,
and determinism. Run pytest and Ruff in PR CI.

**Still pending:** native managed-job integration, durable backtest outputs,
real-source revision fencing, full source provenance, UI/plot/table/export,
installed-desktop accessibility and offline manual QA, production data
performance/acceptance, and separate user sign-off. These belong to 2C2/2C3.
Do not close Issue #32 or represent this as production-ready backtesting.
