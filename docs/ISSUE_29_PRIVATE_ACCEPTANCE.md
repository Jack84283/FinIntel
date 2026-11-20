# Issue #29 — Private database acceptance runbook

This runbook supplements the existing Issue #29 acceptance checklist. **No local
production tests have been claimed.** The database, credentials, provider payloads
and source prices must remain on the owner's machine. Do not commit real SQLite
files or the private evidence report to Git.

## 1. Obtain an owner-prepared detached baseline

Acceptance must not open the active production database. Obtain an existing,
checkpointed, offline backup through the owner's normal backup procedure **before**
starting this checklist. Do not copy a running SQLite file or ignore WAL writes.
Set `FININTEL_ACCEPTANCE_BASELINE` to that detached backup's absolute path; this is
not `FININTEL_DB_PATH`. Do not launch the application against the baseline.

The tools reject configured/default development and standard installed-app data
paths, symlinks, hard links and SQLite sidecars. These checks cannot discover every
custom active database path or prove that another process is not using a file.
The owner must confirm that the supplied file is detached and remains unused.

After installing the backend development dependencies, run from the repository
root (use a fresh output directory for each run):

    mkdir -p artifacts/issue29-task1
    python -m pytest backend/tests/test_quality.py backend/tests/test_calendar_quality.py \
      backend/tests/test_reconciliation.py backend/tests/test_corrections.py \
      backend/tests/test_database_backup.py backend/tests/test_quality_final_acceptance.py \
      backend/tests/test_issue29_private_evidence.py -q
    python scripts/quality_acceptance.py --output artifacts/issue29-task1/synthetic.json

These commands use disposable synthetic databases only; they do not use the
private baseline. They are executable regression checks, not real-data acceptance.

For private inspection, replace the example path below with the detached baseline
or use `--inspect-db "$FININTEL_ACCEPTANCE_BASELINE"`. Never supply the active DB.
Create a **different disposable working copy** for each scan, migration, and
restore scenario. Keep the baseline unchanged. To copy it safely to a new working
file, with `FININTEL_ACCEPTANCE_BASELINE` set:

    python - <<'PY'
    import os, shutil
    from pathlib import Path
    from scripts.issue29_private_evidence import _detached_copy
    source = _detached_copy(Path(os.environ["FININTEL_ACCEPTANCE_BASELINE"]).expanduser())
    target = Path("artifacts/issue29-task1/working.sqlite3").resolve()
    if target == source or any(Path(str(target) + s).exists() for s in ("", "-wal", "-shm", "-journal")):
        raise SystemExit("Choose a new disposable destination.")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
        shutil.copyfileobj(src, dst)
    PY

If the baseline is schema v14, inspect it without migration first. A nonzero
schema result is expected. Migrate **only the disposable working copy** using
FinIntel's built-in migration; compare before/after integrity, counts and digests.
Never manipulate `PRAGMA user_version` directly. The current supported schema is
v15; a newer unknown version is unsupported, not implicitly compatible.

## 2. Gather local numeric and integrity evidence

Run on a fresh, detached SQLite backup copy with no WAL, SHM or journal sidecars:

    python scripts/issue29_private_evidence.py \
      --inspect-db "$FININTEL_ACCEPTANCE_BASELINE" \
      --output artifacts/issue29-task1/baseline-private.json \
      --quick-check --foreign-key-check --source-digests

The script uses read-only SQLite immutable mode, refuses known active DB paths,
rejects symlinks/sidecars and existing output files, and does not perform scans,
migrations, restores, provider requests or source updates. Outputs are created with
private permissions where supported. Exit 1 means a schema/version mismatch, missing core
table, failed or incomplete mandatory bounded query, failed requested integrity
check, or unavailable/failed requested source digest. All mandatory bounded queries
must report `measured`; `failed`, `not_run`, missing or unknown query statuses
are unsuccessful. Optional quick-check, foreign-key-check and source digests
remain opt-in and do not fail solely because they were not requested.
The report is retained for investigation; exit 0 means all mandatory bounded
queries completed and no requested checks failed, not production sign-off. `version_matches_structure_unverified` explicitly does not
certify the complete schema. Run `quality_acceptance.py --inspect-db` on the same
detached copy for the separate required-column compatibility probe. Review the
JSON **locally** before sharing.
Even hashes, provider counts and timings are private metadata.

    python scripts/quality_acceptance.py \
      --inspect-db "$FININTEL_ACCEPTANCE_BASELINE" \
      --output artifacts/issue29-task1/baseline-schema.json

A legacy/incomplete schema must remain a failed local inspection while the
independent synthetic section remains passed. No production acceptance is implied.

The JSON records schema version, selected table counts, provider family counts,
bounded repeat timings for SP500 index/finding and one equity series, SQLite
quick_check and foreign_key_check (when opted in), and approximate process CPU/RSS.
Optional --source-digests streams original OHLCV/index rows in stable key order
into SHA-256 hashes **without exporting observations**. On 18M+ rows, these checks
may take considerable time. Use --repeat (1–10) and --sample (1–500) to control
bounded timings. These queries do NOT measure a full/resumed scan, UI responsiveness,
OS-wide peak memory or peak disk use. Production acceptance is always unverified.

## 3. Execute one scan batch on the disposable working copy

The following command **writes findings/checkpoints and may migrate the working
copy**. It must use the disposable file created in step 1. It refuses a missing
file and known active paths. It never targets the detached baseline. Choose and
record a fixed `as_of` date for the whole experiment (the example uses this
review's date). Repeat in a fresh process until `complete` is true; record each
batch's elapsed time and status. Use the OS monitoring described below for peaks.

    python - <<'PY'
    import time
    from datetime import date
    from pathlib import Path
    from scripts.issue29_private_evidence import _detached_copy
    from finintel.market_data.quality import run_quality_scan
    path = _detached_copy(Path("artifacts/issue29-task1/working.sqlite3"))
    started = time.perf_counter()
    result = run_quality_scan(path, max_findings=1000, as_of=date(2026, 11, 19))
    keys = ("status", "complete", "resumed", "cursor_start", "cursor_next", "findings_stored")
    print({key: result[key] for key in keys})
    print({"batch_wall_seconds": round(time.perf_counter() - started, 3)})
    PY

`complete=false`, `partial` or `stale` must not be signed off as a complete scan.
Test interruption separately on another working copy; preserve baseline/final
private reports under new filenames. The automated subprocess regression models
an abrupt exit between finding persistence and checkpoint persistence, but does
not replace installed-desktop interruption or all possible power-loss scenarios.

## 4. Remaining manual acceptance (only on other disposable copies)

1. Capture baseline source digests and counts. Keep that detached baseline untouched.
   Capture provider/ingestion provenance and finding/review/corporate-action/correction/
   audit identities separately: source-price digests do not cover those records.
2. On a separate working copy, run a full index-first quality scan, interrupt and
   restart the desktop, resume to complete, confirm findings/review IDs, audit
   histories, coverage and partial/stale/complete status. Measure actual elapsed
   time, peak CPU/RAM/disk and the tested hardware/build with OS monitoring. Use
   macOS Activity Monitor or `/usr/bin/time -l`, Linux `/usr/bin/time -v`, or
   Windows Performance Monitor. Keep one fixed validation reference date across
   restart/resume. Stop after a committed partial batch and relaunch; also test
   interruption during a batch. Record the last durable cursor, final completion,
   stable finding/review IDs, and comparison with a full scan on a separate copy.
   Process-only inspection metrics are not full-scan or whole-machine peaks.
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

## 5. Document and sign off

Copy docs/ISSUE_29_SIGNOFF_TEMPLATE.md into a **private** working note and record
the exact merged SHA, all five CI results, tested builds, device/OS, dataset
coverage, integrity and timing evidence, observed defects, approved waivers and
accountable sign-off. Only a separately reviewed, redacted summary should be
attached to GitHub Issue #29. Keep Issue #29 open until every mandatory gate
passes or is explicitly waived by an authorized risk owner.
