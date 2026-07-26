#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "apps" / "anki-gpt" / "addon-local"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "anki-gpt-addon.zip")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ADDON.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            archive.write(path, path.relative_to(ADDON))
    with zipfile.ZipFile(args.output) as archive:
        names = set(archive.namelist())
        required = {"__init__.py", "organization.py", "html_utils.py"}
        missing = required - names
        if missing:
            raise SystemExit(f"addon_bundle_missing: {sorted(missing)!r}")
        if any(name.startswith("/") or ".." in Path(name).parts for name in names):
            raise SystemExit("addon_bundle_unsafe_path")
    print(f"ADDON_BUNDLE {args.output} files={len(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
