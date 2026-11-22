# AL-03 · Historical risk analysis

`builtin.risk_profile@1.0.0` reports historical risk for the first pinned FRED price index. Add a second distinct eligible index to compute Pearson correlation. With two sources, all calculations use the shared observed dates, with the evidence reporting selected and excluded session counts for each source. Adjacent observed dates can span missing sessions. The model never inserts sessions or divides a multi-session price change into synthetic daily returns.

Sources are restricted to FRED SP500, DJIA, NASDAQCOM and NDX daily price-index levels. The evaluation cap is 2,000 selected observations per source and 4 MiB for the result. The `risk_window` defaults to 63 returns and accepts 2–1,999. The `tail_window` defaults to 252 returns and accepts 60–1,999. The model needs at least one observed session and emits null rolling statistics until their samples are ready.

## Statistical conventions for version 1

The calculation version is `historical-risk-v1` and the implementation revision is `historical-risk-linear95-decimal-v1`.

- **Returns and volatility.** Simple returns are closing level divided by the preceding observed level minus one. The configurable risk window counts returns. Annualized volatility uses sample standard deviation with divisor N−1 and multiplier √252. Downside deviation is the daily square root of the mean of squared returns below zero, with the entire window as the denominator. A full risk window is required for these statistics.
- **Drawdown.** Divide the current level by its running peak and subtract one. Track the most negative drawdown to date and its peak and trough. The latest equal high is the starting peak for any later episode. The first equal-depth trough is retained. Recovery occurs at the first close at or above the peak. Record the most recent completed episode and its durations in observed intervals. An open episode reports its peak, trough and elapsed intervals, with no future recovery date or completed duration.
- **VaR.** Use exactly the configured number of trailing returns, with a minimum configurable window of 60. Until the full requested window is available, report the observed sample count, set VaR, ES and tail count to null, and report `insufficient_history`. Sort losses as −return, set the zero-based rank to 0.95 × (N−1), and interpolate linearly between the surrounding sorted losses. Report the greater of the quantile and zero.
- **Expected Shortfall.** Average all observed losses greater than or equal to the reported VaR; include all ties. A zero threshold includes zero-loss observations and positive losses and excludes gains. If the floored threshold has no qualifying loss, report VaR zero, null ES, zero tail count and `empty_tail`.
- **Correlation.** Calculate Pearson correlation over pairs of returns sharing both interval endpoints. Require the full risk window and return null if either series has zero variance. The result for a single source has null comparison returns and correlation.
- **Precision and units.** Use 28 significant Decimal digits and round-half-to-even. Serialize finite values as fixed-point decimal strings. Returns, drawdowns, volatility, downside deviation, VaR and ES are fractions, so 0.05 represents 5%. Correlation is a dimensionless ratio. Source levels are positive index points.

The 60-return threshold is adopted for this proposed model revision. The empirical tail can be small and gives a historical price-index-change proxy. These indexes omit dividends, and the result describes historical observations rather than estimating maximum loss or modeling a portfolio.

## AL-05 integration

The v2 registry lists the model as proposed. The internal `evaluate_risk_model` evaluator consumes an AL-01 request with ordered source pins and reads them in one read-only local SQLite snapshot. It validates supported source identities, selected-row hashes and quality evidence before calculation. AL-05 should resolve and pin sources before scheduling, recheck quality and snapshot identity at execution, then bind and persist the versioned result through the existing bounded worker and ledger. This phase adds no managed dispatch, routes, migrations or backtest strategy.
