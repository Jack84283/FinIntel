"""Preserve ignored FinIntel data outside the checkout before Git operations.

The default destination is a timestamped sibling of the repository. The tool
copies local files reported by Git, including tracked files below data/ from
older revisions, so data is protected before cleanup removes it from tracking.
Tracked public fixtures such as data/indexes/*.csv are not duplicated. The
active SQLite database is copied through SQLite's backup API to include
committed WAL data safely.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

MANIFEST_NAME = "finintel-local-data-manifest.json"
CHUNK_SIZE = 1024 * 1024
PUBLIC_FIXTURES = Path("data/indexes")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_local_artifacts(root: Path) -> list[Path]:
    """Return local files below data/, excluding tracked public fixtures."""
    commands = (
        [
            "git",
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            "--",
            "data",
        ],
        ["git", "ls-files", "--cached", "-z", "--", "data"],
    )
    paths: set[Path] = set()
    for command in commands:
        result = subprocess.run(
            command,
            cwd=root,
            check=True,
            capture_output=True,
        )
        tracked = "--cached" in command
        for raw in result.stdout.split(b"\0"):
            if not raw:
                continue
            relative = Path(raw.decode("utf-8"))
            if tracked and PUBLIC_FIXTURES in relative.parents:
                continue
            paths.add(relative)
    artifacts = []
    for relative in paths:
        source = root / relative
        if source.is_symlink():
            raise RuntimeError(f"Refusing to preserve symlink: {relative}")
        candidate = source.resolve()
        if not candidate.is_file():
            continue
        artifacts.append(relative)
    return sorted(artifacts)


def _outside_checkout(root: Path, destination: Path) -> Path:
    resolved_root = root.resolve()
    resolved_destination = destination.expanduser().resolve()
    if resolved_destination == resolved_root or resolved_destination.is_relative_to(
        resolved_root
    ):
        raise ValueError(
            "Preservation destination must be outside the repository checkout"
        )
    return resolved_destination


def _copy_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        sqlite3.connect(
            source.resolve().as_uri() + "?mode=ro", uri=True, timeout=30
        ) as incoming,
        sqlite3.connect(destination, timeout=30) as output,
    ):
        incoming.backup(output, pages=1000, sleep=0.05)


def preserve(root: Path, destination: Path, *, dry_run: bool = False) -> Path:
    root = root.resolve()
    destination = _outside_checkout(root, destination)
    artifacts = discover_local_artifacts(root)
    if dry_run:
        for relative in artifacts:
            print(relative)
        return destination / MANIFEST_NAME
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"Preservation destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=False)
    entries = []
    for relative in artifacts:
        source = root / relative
        target = destination / relative
        if relative == Path("data/finintel.sqlite3"):
            _copy_sqlite(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        entries.append(
            {
                "path": relative.as_posix(),
                "size": target.stat().st_size,
                "sha256": sha256(target),
            }
        )
    manifest = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "repository": str(root),
        "files": entries,
    }
    manifest_path = destination / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Preserved {len(entries)} local files in {destination}")
    print(f"Manifest: {manifest_path}")
    return manifest_path


def _read_manifest(manifest_path: Path) -> tuple[Path, dict]:
    manifest_path = manifest_path.expanduser().resolve()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("files"), list):
        raise ValueError("Unsupported or malformed preservation manifest")
    return manifest_path.parent, data


def verify(manifest_path: Path) -> None:
    directory, manifest = _read_manifest(manifest_path)
    for entry in manifest["files"]:
        source = directory / entry["path"]
        if source.is_symlink():
            raise RuntimeError(f"Preserved file is a symlink: {entry['path']}")
        candidate = source.resolve()
        if not candidate.is_relative_to(directory) or not candidate.is_file():
            raise RuntimeError(f"Preserved file is missing: {entry['path']}")
        if (
            candidate.stat().st_size != entry["size"]
            or sha256(candidate) != entry["sha256"]
        ):
            raise RuntimeError(f"Preserved file failed verification: {entry['path']}")
    print(f"Verified {len(manifest['files'])} preserved files")


def restore(manifest_path: Path, root: Path) -> None:
    directory, manifest = _read_manifest(manifest_path)
    root = root.resolve()
    for entry in manifest["files"]:
        source_path = directory / entry["path"]
        target_path = root / entry["path"]
        if source_path.is_symlink() or target_path.is_symlink():
            raise RuntimeError(f"Refusing to restore symlink: {entry['path']}")
        source = source_path.resolve()
        target = target_path.resolve()
        if not source.is_file() or not source.is_relative_to(directory):
            raise RuntimeError(f"Preserved file is missing: {entry['path']}")
        if not target.is_relative_to(root):
            raise RuntimeError(f"Unsafe restore path: {entry['path']}")
        if target.exists():
            if (
                target.is_file()
                and target.stat().st_size == entry["size"]
                and sha256(target) == entry["sha256"]
            ):
                continue
            raise FileExistsError(f"Refusing to overwrite existing file: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    print(f"Restored {len(manifest['files'])} preserved files into {root}")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--verify", type=Path, metavar="MANIFEST")
    parser.add_argument("--restore", type=Path, metavar="MANIFEST")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    actions = sum(value is not None for value in (args.verify, args.restore))
    if actions > 1 or (actions and (args.destination or args.dry_run)):
        parser.error("choose one of preserve, --verify, or --restore")
    if args.verify:
        verify(args.verify)
        return
    if args.restore:
        restore(args.restore, root)
        return
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = (
        args.destination or root.parent / f"{root.name}-local-preservation-{stamp}"
    )
    preserve(root, destination, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
