# Architecture Notes (historical PR #1)

> The first section records the bootstrap design. The current runtime is
> described in the section at the end of this document.

The PR #1 bootstrap runtime had three distinct components:

1. **React + TypeScript** is a client-only UI with no database access.
2. **Tauri/Rust** hosts the native application and exposes only `backend_health`, which requests a fixed allowlisted Python endpoint on loopback. It does not expose a general-purpose HTTP forwarding command.
3. **Python/FastAPI** currently exposes only a typed `GET /api/v1/health` endpoint.

At that stage, development used a **manually launched** Python service at
`127.0.0.1:8765`. Browser-only development used the Vite proxy; native Tauri
mode used Rust IPC. The health API did not accept financial information,
parameters, filesystem paths or user input.

**Historical security boundary:** This endpoint was not authenticated in PR #1;
it carried no sensitive data and was not intended for private datasets.
Preventing arbitrary URLs from reaching Rust commands remains a design rule.

**Historical data boundary:** SQLite schema, ingestion and public API adapters
were reserved for PRs #2 and #3.

**Design docs:** FinIntel Google Drive / 02_Architecture / ARC-0001 and ADR-0001; / 06_Engineering & Delivery / ENG-0002.

## PR #1 verification

- `python -m pytest backend/tests`: contract and non-existent market route.
- `ruff check backend` and `ruff format --check backend`: Python lint/format.
- `npm run typecheck`, `npm run lint`, `npm run test`, `npm run build`: frontend.
- CI macOS job `cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml`: native Rust compile gate.
- **Historical manual work:** macOS app launch, hand-run Python backend, and complete UI integration (including focus and system-text scaling).

## Current runtime

Release builds bundle a PyInstaller Python sidecar. Tauri selects an available
loopback port, passes a per-process token and an OS app-data SQLite path, owns
shutdown, and retries an unexpected sidecar exit. React uses fixed allowlisted
Tauri commands for native health and local reads; browser development keeps the
Vite proxy and separately launched backend.

The backend now includes SQLite persistence, provider acquisition, index history,
membership snapshots, validation jobs, credential-vault settings, and backup /
restore workflows. Mutation routes require the managed process token when the
token is configured. The storage API remains local-only and read-oriented; a
secure cross-device gateway, live quotes, scheduling, adjustments and installed
package acceptance remain open work.
