# FinIntel Storage — SQLite persistence and local historical API

## Purpose and release scope
The first storage milestone separates an explicit network acquisition phase
(PR #2) from an offline, transactional SQLite import phase. It never fetches
provider data from an API request. It is safe to start the backend without a
database. The React UI is deliberately unchanged: market-data charts/grid
are a later milestone.

## Version 1 schema
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
