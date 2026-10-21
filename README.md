# FinIntel (FI)

Private, local-first financial intelligence workstation.

> **Implementation stage:** PR #1 bootstrap only. No financial data acquisition, persistent market database, charting or AI is included in this PR.

## Architecture

- **Desktop:** Tauri 2 (Rust) + React / TypeScript / Vite.
- **Backend:** FastAPI / Pydantic, run as a separate **development** process.
- **Transport:** React calls a typed Tauri command. Rust forwards a bounded health request to the Python backend bound on `127.0.0.1:8765`. In browser-only development, Vite proxies `/api` to that same local service.
- **Later:** authenticated, lifecycle-managed Python sidecar and local SQLite history (PRs #3 and #5).

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

- **PR #2:** Public historical-source adapters and ingestion with known-good fixtures.
- **PR #3:** SQLite migrations, idempotent persistence, and historical-data API.
- **PR #4:** Chart/grid workspace backed by the local API.
- **PR #5:** Bundle/manage the authenticated Python sidecar, offline/resume behavior and native packaging.

This repository uses an independent FinIntel interface inspired by financial workstation workflows; it does not include Bloomberg intellectual property or licensed datasets.
