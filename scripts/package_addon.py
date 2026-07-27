#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "apps" / "anki-gpt" / "addon-local"
CANONICAL_NOTE_PRECONDITIONS = (
    ROOT / "packages" / "anki-contracts" / "note_preconditions.py"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "anki-gpt-addon.zip")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ADDON.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            relative = path.relative_to(ADDON)
            source = (
                CANONICAL_NOTE_PRECONDITIONS
                if relative == Path("note_preconditions.py")
                else path
            )
            archive.write(source, relative)
    with zipfile.ZipFile(args.output) as archive:
        names = set(archive.namelist())
        required = {
            "__init__.py",
            "organization.py",
            "html_utils.py",
            "note_preconditions.py",
        }
        missing = required - names
        if missing:
            raise SystemExit(f"addon_bundle_missing: {sorted(missing)!r}")
        if any(name.startswith("/") or ".." in Path(name).parts for name in names):
            raise SystemExit("addon_bundle_unsafe_path")
        bundled_contract = archive.read("note_preconditions.py")
        if bundled_contract != CANONICAL_NOTE_PRECONDITIONS.read_bytes():
            raise SystemExit("addon_bundle_note_preconditions_diverged")
    print(f"ADDON_BUNDLE {args.output} files={len(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
