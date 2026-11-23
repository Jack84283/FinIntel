# AL-05 · Managed analysis execution

AL-05A introduced the v2 ledger and preserved v1 SMA records. AL-05B connects that ledger to the existing isolated model worker. AL-05C will expose the analysis queue through authenticated HTTP and fixed Tauri commands, with typed clients for AL-06.

## Approval and source pins

The v2 catalog publishes the model availability, parameter schema and metadata. The worker approval table pins the exact ID, version and implementation revision of EMA Trend, Momentum, Relative Strength, RSI State, Risk Profile and Market Regime. A separate fixed Python dispatch table selects the built-in evaluator. Adding an entry to discovery or allowing a model identity in SQLite does not grant execution. The legacy SMA branch continues to use its existing evaluator and contract.

The internal `enqueue_analysis_job` entry point validates the approved request, the ordered local FRED price-index identities, each selected-row SHA-256 pin, quality, and sufficient shared history. It never calculates a model. The ledger stores a canonical `analysis-model-request-v1` payload with resolved parameters and a SHA-256 hash. Retries retain the identical payload and pins. A changed pin requires a new request.

The child verifies the stored request contract, canonical hash and attempt. The fixed evaluator reads its inputs in a single read-only snapshot, checking each identity, pin and quality status before it calculates. The worker verifies the `analysis-model-result-v1` against the registered result contract. For the final source reread it reserves the SQLite writer slot on the market database for up to a one-second acquisition wait and enables query-only mode. It rechecks the pins and quality on that connection and holds the reservation through the separate job transaction. Analysis completion takes the market lock before the ledger lock. An unavailable reservation, changed pin or changed quality status prevents completion. Releasing the reservation rolls back the market transaction without modifying its schema or rows. The reservation coordinates SQLite writers to the same database file. Database file replacement by external tools must be kept outside running jobs. Later imports can make a completed result historical.

## Ledger and recovery

AL-05B uses the existing v2 schema and migration. See [AL-05A](AL-05A_DURABLE_JOB_PERSISTENCE.md) for migration and checksum details. Queue ordering and the active-job limit are shared by the legacy and analysis contracts. Job state is queued, running, completed, failed, or cancelled. Cancellation sets a flag on the running attempt and fences subsequent completion, while cancelling queued jobs immediately. Restart reconciliation marks interrupted attempts failed or cancelled and preserves queued jobs. Only an explicit retry can requeue a failed or cancelled job.

The ledger checks the result envelope, model revision, resolved parameters, observation range, ordered per-source evidence and both analysis hashes against the stored request. A private full-envelope digest protects the archived evaluation timestamp as well as the numerical payload. Detail retrieval verifies the original evidence and hashes without looking up current market observations. Revised market data can block a new run or retry while the original historical result remains inspectable.

Error codes carry stable categories, including `invalid_input`, `source_changed`, `quality_blocked`, `resource_limit`, `storage_unavailable`, `interrupted` and `worker_failure`. A corrupt queued request is failed without preventing later jobs from being claimed. Child logs receive neither source values nor exception details.

## Worker and transport boundaries

The existing ownership lock allows one supervisor and one child. Limits remain 32 active jobs, three attempts, 90 seconds runtime, 2 GiB of platform-specific worker memory controls and 4 MiB retained results. Each source is capped at 2,000 observed sessions. Relative Strength can select up to four sources, for at most 8,000 aggregate selected observations and 2,000 result rows. Child inputs travel through the local ledger; its command line carries only the job ID and generation. The child inherits an allowlisted environment, has no outbound network access, and uses the existing shutdown and cleanup paths.

A synthetic macOS smoke run at the 2,000-observation cap measured four-source Relative Strength at 0.49 seconds and 81 MiB peak RSS, and two-source Risk Profile with 999-return windows at 3.56 seconds and 88 MiB. A single-source Market Regime run with a 1,000-observation calibration window measured 0.74 seconds and 75 MiB. All three included isolated child startup, validation and SQLite persistence. Results were below 1.8 MB each. These are development measurements on disposable data.

The current managed HTTP/Tauri commands and TypeScript client retain the SMA-only contract. AL-05C should use the internal enqueue entry point from an authenticated fixed route, expose the typed request/result distinction and safe errors, and keep existing SMA history compatible. AL-06 will consume that transport. Installed-desktop, private-data, and real-device acceptance remain owner validation gates.
