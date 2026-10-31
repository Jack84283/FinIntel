# FinIntel — Dated Index Constituent Directory

This capability distinguishes **membership in an equity index** from asset type,
exchange listing and market-data provider. A stock can belong to multiple indexes
at once; an index-tracking ETF (such as SPY, QQQ or IWM) is **not** a constituent
of the index merely because it tracks that benchmark.

## Supported index identifiers

| Code | Index |
| --- | --- |
| SP500 | S&P 500 |
| DJIA | Dow Jones Industrial Average |
| NDX | Nasdaq-100 |
| RUT | Russell 2000 |

No membership records are fabricated or bundled in the public repository.
FinIntel only displays constituents after a dated snapshot from an
**independently verified and authorized** source is imported on the local workstation.

## Equibles API acquisition (experimental)

Configure `FININTEL_EQUIBLES_API_KEY` in the environment of the Python backend and
restart that backend. Keep the secret out of Git, frontend code, and screenshots.
In **Data & Operations → Index Constituents**, select one of the four indexes
and choose **Fetch constituents**. Alternatively, use the local POST endpoint:

```bash
curl -f -X POST http://127.0.0.1:8765/api/v1/market-data/indexes/SP500/acquire
```

This action explicitly downloads all pages from Equibles; it checks the date,
declared count, linked tickers and pagination before committing the snapshot.
The source is recorded as an **ETF holdings-derived proxy**, not an official
index constituent certification. Other imported dates and other indexes are
retained. API terms and local data storage permission remain subject to the
user's Equibles plan. The initial integration imports symbols, company names, and provenance;
weights/ranks/fund holdings fields are not yet preserved in the SQLite schema.

## Import a full point-in-time snapshot

Download or obtain a constituent export through a provider whose license permits
your private local use. Review its **effective/as-of date**, whether it is a full
constituent list, and whether it contains share-class symbols.

Create a local CSV with a `symbol` column. Example data below is **synthetic
format only**, not an actual 2026 S&P 500 constituent list:

```csv
symbol
AAPL
MSFT
```

Run, from the repository root in the installed backend virtual environment:

```bash
python -m finintel.market_data.membership \
  --index SP500 --as-of 2026-10-23 \
  --source 'authorized-vendor/export-2026-10-23' \
  --source-kind official_licensed --confidence high \
  --csv /private/sp500.csv

python -m finintel.market_data.membership \
  --index NDX --as-of 2026-10-23 \
  --source 'authorized-vendor/export-2026-10-23' \
  --csv /private/nasdaq100.csv

# Likewise: --index DJIA or --index RUT
```

Optional `--expected-count NUMBER` guards against a short/partial source export.
Use **the security count provided by that specific dated source**, never a
hard-coded assumption that SP500 must contain exactly 500 securities: multiple
share classes can mean a different securities count from the index name.
A valid import replaces only the **specified index and effective date**,
transactionally, while preserving other dates and indexes. Duplicate symbols,
invalid source identifiers, and known non-stock catalog entries are rejected.

The local index directory is available under **Data & Operations →
Index Constituents**. It supports index selection, ticker/company search,
paging, an as-of date, and membership badges that show a ticker's overlap
across different indexes. A stock-level lookup shows `Included`,
`Not in snapshot`, or `Unknown` separately for each index.

Every imported snapshot also records its source kind (`official_licensed`,
`verified_public`, or `fund_derived_proxy`), source as-of date, expected
constituent count, observed member count, validation status, availability, and
confidence. The UI keeps a fund-derived proxy visibly separate from an official
or verified-public list. Existing snapshots upgraded from older databases are
marked `unknown`/`unverified` rather than being presented as newly validated.

## Historical semantics and limitations

- Queries use the latest imported snapshot **on or before** the requested
  date. They never infer a historical member from a later snapshot.
- `Not in snapshot` is a statement about **the chosen local snapshot**, not
  a claim that an issuer has never participated in that index.
- Missing snapshots are reported as **unknown**, not zero constituents.
- There is no assumption that membership changes occur only on annual
  reconstitution dates. Import each new effective composition as required.
- An old snapshot can be stale relative to the user's requested date. Always
  show the actual source, snapshot date and requested date.
- Constituent classifications operate on **security tickers/share classes**,
  not issuer-level identities. Provider-specific ticker aliases and corporate
  actions need separate reconciliation before historical backtests.
- The full lists for licensed indexes, including Russell and S&P DJI, are
  not guaranteed to be freely redistributable. Do not commit licensed CSVs.
- Recheck provider rights before building any automatic sync. ETF holdings
  may be **an imperfect proxy**, not a verified index constituent export.
- Do not use these records as investable current recommendations or a
  survivorship-safe historical backtest without source/date validation.

Reference source/product pages (not APIs for automatic ingestion):
- S&P Dow Jones index constituent licensing:
  https://www.spglobal.com/spdji/en/about-us/data-index-licensing/
- S&P 500 index:
  https://www.spglobal.com/spdji/en/indices/equity/sp-500/
- Nasdaq-100 index:
  https://indexes.nasdaq.com/Index/Overview/NDX
- Russell index overview:
  https://www.lseg.com/en/ftse-russell/indices/russell-us

## REST API (development backend, read-only)

```text
GET /api/v1/market-data/indexes?as_of=2026-10-23
GET /api/v1/market-data/indexes/SP500/constituents?as_of=2026-10-23&q=APP&limit=50&offset=0
GET /api/v1/market-data/indexes/SP500/changes?from_as_of=2026-01-01&to_as_of=2026-10-23&q=APP&limit=50&offset=0
GET /api/v1/market-data/indexes/symbol/AAPL/memberships?as_of=2026-10-23
```

The changes endpoint compares the latest available snapshots on or before the
two requested dates and returns `added`/`removed` records. If either side has
no local snapshot, it returns `status: "unknown"` rather than claiming that no
changes occurred.

Both Tauri desktop and the Vite development client use the same typed
responses through their existing local transports. No external API calls
are made from the index membership UI.

## Schema and tests

SQLite schema v13 adds the `index_membership_snapshots` provenance table while
retaining the dated `index_memberships` rows. It supports SP500/DJIA/NDX/RUT
without deleting historical snapshots. The migration occurs on the first
**authorized local database write**
(e.g. a normal import); read operations themselves do not migrate the DB.
The migration backfills metadata from existing membership rows conservatively,
so legacy records remain unverified until their source completeness is known.

```bash
python -m pytest backend/tests/test_index_memberships.py backend/tests/test_batch_membership.py -q
ruff check backend
ruff format --check backend
npm run test
npm run typecheck
```

Next iteration: provider-backed authorized constituent acquisition, reliable
historical ticker mappings, automatic snapshot freshness checks, and
index-based initial historical price backfill.
