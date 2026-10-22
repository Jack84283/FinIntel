# FinIntel DATA-PR2 — Daily Historical Equity Provider

## Chosen PR #2 provider

**Alpha Vantage** (official API) / TIME_SERIES_DAILY / compact / JSON.
- Documentation: https://www.alphavantage.co/documentation/#daily
- Terms: https://www.alphavantage.co/terms_of_service/
- API key registration: https://www.alphavantage.co/support/#api-key
- Market data policy: https://www.alphavantage.co/realtime_data_policy/

The publicly posted Alpha Vantage terms describe a personal non-commercial license and
classify usage beyond private individual investment analysis, research, testing and
monitoring, or use on behalf of certain businesses/institutions, as commercial.
Users must check their own entitlements and current restrictions; no redistribution,
resale or downstream publishing of provider data is authorized by this project.
Do not commit provider responses or customer financial data to the public GitHub repo.

## Price semantics and limitations

- **Instrument**: symbol returned by provider metadata must exactly match the requested
  normalized symbol. Start with AAPL, MSFT, NVDA and SPY for manual validation.
- **Interval**: daily, as-traded **unadjusted** OHLCV. No inference of adjusted closes,
  splits, dividends, currency, exchange or intraday/realtime freshness.
- **Free access**: compact responses contain at most 100 latest trading sessions;
  full history uses the provider's premium entitlement. Provider terms and quotas
  can change without notice.
- **Daily limit**: the free access tier is limited (commonly described as 25
  requests/day). No automatic bulk refresh is provided. The user is responsible for
  controlling total requests across invocations and installations.
- **Provenance**: symbol, provider, sanitized source URL (no API key), terms URL,
  last refreshed text, UTC fetch timestamp, historical/adjustment labels, earliest
  and latest dates from validated observations are available. Local JSON snapshots
  are not adjusted, merged or normalized across corporate actions.
- **Source safety**: use only the official HTTPS API. Do not scrape websites.
- **Key management**: read FININTEL_ALPHA_VANTAGE_API_KEY from the shell environment;
  do not add it to source, config, fixtures, URLs displayed to users, or logs.

## Validation and failure rules

Every response must parse as unambiguous JSON with a matching symbol, nonempty
daily series, unique well-formed session dates, positive finite decimal OHLC
prices (low <= open/close <= high), and nonnegative integral volume.
Corrupt or semantically invalid data aborts the entire import.
Provider quota notifications (HTTP 429 or HTTP 200 JSON containing Note/
Information rate-limit text) abort immediately without automatic retry.
Transient network/HTTP 5xx errors retry at most twice with short exponential
delays. Credential-bearing request URLs are never included in user-visible errors.
An atomic local replacement happens **only after complete validation**; a failed
fetch must leave the existing last-known-good snapshot untouched.

## Live smoke check — manual, optional

1. Activate the existing project-specific Python venv and install the backend.
2. Obtain and approve use of your own Alpha Vantage API key; export it only in
   your local shell.
3. Run: python -m finintel.market_data --symbol AAPL
4. Inspect data/raw/AAPL.json and confirm provider, raw series label, dates,
   100-or-fewer observations on compact, and source URL without API key.
5. Repeat for MSFT, NVDA or SPY only within your plan's quota.
6. Simulate a quota/outage (or use offline tests); confirm a prior validated
   snapshot remains unchanged.

The synthetic IBM fixture in backend/tests/fixtures exercises the API's JSON
shape but contains **invented test values**, not real historical market observations.

## Separation from future releases

PR #2: provider adapter, validation, CLI and local staging JSON.
PR #3: versioned SQLite migrations, ingestion history, idempotence and data APIs.
PR #4: charts and grids driven by persisted data.
PR #5: signed native Python sidecar and lifecycle/security integration.
M1.5: authenticated HTTPS gateway for the cross-device tablet PWA.

This document does not claim any third-party dataset is public domain or freely
redistributable. Provider plans, data policy and licensing require periodic review.

## Integrated UI workflow (GitHub PR #6)

In **Markets → Data acquisition**, enter a ticker and explicitly click
**Fetch & store**. The backend alone reads its locally configured
`FININTEL_ALPHA_VANTAGE_API_KEY`. One validated Alpha Vantage batch is
staged briefly in a private temporary directory and then imported using
the existing transaction-safe SQLite importer; the temporary JSON is
deleted. The UI reloads the catalog, OHLCV chart, table and ingestion
history on success. Repeating an explicit import is idempotent at the price
row level, though each click consumes a provider request and creates a
new audit entry. The card does not poll or fetch on mount.

A full-history request is allowed in the UI only as a user-selected option
and requires the appropriate provider subscription; local software cannot
infer the account's entitlement from an API-key-presence check. Free access
is quota-limited, and the UI should not bulk-refresh symbols automatically.

The loopback development POST uses a strict JSON contract and workstation
header plus Origin checks. These controls are not strong local-client
authentication; never expose the dev Python API outside loopback. The
future secure gateway must provide authentication and authorization.

