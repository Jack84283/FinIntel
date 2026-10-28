# Local data preservation before Git operations

FinIntel keeps private databases, SQLite backups, provider snapshots, logs and
backfill checkpoints outside Git. `.gitignore` prevents new artifacts from
being committed, but it cannot protect a file while switching between commits
where that file was previously tracked. The preservation tool includes both
ignored/untracked files and tracked files below `data/`, while excluding the
tracked public index fixtures. Preserve local artifacts before switching
branches, pulling, merging, rebasing, or applying a cleanup commit.

From the repository root:

```bash
# Review what Git considers local/ignored data.
git status --short --ignored -- data/

# Preview the files that will be copied outside the checkout.
python scripts/preserve_local_data.py --dry-run

# Copy them to a timestamped sibling directory, using SQLite's backup API
# for the active database. Choose another secure destination if preferred.
python scripts/preserve_local_data.py \
  --destination "../FinIntel-local-preservation-$(date -u +%Y%m%dT%H%M%SZ)"
```

The command prints a manifest path. Verify it before changing Git state:

```bash
python scripts/preserve_local_data.py --verify \
  ../FinIntel-local-preservation-YYYYMMDDTHHMMSSZ/finintel-local-data-manifest.json
```

The preservation destination must be outside the checkout and is never
overwritten by the script. Restore only missing files after the branch
operation; existing files with different content are refused rather than
silently overwritten:

```bash
python scripts/preserve_local_data.py --restore \
  ../FinIntel-local-preservation-YYYYMMDDTHHMMSSZ/finintel-local-data-manifest.json
```

Fresh checkouts create runtime directories on demand. The database creates its
parent directory, backup/restore creates `backups/`, and the backfill
controllers create `data/` before writing logs, PIDs or checkpoints. Do not
use `git clean -fdx` in a checkout containing local financial data unless the
preservation manifest has been verified and the data is intentionally being
removed.
