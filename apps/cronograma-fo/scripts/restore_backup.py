#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backup import (
    describe_backup_contents,
    extract_backup_database,
    is_backup_archive,
    restore_review_uploads_from_backup,
    validate_backup_schema,
)
from app.db import DEFAULT_DB_PATH, connect_db, init_db
from app.review_questions import REVIEW_QUESTION_UPLOADS_DIR, ensure_review_question_storage_dirs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restaura um backup .db/.zip do Cronograma FO.",
    )
    parser.add_argument(
        "--backup",
        required=True,
        help="Caminho do arquivo de backup (.db/.sqlite/.zip)",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="Caminho do banco principal que sera substituido",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    backup_path = Path(args.backup).expanduser().resolve()
    db_path = Path(args.db).expanduser().resolve()

    if not backup_path.exists():
        raise SystemExit(f"Backup nao encontrado: {backup_path}")

    inspection = validate_backup_schema(backup_path)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    safety_backup: Path | None = None
    uploads_safety_backup: Path | None = None
    if db_path.exists():
        safety_backup = db_path.with_name(f"{db_path.stem}.pre-restore-{timestamp}{db_path.suffix}")
        shutil.copy2(db_path, safety_backup)

    if is_backup_archive(backup_path):
        ensure_review_question_storage_dirs()
        if REVIEW_QUESTION_UPLOADS_DIR.exists():
            uploads_safety_backup = REVIEW_QUESTION_UPLOADS_DIR.parent / f"uploads.pre-restore-{timestamp}"
            if uploads_safety_backup.exists():
                shutil.rmtree(uploads_safety_backup)
            shutil.copytree(REVIEW_QUESTION_UPLOADS_DIR, uploads_safety_backup)
        shutil.rmtree(REVIEW_QUESTION_UPLOADS_DIR, ignore_errors=True)

    with extract_backup_database(backup_path) as extracted_db_path:
        shutil.copy2(extracted_db_path, db_path)

    with connect_db(db_path) as conn:
        init_db(conn)

    if is_backup_archive(backup_path):
        restore_review_uploads_from_backup(backup_path, REVIEW_QUESTION_UPLOADS_DIR)

    print(f"Restore concluido em: {db_path}")
    if safety_backup:
        print(f"Backup de seguranca do banco antigo: {safety_backup}")
    if uploads_safety_backup:
        print(f"Backup de seguranca dos uploads antigos: {uploads_safety_backup}")
    for line in describe_backup_contents(inspection):
        print(f"Backup restaurado {line}")


if __name__ == "__main__":
    main()
