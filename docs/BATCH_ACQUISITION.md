# Bulk historical acquisition and index membership

FinIntel supports a deliberately capped **manual batch run** from a local backend environment. It uses existing validated daily acquisition and SQLite persistence, selects only Stock and ETF rows missing a series for the selected provider, and never starts on application launch.

```sh
# From repo root; activate backend virtual environment first.
python -m finintel.market_data.batch --dry-run --max-requests 20
python -m finintel.market_data.batch --provider alpha_vantage --max-requests 20 --delay-seconds 15

# To run detached locally (not a ChatGPT hosted background task):
nohup python -m finintel.market_data.batch --max-requests 20 --delay-seconds 15 > data/batch.log 2>&1 &
```

The per-run cap is **not** a provider quota manager shared with other requests or processes. Alpha Vantage free keys allow only 25 API requests per day across the account; leave headroom for other operations and do not automatically rerun after a quota response. Failed symbols may be retried on a later invocation. Successful imports persist and are skipped on future runs. The detached process ends after its selected requests. It is not a recurring scheduler and does not continue automatically after computer shutdown. Keep only one batch process running and avoid simultaneous UI acquisition.

**Universe tags:** `symbol_catalog.asset_type` already distinguishes Stock, ETF, Index and Mutual Fund; an ETF tracking the S&P 500 is **not** itself an S&P 500 constituent. SQLite v6 stores dated `index_memberships` for `SP500`, `DJIA`, `NDX` and `RUT` with source references. No constituent dataset is shipped or guessed. Import a duly sourced/authorized CSV containing a `symbol` header (see [Index Membership](INDEX_MEMBERSHIP.md)):

```sh
python -m finintel.market_data.membership --index SP500 --as-of 2026-10-23 --source 'authorized-provider-2026-10-23' --csv /private/sp500.csv
python -m finintel.market_data.membership --index DJIA --as-of 2026-10-23 --source 'authorized-provider-2026-10-23' --csv /private/dow.csv
```

The importer **replaces one index/date snapshot**, not the entire history. Stock membership requires credible as-of constituent data; the symbol listing feed alone cannot establish this. Index Constituents UI shows sourced snapshot memberships; automated vendor constituent sync is a separate future milestone. Index symbols and mutual funds are not requested through the daily Stock/ETF adapter.

On Windows PowerShell, use `powershell -ExecutionPolicy Bypass -File scripts/backfill.ps1 start` for the equivalent process, log, and checkpoint controller. The worker uses native advisory checkpoint locking on macOS, Linux, and Windows.


## Long-running initial backfill (opt-in)

Use the persistent worker for an initial pass over **all eligible catalog symbols**, without a manually repeated per-run symbol cap. It runs in a separate local process and resumes from both the SQLite import records and an atomic daily checkpoint. The daily cap limits API consumption, **not** the total size of the collection universe.

```sh
# After installing the backend package in your virtual environment:
mkdir -p data
nohup python -m finintel.market_data.backfill_worker \
  --provider alpaca_sip --period 10y \
  --max-requests-per-day 100 --delay-seconds 2 \
  > data/backfill.log 2>&1 &
```

Choose a request cap and interval **appropriate to your actual provider plan**. The example is not an assertion of entitlement, and the Alpaca SIP adapter may require a subscription with SIP access. To use Alpha Vantage free instead, set `--provider alpha_vantage --max-requests-per-day 20`, bearing in mind that `compact` is only the latest 100 sessions.

The worker runs across days while the computer and local process stay running; it sleeps when a daily limit is reached, persists the attempted tickers, and retries unsuccessful symbols the following UTC day. A single-process file lock prevents a duplicate worker with the same state file. Re-launch the command after reboot to resume. No service, live download, automatic app-start execution, or desktop Start/Pause controls are included yet.

The worker skips any instrument/provider that already has a daily series; it **does not prove 10-year completeness or repair partial history**. A later coverage-audit stage must verify dates, listing age, and gaps before declaring backfill complete. This is a local opt-in job, not an automated ChatGPT task.

## Tiingo historical data

Tiingo is available as a third, independent raw daily OHLCV provider for Stock and ETF instruments. Its provider token stays in the local backend environment and is never sent to the React webview or saved in snapshots. Do not commit credentials.

From the FinIntel repo root, activate your virtualenv and export `FININTEL_TIINGO_API_KEY` into the backend process environment, then restart the backend service. You can explicitly select **Tiingo** in **Data Acquisition → Data source** and choose 5 years, 10 years, or maximum (requests from 1990 onward, subject to Tiingo coverage and plan). The Markets explorer lists Tiingo records as their own series.

For a Tiingo background backfill, use a **separate checkpoint** or stop and archive an existing Alpaca checkpoint before switching providers. The current controller fixes `data/finintel.backfill.json` to a single provider, and will refuse mismatched provider restarts.

```sh
FININTEL_BACKFILL_PROVIDER=tiingo \
FININTEL_BACKFILL_MAX_REQUESTS_PER_DAY=100 \
FININTEL_BACKFILL_DELAY_SECONDS=75 \
bash scripts/backfill.sh start
```

The numbers above are conservative examples, not a guarantee of access or quota entitlement. Provider quotas may include distinct monthly symbol limits, and FinIntel does **not** yet track such monthly quotas across applications. Historical prices are stored as unadjusted in provider-isolated SQLite series; adjusted Tiingo fields and corporate actions are not yet imported. Historical completeness is not yet guaranteed, and local database retention must comply with your Tiingo subscription.
