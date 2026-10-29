# Market Explorer

The Market screen is local-first. On the first visit in a desktop session it selects `SP500` (S&P 500) and reads the FRED close-only history already stored in SQLite. Opening Market never downloads data. If the selected index has no local observations, the screen shows an empty state with a link to Data Acquisition.

## Instrument types

The Explorer searches stored stock and ETF series together with the five supported index codes: `SP500`, `DJIA`, `NASDAQCOM`, `NDX`, and `VIX`. Equity history remains in the existing `series` and `daily_prices` tables. Index history remains in `index_daily_closes` and is read through `/api/v1/market-data/index-history`.

Indexes are represented only by `{session_date, close}` observations. The index view uses a closing-level line chart and a Date/Close table, and does not manufacture open, high, low, or volume values. Equity selections continue to use provider-specific OHLCV history and preserve the selected provider when navigating back to Market.

## Selection behavior

The selected instrument is held in the desktop session state. The default is used only when there is no previous selection. Navigation from Data Acquisition or Action History passes an explicit equity selection and overrides the default; returning to Market through the sidebar preserves the current selection. Reloading local data refreshes catalogs and history without replacing the selection.

Index downloads remain explicit actions in Data Acquisition. Provider credentials and acquisition behavior are unchanged.
