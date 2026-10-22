# FinIntel (FI)

Private, local-first financial intelligence workstation.

> **Implementation stage:** Native shell, historical provider, shadcn UI and SQLite persistence are merged. The read-only Market Data Explorer is being developed on GitHub PR #5.

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
