# PR #1 Architecture Notes

The initial runtime has three distinct components:

1. **React + TypeScript** is a client-only UI with no database access.
2. **Tauri/Rust** hosts the native application and exposes only `backend_health`, which requests a fixed allowlisted Python endpoint on loopback. It does not expose a general-purpose HTTP forwarding command.
3. **Python/FastAPI** currently exposes only a typed `GET /api/v1/health` endpoint.

Development uses a **manually launched** Python service at `127.0.0.1:8765`. Browser-only development uses the Vite proxy; native Tauri mode uses Rust IPC. The health API does not accept financial information, parameters, filesystem paths or user input.

**Security boundary:** This endpoint is not authenticated in PR #1; it carries no sensitive data and should **not** be extended for private datasets. PR #5 replaces this bootstrap transport with a lifecycle-managed and authenticated sidecar channel before sensitive APIs are exposed. Prevent arbitrary URLs from reaching Rust commands.

**No data layer yet.** SQLite schema, ingestion and public API adapters are reserved for PRs #2 and #3.

**Design docs:** FinIntel Google Drive / 02_Architecture / ARC-0001 and ADR-0001; / 06_Engineering & Delivery / ENG-0002.

## PR #1 verification

- `python -m pytest backend/tests`: contract and non-existent market route.
- `ruff check backend` and `ruff format --check backend`: Python lint/format.
- `npm run typecheck`, `npm run lint`, `npm run test`, `npm run build`: frontend.
- CI macOS job `cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml`: native Rust compile gate.
- **Still manual:** macOS app launch, hand-run Python backend, and complete UI integration (including focus and system-text scaling).
