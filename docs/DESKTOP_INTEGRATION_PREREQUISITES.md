# FinIntel — Desktop Integration Prerequisites

Status: implementation in review. English/Simplified Chinese screen catalog, a packaged native Python sidecar implementation, and OS credential-vault Settings UI have been committed. CI and clean-machine acceptance are NOT complete.

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
- Release Tauri configuration bundles an OS/architecture-specific Python sidecar. Clean-machine installer acceptance remains pending.

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

## Project-wide operating-system portability rule

Any implementation that touches operating-system APIs, filesystem locations, process management, installation, key storage, native notifications, network services, startup/shutdown, packaging, or background execution MUST assess macOS, Windows, and Linux before the design is approved.

- Maintain one shared React/TypeScript interface, Python services, SQLite domain/storage contracts and analysis logic.
- Put native behavior behind narrow platform adapters. Do not scatter `sys.platform`, hardcoded executable names, slash-dependent filesystem paths, or platform-specific commands across shared business logic.
- Support the OS-specific secure credential mechanism: macOS Keychain, Windows Credential Manager, and Linux Secret Service when available. On Linux without a secure secret store, fail safely with actionable setup guidance; never silently write plaintext secrets.
- Use each platform's user data/config/log directories, file permissions, process lifecycle, and shutdown model. Do not assume macOS paths or POSIX shells.
- Produce the proper per-OS Python sidecar and Tauri bundle with architecture-specific build artifacts; do not assume the macOS build runs elsewhere.
- Treat platform-dependent background tasks, system tray/menu, update mechanisms, signing, firewall behavior and process spawning as explicitly portable design areas.
- Add unit coverage for OS adapters, and Windows/macOS/Linux CI build checks. Track native smoke-test evidence by platform; a successful CI build does not prove an installer works.
- Accessibility, English/Chinese translations, data and credential schema behavior must be consistent across supported systems.
- macOS is the first acceptance target; Windows and Linux may follow as tracked acceptance stages, but an OS-specific implementation must not be presented as cross-platform complete without those stages passing.

This is a standing engineering requirement for future FinIntel PRs, not limited to the current desktop integration work.

## October 26 implementation evidence and limits

- Locale switching uses reactive subscriptions and covers navigation, Overview, Preferences, core acquisition/validation/history/index/market pages, and a localized date-picker. Remaining interpolated/provider-originated error copy and real-screen bilingual QA must still be audited.
- Release builds specify an OS/architecture-specific PyInstaller Python sidecar. Tauri owns startup/shutdown, selects a loopback port, passes a per-process token, chooses a platform app-data SQLite location, and retries sidecar startup after an unexpected exit. Browser-only development retains separate Python startup. Legacy SQLite database import and installed-package acceptance remain open.
- The sidecar requires a process token on all local API routes. This is a defense against casual cross-process requests, not a substitute for OS account security or protection from malicious same-user processes.
- The Settings provider card offers save, replace, delete, and configured/not-configured status for Alpha Vantage, Alpaca, Tiingo, and Equibles. Python keyring uses macOS Keychain, Windows' protected vault, and Linux Secret Service/KWallet when installed. No saved secret is returned through the UI read endpoint. A full live provider-authentication Test Connection workflow is not implemented.
- For packaged builds, provider adapters obtain credentials from the OS vault rather than environment variables; environment fallback remains for separate developer CLI/backend workflows. Missing or locked Linux secret services must fail safely.
- Cross-platform native build script generates the PyInstaller sidecar for each host target; CI attempts macOS, Windows and Ubuntu Rust checks. Actual .app/.dmg, Windows installer and Linux package launch, keyring integration, process exit/restart, and migration must still be verified on real installations before marking complete.
- CI may be queued/cancelled across rapid branch commits. Never interpret committed code as passing tests until the latest GitHub workflow has a successful conclusion.

## Database backup and restore policy (October 27)

- FinIntel does **not** run a daily or periodic database backup job. Opening or leaving the app running does not create scheduled snapshots.
- Use **Preferences → Database backup & restore → Backup now** to make a manual SQLite snapshot.
- A verified recovery snapshot is created automatically before an existing database undergoes a schema-version upgrade, or before a user confirms **Merge** or **Replace** restoration.
- Restore offers **Cancel** (no write), **Merge** (retain newer market observations and add missing data), and **Replace** (overwrite, with a typed confirmation). Backups from the former daily policy, if already created, remain visible and restorable.
- Backup files reside next to the active database in its private `backups/` directory. There is no scheduled rotation or automatic deletion of user backups or pre-operation recovery points.
- Validate on a disposable full-size copy before merging: SQLite integrity, price/series/history preservation, interrupted restore behavior, and rollback to the pre-restore snapshot. Migration from a developer database location into an installed app remains separate acceptance work.
