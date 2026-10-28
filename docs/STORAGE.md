# FinIntel Storage — SQLite persistence and local historical API

## Purpose and release scope
The first storage milestone separates an explicit network acquisition phase
(PR #2) from an offline, transactional SQLite import phase. It never fetches
provider data from an API request. It is safe to start the backend without a
database. The React UI is deliberately unchanged: market-data charts/grid
are a later milestone.

## Version 1 schema — original historical price tables
- `instruments`: uppercase ticker symbol.
- `series`: unique (symbol, provider, frequency, price adjustment); currently
  Alpha Vantage / 1d / raw_unadjusted.
- `daily_prices`: unique (series, session date), OHLC prices as decimal TEXT,
  integer volume, retrieval timestamp, reference to the ingestion that last
  changed the stored values.
- `ingestion_runs`: append-only run history with UTC retrieval/import times,
  SHA-256 of the validated snapshot, sanitized provider source URL, provider
  terms URL and optional source refresh date, session date range, row counts,
  insert/update/unchanged/stale counts.

`PRAGMA user_version` is the migration version. The first migration creates
all tables in one explicit transaction; unknown or future versions stop
without trying to destroy data. Future schema changes must add migration
steps rather than reinitializing the database.

## Correctness and precedence
The importer verifies a real local JSON file (not a symlink, <=25 MiB),
Pydantic's complete historical batch contract, decimal-valued price
integrity, chronological unique dates and approved sanitized source URLs
**before** opening SQLite for mutation. All daily prices and ingestion run
metadata commit atomically together; errors roll back. Date keys ensure a
second identical import inserts no duplicates. Older retrieval timestamps
do not overwrite more recently retrieved bars; they may add previously
missing dates. Every valid import attempt has an audit record.

Note: `retrieved_at` is provider snapshot acquisition time, **not exchange
timestamp**; the daily session_date comes from the provider. Raw unadjusted
series are not split- or dividend-adjusted. Reconciliation of different
data vendors and currencies needs future explicit business rules.

## API contracts — development use only
- `GET /api/v1/market-data/instruments`: stored series and date coverage.
- `GET /api/v1/market-data/{SYMBOL}/daily`: chronological prices.
  Optional `start_date`, `end_date` (ISO dates), `limit` 1–4000.
  Prices encoded as strings, `has_more` for bounded pagination.
- `GET /api/v1/market-data/imports`: recent provenance and import stats,
  `limit` 1–100.

These routes are read-only and remain bound to loopback. In development they
may be called without a process token; packaged sidecars require the token on
every route. They are not the PWA tablet API. Do not expose them to LAN or
Internet. There are no storage write endpoints and no automatic migrations on
GET.
A separate authenticated HTTPS gateway is required for M1.5 tablet access.

## Local workflow
```sh
# From FinIntel root, after getting an Alpha Vantage key and a successful PR #2 fetch:
source .venv/bin/activate
python -m finintel.storage import data/raw/AAPL.json
python -m finintel.storage inspect --symbol AAPL
python -m finintel.storage imports
python -m pytest backend/tests -q

# Optional isolated database:
FININTEL_DB_PATH=data/private/test.sqlite3 python -m finintel.storage import data/raw/AAPL.json
```

Store and back up `data/finintel.sqlite3` privately; live SQLite files
must be backed up with the SQLite backup API or while all writers are
stopped. Do not commit any SQLite DB, private market-data snapshots,
tokens or user portfolios. The default database and journal files are
gitignored. Schema and query acceptance tests run against temporary SQLite
databases and a **synthetic**, non-investable IBM fixture.

## Deferred
Multi-provider normalization, continuous ingestion scheduling,
corporate-action adjustments, multi-year pagination/cursors, encryption and
cross-device access remain explicit follow-up scope.

## Provider-to-SQLite integrated write path (GitHub PR #6)

The Markets UI can now explicitly request an Alpha Vantage daily series.
The backend validates provider output using PR #2's strict Pydantic contract,
then reuses the PR #4 snapshot importer with a private temporary file.
SQLite's unique daily-session keys and single transaction still protect
the database against duplicated or partial price records. The original market-price tables remain unchanged. A fifth table, symbol_catalog, is added in v2 and expanded to four instrument classes in v3. The UI automatically refreshes
after a successful commit. The legacy explicit JSON export/import tools
remain supported for audit and offline workflows.



## SQLite schema v3 — Persistent instrument catalog (PR #6)

The database keeps the existing four historical-market tables and adds
`symbol_catalog` as a **fifth table**. Its columns are `symbol` (primary
key), `name`, `asset_type`, `exchange`, `source`, and `updated_at`.
The valid classifications are `Stock`, `ETF`, `Index`, and
`Mutual Fund`. An instrument's directory record is independent from
`series` and `daily_prices`, which exist only after historical data import.

The migrations run sequentially: v1 initializes the historical tables;
v1 → v2 adds stock/ETF catalog rows; v2 → v3 safely expands the catalog
classification to include indexes and mutual funds without changing
historical price or ingestion tables. The 73 starter records are seeded in
SQLite on first symbol-search access, **even without an API key**, so they
survive subsequent app restarts. Unlike the historical-price GET routes,
the symbol-directory GET intentionally initializes/migrates its local table.

The initial classifications include 63 stock/ETF examples, six index
references (`^GSPC`, `^DJI`, `^IXIC`, `^RUT`, `^NDX`, `^VIX`),
and four mutual funds (`FXAIX`, `VFIAX`, `VTSAX`, `SWPPX`).
The index IDs are reference labels, not guaranteed Alpha Vantage mappings.
The current daily-price adapter fetches only individual stocks/ETFs;
unsupported index and mutual-fund acquisitions are blocked until separately
validated provider adapters exist.

An explicit `LISTING_STATUS` sync replaces only **Stock/ETF catalog rows**
after complete CSV validation. Reference indexes and mutual funds are
retained, as are all imported daily prices and provenance. Failed provider
syncs do not change the previous saved catalog. The UI performs bounded
SQLite searches, rather than loading the full directory in the webview.

Before a migration, make a private backup of `data/finintel.sqlite3` using
SQLite's backup API or while all writers are stopped.

## Reading up to ten years in Markets

The local historical-price GET now accepts a maximum `limit=4000` (formerly
1000) to support approximately 2,520 daily observations over ten calendar
years. `latest=true` maintains chronological output after selecting the
newest bounded records. Search of older or broader archives still requires
date filters, not an unlimited response. The underlying daily_prices and
ingestion_runs schema and raw decimal-text persistence are unchanged.
Charts use weekly/monthly/quarterly aggregates only in the frontend; daily
rows are never overwritten with aggregated observations.

## Separate Alpaca and Alpha Vantage price series (PR #6)

No new schema migration is necessary for provider selection. The existing
unique series key (symbol, provider, frequency, adjustment) separates
alpaca_sip from alpha_vantage. Each series has its own daily_prices,
ingestion_runs, source URL and provider terms. Both have raw_unadjusted
price adjustment; the sources are never silently merged. The read API's
provider selector chooses which series is shown in Markets.

Alpaca retrieval always uses the HTTPS historical SIP/raw endpoint and
bounded page-token pagination; source URL metadata excludes credentials,
and both the fetched bars and sanitized provenance are validated before
transactional insertion. A failed or partial response leaves previously
stored data untouched. The SQLite application schema stays at v3.
