#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.dont_write_bytecode = True

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import DatabaseInspection, initialize_or_migrate_database, inspect_database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cria ou migra explicitamente o banco do Cronograma FO.",
    )
    parser.add_argument("--db", required=True, help="Caminho do banco SQLite")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Somente diagnostica; nao altera nada")
    mode.add_argument("--apply", action="store_true", help="Aplica explicitamente as migracoes")
    return parser


def storage_paths(db_path: Path) -> tuple[Path, Path]:
    legacy = PROJECT_ROOT / "state" / "federal_online" / "review_questions" / "uploads"
    target = db_path.parent / "review_questions" / "uploads"
    return legacy.resolve(), target.resolve()


def pending_legacy_uploads(legacy_dir: Path, target_dir: Path) -> list[Path]:
    if not legacy_dir.is_dir():
        return []
    return [
        source
        for source in sorted(legacy_dir.rglob("*"))
        if source.is_file() and not (target_dir / source.relative_to(legacy_dir)).exists()
    ]


def apply_storage_migration(
    legacy_dir: Path,
    target_dir: Path,
    pending_files: list[Path],
) -> int:
    target_dir.mkdir(parents=True, exist_ok=True)
    for source in pending_files:
        destination = target_dir / source.relative_to(legacy_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return len(pending_files)


def print_inspection(label: str, inspection: DatabaseInspection) -> None:
    print(f"{label}_db={inspection.db_path}")
    print(f"{label}_exists={inspection.exists}")
    print(f"{label}_runtime_compatible={inspection.is_runtime_compatible}")
    print(f"{label}_migration_actions={len(inspection.actions)}")
    for action in inspection.actions:
        print(f"{label}_would={action}")
    print(f"{label}_problems={len(inspection.problems)}")
    for problem in inspection.problems:
        print(f"{label}_problem={problem}")


def main() -> int:
    args = build_parser().parse_args()
    db_path = Path(args.db).expanduser().resolve()
    legacy_dir, target_dir = storage_paths(db_path)
    pending_files = pending_legacy_uploads(legacy_dir, target_dir)
    before = inspect_database(db_path)

    print(f"mode={'check' if args.check else 'apply'}")
    print_inspection("before", before)
    print(f"before_upload_dir_exists={target_dir.is_dir()}")
    print(f"before_legacy_uploads_pending={len(pending_files)}")
    if not target_dir.is_dir():
        print(f"before_would=criar diretorio de uploads {target_dir}")
    if pending_files:
        print(f"before_would=recuperar {len(pending_files)} upload(s) legado(s)")

    if args.check:
        migration_needed = (
            before.needs_migration or not before.is_runtime_compatible
            or not target_dir.is_dir() or bool(pending_files)
        )
        print(f"check_result={'migration_required' if migration_needed else 'compatible'}")
        return 2 if migration_needed else 0

    _, after = initialize_or_migrate_database(db_path)
    if not after.is_runtime_compatible:
        print_inspection("after", after)
        print("apply_result=failed")
        return 1

    copied_count = apply_storage_migration(legacy_dir, target_dir, pending_files)
    final = inspect_database(db_path)
    print_inspection("after", final)
    print(f"after_legacy_uploads_copied={copied_count}")
    print("apply_result=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
