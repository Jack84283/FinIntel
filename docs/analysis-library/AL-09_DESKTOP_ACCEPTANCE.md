# AL-09 installed desktop and recovery checklist

Run on macOS Apple Silicon, Windows and Linux. Record platform, build commit, OS, architecture, date, and PASS, FAIL, BLOCKED, NOT_RUN or NOT_APPLICABLE for each item. Use synthetic data and an isolated application profile with explicitly verified market and job-ledger paths. Keep installed private-data acceptance separate. Never restore over an owner's working profile or attach databases, exported reports, tokens or absolute private paths to hosted CI.

## Installed workflow

1. Install the packaged app, then start it from the launcher with all development terminals closed. Verify the bundled Python sidecar starts and binds only to loopback. Record safe status and process identity without copying tokens.
2. Disconnect external networking while retaining loopback. Open local history and verify previously retained research is still readable. With empty history, check the acquisition guidance without initiating a download. Check loading, unavailable-source and local-backend error states using the disposable profile.
3. Open Index Intelligence. Select, validate and explicitly queue a model. Inspect chart values against the corresponding observed-session table, including null warm-up fields, units, actual dates, source order, quality, and hashes. Save the job ID and hash in the private checklist.
4. Open Model Jobs and create a backtest from a verified, strict-policy completed SMA job. Select adjacent observed-session boundaries, review the assumptions, and queue it. Check durable progress, both cancellation paths, or eligible retry. Verify the completed report against its charts, exact result tables, assumptions and pinned source. Check that hypothetical fill dates follow the corresponding observed signal dates and that the benchmark uses the same price index.
5. Exercise keyboard navigation, visible focus, narrow layout, both themes and English/Simplified Chinese. Check that captions and tables convey the same information as charts.
6. Follow the export checklist below. Restart with queued and completed synthetic jobs. Confirm retained identities and hashes, safe interrupted-job recovery and no orphan workers after cancellation and shutdown. Record elapsed queue-to-result and retrieval time, and observed worker memory if available, along with the system and observation count. No release threshold is defined.

## Native export and live mount

1. With a completed synthetic report, open each JSON, equity CSV and hypothetical transaction CSV save flow. Verify the plaintext disclosure, native destination chooser and safe cancellation. A cancelled dialog leaves no file. Save on a known local volume, checking canonical values, hashes in JSON, and hypothetical transaction labels. Replace an existing synthetic file deliberately and check that failure preserves it and leaves no staged file.
2. With administrator-approved disposable shares, mount SMB and NFS where supported. Select a destination through the native dialog and confirm a safe error, no exported file and no staging residue. Use a fresh sentinel on the share to check that pre-existing bytes are preserved. Windows also requires UNC and mapped remote-drive coverage. Exercise a disposable FUSE mount on Linux when available. Respect the platform policy in [the export design](AL-08B_BACKTEST_VISUALIZATION_EXPORT.md).
3. Record local-volume and remote-volume results separately. If a mount type or platform is unavailable, record NOT_RUN or BLOCKED with the reason. A classifier unit test does not substitute for a native save attempt on a mounted share.

## Disposable backup and restore

1. Record the market schema version, source counts and fingerprints, relevant audit counts, ledger counts, and completed result hashes before starting. Use synthetic records only.
2. Create and inspect a market backup. Cancel through the UI and check that the destination is unchanged. Restore into an empty disposable profile and verify the restored schema, observations and audit counts.
3. During a replacement, confirm incompatible or corrupt backups are rejected. Verify the pre-restore recovery backup can restore the prior market state. Use the automated fault-injection test for an exception after destination write. Abrupt process termination during restore is a separate destructive rehearsal and requires a disposable profile.
4. The market backup does not include the separate model-job ledger. Verify the original ledger is unchanged during market restore. If rehearsing ledger recovery, stop the app and use a consistent standalone SQLite copy with verified schema and result hashes. Ledger recovery is not a feature of the market backup UI.
