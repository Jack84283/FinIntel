# Issue #29 — Private database acceptance runbook

This runbook supplements the existing Issue #29 acceptance checklist. **No local
production tests have been claimed.** The database, credentials, provider payloads
and source prices must remain on the owner's machine. Do not commit real SQLite
files or the private evidence report to Git.

## 1. Confirm the actual database and make a detached snapshot

From the FinIntel repository root, check the configured location first:

    python - <<'PY'
    import os, sqlite3
    from pathlib import Path
    source = Path(os.environ.get("FININTEL_DB_PATH", "data/finintel.sqlite3")).expanduser().resolve()
    if not source.is_file():
        raise SystemExit("Database not found here. Check FININTEL_DB_PATH in your backend environment.")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
        print("Schema version:", db.execute("PRAGMA user_version").fetchone()[0])
    print("Database:", source)
    PY

If correct, create a new snapshot using SQLite's backup API (includes committed
WAL observations, unlike a raw filesystem copy). Check free disk space first.
The command deliberately refuses to overwrite an existing snapshot:

    python - <<'PY'
    import os, sqlite3
    from pathlib import Path
    source = Path(os.environ.get("FININTEL_DB_PATH", "data/finintel.sqlite3")).expanduser().resolve()
    target = Path("artifacts/issue29-detached-copy.sqlite3").resolve()
    if not source.is_file() or source.is_symlink() or target == source:
        raise SystemExit("Invalid source or destination.")
    if target.exists() or any(Path(str(target) + s).exists() for s in ("-wal", "-shm", "-journal")):
        raise SystemExit("Destination already exists; never overwrite a database copy.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst, pages=1000, sleep=0.05)
    target.chmod(0o600)
    print("Detached copy:", target)
    PY

Do not migrate the original production database as part of acceptance. If a
detached copy is schema v14, create **another disposable copy**, run FinIntel's
built-in v14-to-v15 migration on that copy, and compare integrity, row counts and
source-row digests. Never manipulate PRAGMA user_version directly.

## 2. Gather local numeric and integrity evidence

Run on a fresh, detached SQLite backup copy with no WAL, SHM or journal sidecars:

    python scripts/issue29_private_evidence.py \
      --inspect-db artifacts/issue29-detached-copy.sqlite3 \
      --output artifacts/issue29-private-evidence.json \
      --quick-check --foreign-key-check --source-digests

The script uses read-only SQLite immutable mode, refuses obvious active DB paths,
rejects symlinks/sidecars and existing output files, and does not perform scans,
migrations, restores, provider requests or source updates. Outputs are created with
private permissions where supported. Review the JSON **locally** before sharing.
Even hashes, provider counts and timings are private metadata.

The JSON records schema version, selected table counts, provider family counts,
bounded repeat timings for SP500 index/finding and one equity series, SQLite
quick_check and foreign_key_check (when opted in), and approximate process CPU/RSS.
Optional --source-digests streams original OHLCV/index rows in stable key order
into SHA-256 hashes **without exporting observations**. On 18M+ rows, these checks
may take considerable time. Use --repeat (1–10) and --sample (1–500) to control
bounded timings. These queries do NOT measure a full/resumed scan, UI responsiveness,
OS-wide peak memory or peak disk use. Production acceptance is always unverified.

## 3. Remaining manual acceptance (only on other disposable copies)

1. Capture baseline source digests and counts. Keep that detached baseline untouched.
2. On a separate working copy, run a full index-first quality scan, interrupt and
   restart the desktop, resume to complete, confirm findings/review IDs, audit
   histories, coverage and partial/stale/complete status. Measure actual elapsed
   time, peak CPU/RAM/disk and the tested hardware/build with OS monitoring.
3. Re-run the private-evidence script against the scan-tested copy using a NEW
   output filename. Compare daily_prices and index_daily_closes counts and hashes
   against baseline; findings may change but original observations must not.
4. On yet another copy, test manual backup, v14 migration, merge/replace restore,
   rollback/restart, foreign keys, and preservation of correction evidence and
   review/audit history. Never restore into your primary production DB for testing.
5. On installed desktop builds test macOS cold start, SP500/index/equity navigation,
   evidence drills, correction preview/approve/reverse, offline, EN/zh-CN, dark/light,
   and accessibility. Test Windows/Linux native launches or explicitly document
   risk-owner-approved exceptions. A green packaging job alone is not manual QA.
6. With authorized provider entitlements and disposable storage, verify Tiingo,
   Alpaca, Alpha Vantage and FRED source coverage/provenance, raw versus adjusted
   returns, price versus total-return indexes, corporate actions and revisions.
   Absent entitlements are blocked/not_run, not pass.

## 4. Document and sign off

Copy docs/ISSUE_29_SIGNOFF_TEMPLATE.md into a **private** working note and record
the exact merged SHA, all five CI results, tested builds, device/OS, dataset
coverage, integrity and timing evidence, observed defects, approved waivers and
accountable sign-off. Only a separately reviewed, redacted summary should be
attached to GitHub Issue #29. Keep Issue #29 open until every mandatory gate
passes or is explicitly waived by an authorized risk owner.
