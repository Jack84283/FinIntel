# AL-01 · Model registry and evidence contracts

AL-01 adds a discovery contract and reusable Python request and result contracts for Analysis Library models. The [AL-00 architecture audit](AL-00_ARCHITECTURE_AUDIT.md) records the boundaries for analytics, research models, job execution, and backtesting. AL-01 adds no numerical models or database migrations.

## Discovery and execution

| Surface | Contract |
| --- | --- |
| `GET /api/v1/models/registry` | Existing registry v1 response, unchanged. |
| `GET /api/v1/models/registry/v2` | Registry v2 response with model category, implementation and evidence revisions, typed parameter schema, source identities, observation limits, quality requirements, output fields with units and nullability, timing, and execution restrictions. The existing managed sidecar token applies. |
| `POST /api/v1/models/evaluate` and `POST /api/v1/models/jobs` | Existing SMA-only input, execution, and result contracts. |
| Analysis request, evidence, and result v1 | Reusable Python types for later phases. No HTTP execution route or Tauri command accepts these envelopes in AL-01. |

A catalog entry declares `released` or `proposed`. This describes metadata availability. The existing SMA evaluator, API, and worker still independently enforce their executable allowlists. Discovery opens no market or job databases and loads no third-party models, provider connections, module paths, or user code.

The current entry is `builtin.sma_crossover@1.0.0`, revision `sma-crossover-observed-close-v1`, using `index-analytics-v2` input analytics. Its fast window defaults to 5 and accepts 2–100, its slow window defaults to 20 and accepts 3–252, and fast must be less than slow. Evaluation needs at least slow plus one observed close and refuses more than 2,000 observations. SMA uses 28-digit Decimal arithmetic on actual observed closes. The model emits a signal after the close, with a long signal only when fast exceeds slow. The available sources are FRED SP500, DJIA, NASDAQCOM, and NDX daily price-index levels. VIX, other providers, and incompatible return semantics are rejected.

The existing API and jobs use SMA-specific literals and models. The job ledger schema pins the SMA ID and version, and the worker invokes only the SMA evaluator. The native command validators, TypeScript model research and job clients, and pure Phase 2C1 backtest recognize the legacy evaluation contract. These surfaces retain the existing SMA result wire format, hashes, and timing. Legacy quality override behavior is preserved.

## Request and reproducible result

- An `analysis-model-request-v1` includes a model ID and version, requested date range, typed parameters, a quality policy, and one SHA-256 pin per ordered source identity. A definition validates the source count, exact source identity, policy, and registered Pydantic parameter schema. Parameter defaults resolve before evidence is generated. Strict wire decoding uses Pydantic's `model_validate_json`.
- An `analysis-model-evidence-v1` records model ID, version and implementation revision, calculation version, inputs and resolved parameters, requested dates, and ordered observed sessions. Each source retains its own full identity, selected-row digest, observed window, total count, aligned count, excluded count, and quality status. The aligned count matches the result session count. Observed endpoints outside the aligned sessions consume excluded slots. Source order is significant, including when it expresses a baseline or comparison role.
- `require_complete` requires verified evidence for each source. `allow_unverified` requires the explicit `quality_override` warning even when every source reports verified. Nonverified sources carry their own warning codes. This is the new evidence rule. Each result is designated research-only.
- An `analysis-model-result-v1` combines evidence, dated output rows, a UTC evaluation timestamp, and two SHA-256 digests. The reproducibility hash covers the evidence, including output schema and units. The output hash covers the ordered rows. The canonical encoding uses UTF-8 JSON with sorted object keys, compact separators, ASCII escapes, finite numeric values, and ISO dates. Decimal output is represented by fixed-point strings, with precision and trailing zeros determined by the model. Decimal fields can opt into strict positivity. SMA close and moving averages require positive values when present, and the moving averages remain nullable during warm-up. A reason-code field declares its allowed values and uses an ordered JSON array of unique codes. The evaluation timestamp is excluded from both hashes.
- `AnalysisResult.from_evaluation` builds checked hashes. `ModelDefinition.validate_result` checks the envelope and binds it to the request pins, resolved parameters, model revision, declared output fields, and observation bounds. Hash validation establishes payload consistency. Source data and quality currency still require independent checks against local storage.

## Registering later models

AL-02 and AL-03 can add static definitions to the existing [registry](../../backend/finintel/models/registry.py) using a frozen, strict parameter type, explicit source eligibility, observation bounds, output units and nullability, and versioned timing and quality rules. Use a parameter-dependent minimum rule when warm-up depends on configuration. The registry rejects duplicate ID/version pairs. An approved evaluator should reuse existing analytics primitives and produce evidence through the common contracts. Catalog registration alone grants no runtime dispatch.

AL-05 owns durable dispatch, snapshot rechecks and storage of the new envelopes. Existing worker runtime and result limits still apply, and the legacy reader continues to handle prior results. AL-07 owns any verified-backtest rules for common results. The hosted tests use synthetic evidence. Installed desktop and private-data acceptance remain separate.
