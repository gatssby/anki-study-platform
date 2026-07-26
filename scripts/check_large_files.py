#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 10 * 1024 * 1024


def main() -> int:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    oversized = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8"))
        path = ROOT / relative
        if path.is_file() and path.stat().st_size > MAX_BYTES:
            oversized.append(f"{relative}:{path.stat().st_size}")
    if oversized:
        raise SystemExit("Tracked files exceed 10 MiB:\n" + "\n".join(oversized))
    print("No tracked file exceeds 10 MiB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
