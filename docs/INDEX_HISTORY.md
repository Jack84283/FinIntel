# Source-backed index history

FinIntel stores index levels separately from equity OHLCV. The enabled FRED
series are `SP500`, `DJIA`, `NASDAQCOM`, `NDX`, and `VIX`; each row is a daily
close or volatility level in index points. No open, high, low, or volume values
are fabricated for these series.

## Capability and provenance

Schema v11 adds `index_series_catalog` for provider capability metadata and
`index_ingestion_runs` for append-only retrieval evidence. Schema v12 names
local coverage explicitly and adds nullable provider availability bounds. The
catalog reports official name, series type, frequency, units, source URL,
licensing note, provider-reported history limitation, provider availability
when actually known, and observed local coverage. Provider bounds remain null
when FRED has not been queried for authoritative availability metadata. Each
ingestion run records the requested date range, sanitized FRED request URL,
retrieval timestamp, first/last returned session, counts, and separate SHA-256
hashes for the raw provider payload and normalized observations. Programmatic
imports without the original payload set only the normalized observation hash.

The observation table remains keyed by `(index_code, provider, session_date)`.
An overlapping import inserts only missing sessions, updates a revised level,
and records unchanged rows without creating duplicates. Existing observations,
backups, and equity tables are preserved by the additive migration.

## Bounded backfill

Index acquisition is explicit and remains disabled during page load. The
desktop date fields map to FRED's bounded `cosd` and `coed` parameters. A blank
range requests the provider's full available window; a bounded request is
validated both before the HTTP call and against the returned rows. Future or
reversed dates, malformed content, duplicate sessions, and provider data
outside the requested range are rejected before storage.

The local API exposes the same range on `POST
/api/v1/market-data/index-history/{code}/acquire?start=YYYY-MM-DD&end=YYYY-MM-DD`
and supports bounded reads on the daily history endpoint. Packaged desktop
calls continue to pass only allowlisted index codes and ISO date filters to the
loopback backend.

FRED licensing and the provider's actual available history window still apply.
The catalog never claims a fixed ten-year limit or a date range that has not
been observed locally.
