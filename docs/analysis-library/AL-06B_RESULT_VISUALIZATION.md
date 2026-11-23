# AL-06B · Analysis Library result visualization

A completed job is inspected from Index Intelligence or Model Jobs through the same `AnalysisJobDetail` component. The detail view renders the visualization only after `getAnalysisJob(id, true)` verifies the result envelope, request, source pins and hashes. It checks the completed state, job ID and request hash before accepting a response. Job and configuration changes invalidate outstanding requests. The renderer is keyed by job, request and output identity.

## Components

- `AnalysisResultVisualization` dispatches on the approved model ID. Its model views select fields from the verified `output_fields` contract and use the same field descriptors for chart inspection and numerical tables.
- `AnalysisSeriesChart` renders static SVG paths on a shared UTC date scale. A keyboard-operable session slider exposes the exact backend values for each plotted series. `AnalysisStateTimeline` uses separate labelled categorical lanes for trend and volatility.
- `AnalysisMetricTable` uses the existing `LocalDataTable` search and ten-row pagination. Tables scroll inside the result panel, with row and column headers.
- `analysis-result-display.ts` contains presentation-only adapters for dates, units, Decimal text and null-aware chart segments.

## Model views

| Model | Chart and summary | Table |
| --- | --- | --- |
| EMA Trend | Close, fast and slow EMA on an index-point axis. Crossover markers use the returned post-close event. The summary shows the relationship, latest crossover and configured windows. | Close, both EMAs, difference, relationship, crossover and warm-up. |
| Trailing Momentum | Returned momentum on a percentage scale with a zero reference. The summary shows the actual comparison session and observed lookback. | Close, comparison date and close, lookback, momentum and warm-up. |
| Relative Strength | Separate normalized level, ratio and return-difference modes. Series follow the stored source order, and the first source is labelled as the benchmark. Coverage uses stored aligned sessions and exclusions. | The same fields as the selected chart. Unselected source fields stay hidden. |
| RSI | RSI and both configured thresholds on a fixed 0–100 axis. The summary shows the returned historical state and warm-up. | All RSI output fields. |
| Historical Risk | Historical volatility and tail metrics in a percentage view, plus current and maximum drawdown. The summary separates tail availability, optional correlation, open drawdown and completed recovery. | Volatility, drawdown and all-field views preserve sample counts, dates, lengths and units. |
| Market Regime | Independent trend and volatility lanes with labelled unknown states. Price levels, trend distances and volatility thresholds use separate scales. | Both classifications and their reasons accompany the trend or volatility feature set. |

## Numerical presentation

Decimal returns and return differences become percentages. Index levels, normalized values, ratios, correlation and RSI retain their native scales. Tables and the chart inspector format Decimal strings by moving the radix point for percentages, preserving every supplied digit. Table cells expose canonical Decimal text on hover. Chart geometry uses a finite numeric conversion and a bounded Decimal representation. Axis tick labels are rounded for readability.

Charts use the actual observation date domain, leaving calendar gaps at their true spacing. A null metric ends its path segment. An isolated non-null observation appears as a dot. No warm-up value is imputed. The maximum 2,000 sessions fit directly into bounded SVG paths, so the renderer keeps all observations, endpoints and crossover events without sampling. The path geometry is memoized while inspecting sessions. Dense markers and categorical ticks may overlap visually at full range. The session inspector and table retain the original data.

## Evidence and accessibility

The evidence panel keeps model and calculation versions, implementation revision, resolved parameters, requested and observed dates, source ordering, quality warnings, source coverage and UTC evaluation time. Full hashes remain behind the expandable copy controls. Archived results are displayed from their stored evidence. Research-only and unverified-evidence labels remain visible above the presentation.

The charts use labelled axes, patterned series, the active FinIntel theme and no animation. Native selectors and range controls support keyboard use. Tables provide a textual alternative for every plotted series and classification. English and Simplified Chinese use the existing locale catalog. Risk assumptions describe historical price-index returns and empirical losses, with annualization applied to volatility only.

## Validation

Mounted tests exercise synthetic envelopes through the unmodified transport validator before rendering, check all six model views, compare table values and the chart inspector, and cover dates, nulls, localization, and a 2,000-observation result. Job detail regressions cover stale and integrity-rejected responses. Native and backend contract tests remain part of validation. Installed-desktop acceptance with private historical data is a separate owner check.
