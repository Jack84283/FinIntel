# FinIntel — Local Ollama provider foundation (Phase 3A)

**Status:** Opt-in discovery-only foundation. No private research retrieval, AI-generated
explanations, chat UX or provider configuration persistence is implemented in this
phase. This must not be called a completed Issue #33 integration.

## Security and privacy contract

- `LocalOllamaConfig.enabled` defaults to `false`. Disabled means no request
  is made, even when a mock or live transport is supplied.
- Endpoint accepts only literal `http://127.0.0.1:<port>` or
  `http://[::1]:<port>`. DNS names, external/metadata hosts, TLS proxy
  endpoints, usernames/passwords, URL paths, queries and fragments are rejected.
  Use a local Ollama process; no external AI endpoint or cloud fallback.
- Explicit opt-in permits one read-only `GET /api/tags` for local model
  discovery. No `/api/generate`, prompts, data retrieval, model downloads or
  arbitrary API paths are implemented. Redirects and inherited proxy variables
  are disabled through HTTPX.
- Discovery responses are streamed and capped at 64 KiB, with at most 100 model
  records and strict model name validation. Host errors and malformed payloads
  return generic errors; no raw provider response or private filesystem path
  is included in errors.
- Models must be explicitly selected in later phases. Discovery never runs
  or loads an arbitrary model and never accesses local market tables or jobs.
- No local storage schema changes, logs, credentials, provider keys, or private
  observations are committed by this PR.

## Automated evidence and pending work

Synthetic HTTPX mock transports test disabled mode, literal loopback enforcement,
GET-only requests, bounded response size, redirects, invalid model identifiers,
server errors and offline failure behavior. Python lint/tests and applicable
cross-platform CI must pass before this code can be reviewed for merge.

**Remaining Issue #33 implementation** (separate PRs): application opt-in settings
and managed endpoint authorization; constrained, redacted evidence retrieval
from locally validated index/history/model/backtest data; structured
source-backed interpretation and unsupported-number rejection; prompt injection
tests; bilingual desktop UI and locally retained/reversible research history;
installed desktop offline/connection QA. No integration may silently send
private data outside the user-controlled local model process.
