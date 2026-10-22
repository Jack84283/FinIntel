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
  Optional `start_date`, `end_date` (ISO dates), `limit` 1–1000.
  Prices encoded as strings, `has_more` for bounded pagination.
- `GET /api/v1/market-data/imports`: recent provenance and import stats,
  `limit` 1–100.

These routes are unauthenticated and MUST remain bound to 127.0.0.1 for
development. They are not the PWA tablet API. Do not expose them to LAN or
Internet. There are no write endpoints and no automatic migrations on GET.
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
Multi-provider normalization, authentication/authorization, native backend
packaging, continuous ingestion scheduling, corporate-action adjustments,
multi-year pagination/cursors, encryption and market charts/grids remain
explicit follow-up scope.

## Provider-to-SQLite integrated write path (GitHub PR #6)

The Markets UI can now explicitly request an Alpha Vantage daily series.
The backend validates provider output using PR #2's strict Pydantic contract,
then reuses the PR #4 snapshot importer with a private temporary file.
SQLite's unique daily-session keys and single transaction still protect
the database against duplicated or partial price records. The original market-price tables remain unchanged. A fifth application table, symbol_catalog, is added by the v1-to-v2 migration. The UI automatically refreshes
after a successful commit. The legacy explicit JSON export/import tools
remain supported for audit and offline workflows.



## SQLite schema v2 — Symbol catalog (GitHub PR #6)

A versioned, additive migration creates the **fifth application table**, symbol_catalog, with columns: symbol (primary key), name, asset_type (Stock or ETF), exchange, source (starter or alpha_vantage), and updated_at (UTC sync time). All four historical price and ingestion tables remain intact.

New installations initialize v1 market data and then upgrade to v2; existing v1 installations migrate on the next explicit database write. Existing v1 data stays readable without automatic write migration on GET. The catalog lists discoverable securities and is independent from historical price series; a symbol in the catalog does not imply a market price history has been imported.

The offline catalog starts with 63 common tickers. An explicit LISTING_STATUS request, using the privately stored provider key, retrieves active US stocks and ETFs as CSV and replaces the local symbol directory atomically after complete validation. Failed sync cannot change the existing directory or prices. The frontend uses GET /api/v1/market-data/symbols for bounded search and POST /api/v1/market-data/symbols/sync for an explicit update.

Before testing a version upgrade, back up data/finintel.sqlite3 using the SQLite backup API or while all writers are stopped.
