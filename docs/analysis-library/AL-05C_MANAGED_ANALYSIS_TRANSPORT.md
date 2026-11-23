# AL-05C · Managed Analysis Library transport

AL-05C connects the desktop webview to the existing isolated worker and durable ledger. It supplies the typed transport for AL-06. Model selection, configuration, monitoring, result visualization and bilingual UI copy belong to AL-06.

## HTTP contract

The managed sidecar serves the routes below on loopback. Every route requires the current desktop process token in `x-finintel-process-token`. Tauri supplies the header and keeps the token in the native process.

| Operation | Method and route | Success |
|---|---|---|
| Queue | `POST /api/v1/models/analysis/jobs` | `202` with a queued summary |
| List | `GET /api/v1/models/analysis/jobs` | `200` with `{total, items}` |
| Inspect | `GET /api/v1/models/analysis/jobs/{job_id}` | `200` with a summary |
| Cancel | `POST /api/v1/models/analysis/jobs/{job_id}/cancel` | `200` with `{state}` |
| Retry | `POST /api/v1/models/analysis/jobs/{job_id}/retry` | `200` with an updated summary |

Queue accepts the existing `analysis-model-request-v1` body with a top-level `implementation_revision` pin. The body includes the approved model ID and version, ordered `sources` with full FRED daily price-index identities and SHA-256 snapshot pins, an inclusive ISO date `window`, model-specific `parameters` and a `quality_policy`. The API validates the caller's pins against the local observations and resolves permitted parameter defaults before storing the original request. Relative Strength uses the first source as its benchmark. Risk Profile uses the first source as its primary series. The approved instruments are SP500, DJIA, NASDAQCOM and NDX.

List defaults to 10 records and accepts `limit` from 1 to 100, `offset` through 10,000, and optional `status`. The ledger applies the Analysis Library contract and status predicates before counting and paginating. Job IDs use canonical lowercase UUIDs. Every summary includes the approved revision, canonical resolved `request` and its `request_sha256`. States are `queued`, `running`, `completed`, `failed` and `cancelled`. A running job can expose `cancel_requested` as its status while the worker acknowledges cancellation.

Detail adds a `result` property only when `include_result=true`. It is null until a result is available. Completed single-source jobs expose their pin in `source_snapshot_sha256`. Completed multi-source jobs expose each independent pin in the evidence and have a null summary snapshot. Retry preserves the resolved request, ordered pins, and request hash. It repeats source and quality preflight and applies the shared queue quota and maximum of three attempts.

| HTTP | Stable detail code | Condition |
|---|---|---|
| 403 | `managed_required` | Missing or invalid desktop process token |
| 404 | `job_not_found` | ID absent from Analysis Library history |
| 409 | `queue_full` | Shared active queue at capacity |
| 409 | `source_changed` | A selected snapshot differs from its pin |
| 409 | `job_conflict` | Job state or attempt quota disallows retry |
| 422 | `invalid_input` | Invalid identity, parameters, pin, window, ID or query |
| 422 | `quality_blocked` | Current observations fail the requested quality policy |
| 422 | `resource_limit` | Selected input exceeds worker preflight bounds |
| 503 | `storage_unavailable` | Local storage is unavailable or integrity validation fails |

Worker failures remain in the summary's separate `error_code` against the retained attempt.

## Fixed Tauri IPC and typed client

| Tauri command | Arguments | TypeScript client |
|---|---|---|
| `analysis_jobs_create` | `{request}` | `queueAnalysisJob(request)` |
| `analysis_jobs_list` | `{limit, offset, status?}` | `listAnalysisJobs(page?, filter?)` |
| `analysis_jobs_get` | `{jobId, includeResult}` | `getAnalysisJob(id, includeResult?)` |
| `analysis_jobs_cancel` | `{jobId}` | `cancelAnalysisJob(id)` |
| `analysis_jobs_retry` | `{jobId}` | `retryAnalysisJob(id)` |

`apps/desktop/src/analysis-jobs-api.ts` exports model-specific inputs and results, lifecycle and pagination types, source pins and quality evidence. The caller can omit parameter keys with registered defaults. The resolved summary and evidence contain all parameter values. The list client uses one-based pages of 10. Failures use `AnalysisJobError.code`, which supports localization in the consuming UI. Native transport adds `backend_unavailable` for connection failures and `invalid_response` for incompatible payloads. A browser session receives `managed_required` without invoking a backend.

| Approved ID | Version | Implementation revision |
|---|---|---|
| `builtin.ema_trend` | `1.0.0` | `ema-sma-seed-decimal-v1` |
| `builtin.momentum` | `1.0.0` | `momentum-trailing-observed-v1` |
| `builtin.relative_strength` | `1.0.0` | `relative-strength-common-baseline-v1` |
| `builtin.rsi_state` | `1.0.0` | `rsi-simple-rolling-states-v1` |
| `builtin.risk_profile` | `1.0.0` | `historical-risk-linear95-decimal-v1` |
| `builtin.market_regime` | `1.0.0` | `market-regime-two-axis-v1` |

The native and TypeScript validators use the checked-in `contracts/analysis-jobs.json` projection of the worker registry. A backend test checks the projection against the approved definitions. Regenerate after a deliberate contract change with `PYTHONPATH=backend python scripts/generate_analysis_transport_contract.py` and update the TypeScript model types and tests.

## Historical results and compatibility

The ledger verifies the archived request, model-specific evidence, output and full result envelope before detail retrieval. Both desktop boundaries validate the `analysis-model-result-v1` contract independently. They bind it to the resolved request and verify the model revision, requested window, parameter identity, ordered per-source pins, observation counts, quality states, registered point fields and canonical SHA-256 values. `require_complete` requires verified evidence for every source. `allow_unverified` retains quality warnings and marks the explicit override.

Historical verification uses the stored evidence. Updating the market database can prevent a new run or retry with an older pin while completed results remain available. This transport uses the existing ledger schema, isolated worker, shared quotas and source-writer reservation. Existing `/api/v1/models/jobs` routes and `model_jobs_*` commands remain SMA-only.

CI covers disposable backend data, the frontend client and packaged native targets. Installed-desktop acceptance with owner-controlled private data and real-device validation remain separate acceptance gates. See [Managed analysis execution](AL-05_MANAGED_EXECUTION.md) for the worker and durability boundaries.
