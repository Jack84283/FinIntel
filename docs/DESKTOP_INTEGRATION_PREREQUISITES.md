# FinIntel — Desktop Integration Prerequisites

Status: in progress. This branch contains the initial English / Simplified Chinese interface infrastructure. Native backend packaging and provider credential management are NOT implemented yet.

## A. Localization
- Persist `en` and `zh-CN` in device-only workspace preferences; default to English.
- Explicit language selector under Preferences; the navigation and overview use translation keys.
- Continue converting all remaining page components, prompts, validation errors, tables, headings, loading/empty/error states, and accessibility labels to translation keys. No string-based automated runtime translation.
- Keep stock symbols, data-source identifiers, numerical formats, and original provider evidence unchanged. Later format dates, numbers and currencies via `Intl` according to language/region independently from market currency.
- Gate acceptance on both locale screenshot review and frontend tests; changing languages must not initiate data acquisition.

## B. Single desktop environment
- Package the Python FastAPI service as an app-owned sidecar compatible with macOS Apple Silicon and Tauri's bundle layout, instead of assuming the user has separately installed Python.
- Tauri should launch one child at startup and wait for health readiness; connect through the existing allowlisted native IPC commands. On exit, terminate only the child process owned by this app; never kill unrelated processes or blindly bind to an occupied port.
- Allocate/validate loopback endpoint, handle process crash/restart, expose truthful health/error states, and avoid exposing the backend on LAN.
- Persist databases and settings in an OS app-data directory, not in the read-only application bundle. Include migration/backup compatibility from today's development database.
- Keep web/Vite development mode supported; development backend startup may remain a separate developer command. CI must test bundled Mac clean-machine startup and shut down.
- Current Tauri config DOES NOT bundle a Python sidecar. This feature is pending.

## C. Data source credentials in Settings
- Provide configured/not configured indicators and set, replace, delete, test connection actions for Alpha Vantage, Alpaca SIP (key ID and secret), Tiingo, and Equibles.
- Use the macOS Keychain, accessed only through trusted native commands or a secured backend/native bridge. The frontend should send a secret only at explicit user submission and never get it back on reads. Do not store API keys in localStorage, ordinary preferences, SQLite, Git, logs, or browser-facing status endpoints.
- Add native IPC input validation, bounded payload lengths and sensitive-data redaction. Provider adapters must resolve secrets at request time (Keychain primary; environment fallback only for existing CLI/backfill developer workflows during migration). Avoid backend restart to change credentials.
- No 'test' action should silently start a history download. Display provider-specific authentication and entitlement errors without exposing credentials.
- Ensure child backend and background backfill worker can securely access configured credentials without command-line arguments or secret-bearing environment exposure when possible. Design explicit trust boundary before implementation.
- Threat model: loopback custom headers + Origin are insufficient for a standalone untrusted local process. Do not expose mutation APIs to arbitrary other local processes; strengthen process-to-process authorization during sidecar integration.

## Merge gates
1. All frontend text used in supported routes translated, English + zh-CN review complete, tests green.
2. One-click native app boots/stops backend without opening a terminal; clean-machine bundle acceptance green.
3. All four providers configurable through Settings, no secret value returned to UI, credentials survive app restart and deletion works.
4. Existing SQLite and provider acquisition/backfill/validation regressions all pass.
5. Do not merge until the remaining requirements above are implemented and verified.
