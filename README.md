# FinIntel (FI)

Private, local-first financial intelligence workstation.

> **Implementation stage:** Native shell, validated historical data, shadcn/Tailwind UI, SQLite and the Market Data Explorer are merged. GitHub PR #6 adds explicit in-app provider acquisition to SQLite.

## Architecture

- **Desktop:** Tauri 2 (Rust) + React / TypeScript / Vite.
- **Backend:** FastAPI / Pydantic, run as a separate **development** process.
- **Transport:** React invokes fixed allowlisted Tauri commands for health and local market-data reads; Rust calls Python bound on `127.0.0.1:8765`. Browser-only Vite development proxies `/api` to the same service.
- **Data storage:** validated snapshot JSON is imported into the versioned local SQLite database (M1 storage milestone, GitHub PR #4). The Python sidecar lifecycle and authentication remain a later delivery phase.

## Prerequisites

- macOS Apple Silicon (initial target; Windows/Linux portability is a goal).
- Python 3.11+.
- Node.js 22.12+ (or the latest supported LTS) and npm.
- Rust stable with Cargo and [Tauri desktop prerequisites](https://v2.tauri.app/start/prerequisites/).

## Development: two terminals

From the repository root:

**Terminal 1 (Python backend):**

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e "./backend[dev]"
python -m uvicorn finintel.main:app --host 127.0.0.1 --port 8765
```

Verify with `curl http://127.0.0.1:8765/api/v1/health`.

**Terminal 2 (frontend):**

```sh
npm install
npm run dev
```

Open `http://127.0.0.1:1420` to verify browser development mode, or use `npm run tauri:dev` to launch the native Tauri window. Keep the Python terminal running. Native mode uses the `backend_health` Rust command rather than direct webview access to the backend.

## Quality checks

```sh
source .venv/bin/activate
python -m pytest backend/tests -q
ruff check backend
ruff format --check backend
npm run typecheck
npm run lint
npm run test
npm run build
```

CI runs these checks on Ubuntu, and a separate macOS job checks the Tauri Rust crate. Native macOS build/signing, automatic Python startup, encrypted local storage, market data ingest, and end-to-end UI checks are **not yet verified**.

## Repository layout

```
apps/desktop/        React/Vite frontend and Tauri Rust shell
backend/             FastAPI service with /api/v1/health
contracts/           Health response schema
scripts/             Developer utilities
docs/                Architecture notes and phased scope
.github/workflows/   Checks for frontend, backend and macOS Rust
```

## Private project precautions

The repository was **public** when bootstrapped. Do not commit proprietary models, keys, portfolio information, downloaded private financial data or personal datasets until visibility and access have been reviewed. The .gitignore excludes local databases, generated downloads, credentials and build output; it is not a substitute for access control.

Project specifications are maintained in the FinIntel Google Drive: [ENG-0002 Implementation Roadmap](https://docs.google.com/document/d/1D44Vs_Lmgn3hKRqw-8xYermLjhZ5aS6PlRmvUDeGxLY/edit).

## Next implementation milestones

- **PR #2:** Alpha Vantage historical OHLCV adapter, integrity tests, privacy-safe provenance and a one-shot local snapshot CLI (see below).
- **M1 storage milestone (GitHub PR #4):** SQLite migrations, idempotent persistence, snapshot ingestion history and read-only historical-data APIs.
- **Next M1 dashboard milestone:** React chart/grid workspace backed by the stored data API.
- **PR #5:** Bundle/manage the authenticated Python sidecar, offline/resume behavior and native packaging.

This repository uses an independent FinIntel interface inspired by financial workstation workflows; it does not include Bloomberg intellectual property or licensed datasets.

## Historical daily market-data acquisition — PR #2

This is **historical end-of-day** data, not a real-time quote service. The first provider is [Alpha Vantage TIME_SERIES_DAILY](https://www.alphavantage.co/documentation/#daily). It returns **raw / unadjusted** OHLCV. The free compact endpoint returns **up to the latest 100 trading observations**; the full-history option requires a premium entitlement. Do not mix raw series with corporate-action-adjusted prices without explicit adjustment metadata.

Get your **own** API key from https://www.alphavantage.co/support/#api-key after reviewing its terms for private non-commercial research. The key is not stored in GitHub or printed to the console. With your backend venv active:

```bash
export FININTEL_ALPHA_VANTAGE_API_KEY="YOUR_OWN_KEY"
python -m finintel.market_data --symbol AAPL
python -m finintel.market_data --symbol MSFT
python -m finintel.market_data --symbol NVDA
python -m finintel.market_data --symbol SPY
unset FININTEL_ALPHA_VANTAGE_API_KEY
```

The CLI saves locally to `data/raw/<SYMBOL>.json` (ignored by git). Override with `--output /private/path/snapshot.json` or request `--outputsize full` only with a premium key. The output stores provider identity, requested instrument, source URL **without an API key**, download time, session range (from observations), and validated decimal OHLCV. It refuses malformed data and writes via a temporary file + atomic replacement.

One CLI call normally consumes one provider API request; transient server/network failures may make up to three attempts. Quota/limit notifications are **not retried**. Free accounts have limited daily access; monitor your account's quota and don't loop through many symbols. No automatic scheduler is included.

**Important:** CI uses a clearly **synthetic** IBM API-shaped fixture for deterministic parser tests. Fixture observations are not market prices and must never be used for analysis. Live smoke tests are opt-in, require your own key and do not run in CI. PR #3 will implement persistent SQLite ingestion; these JSON snapshots are a PR #2 staging format, not the final database.

See [Data provider policy](docs/DATA_PROVIDER.md) for terms, rate-limit behavior, failure recovery and provenance. Do not expose the Python development API over your LAN or Internet; the secure tablet gateway is a separate M1.5 milestone.

## SQLite persistence & local history API (M1 storage milestone)

PR #2 fetches and validates a source snapshot; the storage milestone **imports**
that snapshot into a local versioned SQLite database. Imports are explicitly
initiated by the developer, never automatically by application launch. No
network request happens when inspecting the database.

With the existing Python venv active, from the repository root:

```sh
python -m finintel.storage import data/raw/AAPL.json
python -m finintel.storage inspect
python -m finintel.storage inspect --symbol AAPL
python -m finintel.storage imports
```

Default database: `data/finintel.sqlite3`, gitignored along with its journal
files. Override it via environment variable `FININTEL_DB_PATH`, or by passing
`--db /path/to/db.sqlite3` **before** the subcommand.

The backend must be started from the repository root with the same
`FININTEL_DB_PATH` environment value:

```sh
python -m uvicorn finintel.main:app --host 127.0.0.1 --port 8765
curl http://127.0.0.1:8765/api/v1/market-data/instruments
curl 'http://127.0.0.1:8765/api/v1/market-data/AAPL/daily?limit=20'
curl 'http://127.0.0.1:8765/api/v1/market-data/AAPL/daily?start_date=2026-09-01&end_date=2026-10-22'
curl http://127.0.0.1:8765/api/v1/market-data/imports
```

These are **historical**, as-traded, unadjusted price observations. The JSON
API keeps prices as **decimal strings** to prevent precision loss; volume is
an integer. Responses are bounded (daily: 1–1000 rows, default 250;
import history: 1–100). A missing database yields an empty catalog and a 404
for an unknown symbol. No API write endpoints, remote exposure, scheduled
fetching, or browser-side financial data caching are provided.

The data survives backend restart. Reimporting the same file won't duplicate
daily bars; new provider revisions replace changed values in a single SQLite
transaction, while an older retrieval cannot overwrite a newer value. Failed
validation leaves the existing local database untouched.

See [SQLite data storage design](docs/STORAGE.md) for schema, provenance,
backups, security boundaries and verification. A dedicated market-data
**frontend** is still a separate planned dashboard milestone.

## Read-only Market Data Explorer (GitHub PR #5)

After importing historical prices into the SQLite database and starting the Python service,
launch the Mac app with `npm run tauri:dev` and choose **Markets**. You can select a locally
stored ticker, apply a session-date range, inspect the most recent 1,000 records,
view a candlestick and volume chart based on the most recent 70 loaded sessions,
sort and page through a history table, and review source/ingestion metadata.
No provider API requests, file writes or synthetic prices occur on this screen.
If no instruments are available, run `python -m finintel.storage import data/raw/AAPL.json`
and use **Reload local data**. The Python backend and UI must use the same
`FININTEL_DB_PATH` (default: `data/finintel.sqlite3`) and working directory.

For recently loaded data, the read-only history API now supports `latest=true` to
return the newest bounded records in chronological order. Existing clients omitting
this parameter retain the original oldest-first response. Source data remains
historical, unadjusted, and **not** a real-time quote. Automatic fetching/refresh
and packaged Python sidecar support are separate follow-up milestones.

## Integrated market data acquisition — GitHub PR #6

FinIntel's **Markets → Data acquisition** card performs this single-click
workflow: **Fetch → Validate → Transactional SQLite import → Reload Markets**.
It works even when the instrument list is empty, allowing the first ticker
to be added directly from the UI. Historical OHLCV and import history remain in the original four tables. A new **symbol_catalog** master-data table is added in SQLite schema v2. A catalog symbol does not imply its historical prices have been downloaded.

Start the backend **from the repository root** with a key in the **backend's
own environment**. The UI never receives the key; provider responses and
keys are not checked into Git:

```sh
cd /path/to/FinIntel
source .venv/bin/activate
# Set FININTEL_ALPHA_VANTAGE_API_KEY in your local shell securely before launch.
# Never paste the actual key into issues, PRs, screenshots or chat.
python -m uvicorn finintel.main:app --host 127.0.0.1 --port 8765
```

In another terminal, run `npm run tauri:dev`; open **Markets** and choose
a ticker and **Fetch & store**. Select **Latest ~100 sessions** unless your
provider subscription entitles you to full history. One click can consume
one provider API request (up to 3 attempts **only on transient errors**).
The UI displays inserted, revised, unchanged and stale counts. The Markets
catalog, charts, data table and import history refresh after success.

The acquisition-status endpoint returns only a Boolean describing whether
the backend has the key. If the card reports "API key needed" after setting
it, **restart the Python backend process**; a key exported in another shell
is not automatically visible to an already-running server.

The validated response is written to a temporary, owner-private JSON file
and loaded by the pre-existing transactional SQLite importer. The temporary
file is removed; durable acquisition records and OHLCV rows remain in
`data/finintel.sqlite3`. Users can still use PR #2's local JSON export and
PR #4's manual import commands independently.

The development write API accepts only loopback callers, JSON, a dedicated
workstation header, and a constrained browser Origin. This mitigates basic
cross-site browser requests but **does not provide authenticated remote
access** or isolation from untrusted local processes. Do not expose the
FastAPI development server to LAN or Internet. The secure multi-device
gateway and native packaged Python sidecar remain future milestones.

Acquisition is **manual only**, not a scheduler or a live quote. Invalid
provider responses, quota notifications and import failures never replace
previously committed price rows. There are no real-provider calls in CI.


## Persistent instrument directory (PR #6)

The Markets Data acquisition form now offers a searchable **instrument selector**
with four explicit classes: **Individual stocks, ETFs, market indexes, and
mutual funds**. Search by symbol or instrument name, filter by class and choose
a record. The catalog is separate from OHLCV observations: seeing a symbol
in the directory **does not mean historical data is downloaded**.

The local SQLite database `data/finintel.sqlite3` contains a fifth application
table, `symbol_catalog`, whose main columns are `symbol`, `name`,
`asset_type`, `exchange`, `source` and `updated_at`. The first
instrument search initializes the database and saves **73 starter instrument
records** locally, including 63 stocks/ETFs and 10 index/mutual-fund references.
There is no provider request for this initial seed, and records persist after
restarting FinIntel. Existing SQLite v1/v2 databases are upgraded to
**schema v3** on the first instrument search or next database write. Back up
your local database before testing any schema migration.

**Sync symbols** explicitly fetches the Alpha Vantage `LISTING_STATUS`
active US stocks and ETFs, and transactionally updates only those two classes
in the local catalog. It **preserves** index/mutual-fund references and all
historical price/import records. Sync uses the backend API key and can consume
provider quota. Symbols may also be entered manually for instruments absent
from the directory, but classification is preserved when selecting entries.

**Provider capability boundary:** the current Alpha Vantage
`TIME_SERIES_DAILY` adapter handles only stock/ETF symbols. Selecting an
Index or Mutual Fund shows its reference metadata but disables Fetch & store;
the backend also rejects unsupported types. Index identifiers such as
`^GSPC` are display/reference IDs, **not** validated Alpha Vantage daily
ticker mappings. Dedicated index and mutual-fund data adapters are future
work, and the directory is not a live listing feed.

To inspect your local records:

```sh
sqlite3 data/finintel.sqlite3 "SELECT asset_type, COUNT(*) FROM symbol_catalog GROUP BY asset_type ORDER BY asset_type;"
sqlite3 data/finintel.sqlite3 "SELECT symbol, name, asset_type, source FROM symbol_catalog WHERE asset_type='Index' ORDER BY symbol;"
```

## Multi-year historical research (PR #6)

Alpha Vantage's `TIME_SERIES_DAILY` endpoint supports **compact** (last 100
trading sessions; free or premium) and **full** (up to 25+ years where an
instrument has traded; premium key required). It does not accept a direct
five-year or ten-year response-size option. FinIntel imports the provider's
full validated dataset when explicitly requested and retains it in SQLite.

The Markets Explorer now offers **Latest 100 sessions, Last 1 year, Last 5
years, Last 10 years, and Custom dates**. Those are local stored-history
filters, not additional provider requests. The date range is based on
calendar years, while actual trading-day counts vary. 5 and 10 years typically
contain about 1,260 and 2,520 daily records.

To support this, the local daily-read endpoint remains bounded but now allows
up to **4,000 rows** per request (formerly 1,000). The React/Tauri bridge and
validation are aligned with this limit, so ten-year history can be inspected
in the table. Custom requests beyond 4,000 available observations are
explicitly marked as bounded; date filtering can access older sessions.

Charts aggregate stored daily prices into genuine OHLCV weekly candles for
1 year, monthly candles for 5 years, and quarterly candles for 10 years,
preserving open/high/low/close/total volume per interval. This is solely a
chart-resolution choice: **daily** observations remain unchanged in SQLite
and are visible in the table. Raw/as-traded prices are not adjusted for
splits or dividends, which matters especially over a decade.

Free Alpha Vantage keys cannot fetch five or ten years of **daily** records
from this endpoint. A longer selected period will only display history
already stored locally; choosing it does not bypass premium entitlements or
fabricate missing dates.

## Selectable historical price APIs (PR #6)

Markets → Data acquisition → Data source now has two **working API** choices:
**Alpaca SIP** (5 years, 10 years, available since 2016) and
**Alpha Vantage** (compact 100 daily sessions on free; full history requires premium).
Selecting the source, ticker and history only configures the request.
The user must explicitly click **Fetch & store** to call an external provider.
Both providers use the same validated SQLite ingestion pipeline and price table,
but each symbol/provider combination has its own history and provenance.

For Alpaca, set **FININTEL_ALPACA_API_KEY_ID** and
**FININTEL_ALPACA_API_SECRET_KEY** in the local Python backend process environment.
For Alpha Vantage, set **FININTEL_ALPHA_VANTAGE_API_KEY** instead.
Do not enter secrets in the webview or commit them to the repository.
After changing credentials, restart the Python backend.

Alpaca uses a fixed HTTPS historical SIP endpoint, raw daily OHLCV values,
explicitly selected 5/10-year or available-since-2016 window (ending yesterday
UTC), and bounded pagination. Free Basic plan historical SIP access is
delayed and account entitlements still apply. The adapter **does not**
silently fall back to IEX when SIP access is denied.

The Markets instrument selector distinguishes the same ticker from multiple
stored providers; its selected source also controls chart/table reads.
The SQLite series key includes provider and adjustment, so importing Alpaca
does not overwrite Alpha Vantage OHLCV. Historical 1/5/10-year view filters
continue to query SQLite and never make additional provider API calls.

Yahoo Finance, Stooq and Twelve Data are **not yet integrated** as automatic
API sources. Their permissions, stability and credential contracts need
separate validation. The existing stock/ETF daily adapters do not support
index or mutual-fund acquisition, even though those classes appear in the
symbol directory.
