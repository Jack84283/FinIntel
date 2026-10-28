"""Build a native Python sidecar for this host and place it under Tauri externalBin."""

import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = ROOT / "apps" / "desktop" / "src-tauri" / "binaries"


def main() -> None:
    host = next(
        (
            line.partition(": ")[2]
            for line in subprocess.check_output(["rustc", "-vV"], text=True).splitlines()
            if line.startswith("host: ")
        ),
        None,
    )
    if not host:
        raise RuntimeError("Could not determine Rust target triple")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            "finintel-backend",
            "--collect-all",
            "finintel",
            "--collect-all",
            "exchange_calendars",
            "--collect-all",
            "keyring",
            str(ROOT / "backend" / "finintel" / "desktop_entry.py"),
        ],
        cwd=ROOT,
        check=True,
    )
    extension = ".exe" if platform.system() == "Windows" else ""
    source = ROOT / "dist" / ("finintel-backend" + extension)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = BIN_DIR / ("finintel-backend-" + host + extension)
    shutil.copy2(source, target)
    print("Bundled backend:", target)


if __name__ == "__main__":
    main()
