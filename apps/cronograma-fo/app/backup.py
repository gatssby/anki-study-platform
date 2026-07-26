from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .review_questions import REVIEW_QUESTION_UPLOADS_DIR, ensure_review_question_storage_dirs


MINIMUM_REQUIRED_TABLES = {"lessons"}
TRACKED_OPTIONAL_TABLES = {
    "app_settings",
    "exercise_tasks",
    "schedule_settings",
    "schedule_unavailability",
    "review_questions",
    "review_question_attempts",
}

BACKUP_MANIFEST_NAME = "backup_manifest.json"
BACKUP_DB_ENTRY_NAME = "cronograma.db"
BACKUP_REVIEW_UPLOADS_PREFIX = "data/review_questions/uploads/"
LEGACY_BACKUP_REVIEW_UPLOADS_PREFIXES = (
    BACKUP_REVIEW_UPLOADS_PREFIX,
    "state/federal_online/review_questions/uploads/",
)
BACKUP_FORMAT_VERSION = 3


@dataclass(frozen=True)
class BackupInspection:
    path: Path
    kind: str
    tables: set[str]
    lesson_rows: int
    seen_lessons: int
    exercise_task_rows: int | None
    exercise_done_rows: int | None
    review_question_rows: int | None
    review_attempt_rows: int | None
    includes_review_uploads: bool
    review_upload_file_count: int

    @property
    def includes_exercise_tasks(self) -> bool:
        return "exercise_tasks" in self.tables

    @property
    def includes_schedule_tables(self) -> bool:
        return {"schedule_settings", "schedule_unavailability"}.issubset(self.tables)

    @property
    def includes_review_question_tables(self) -> bool:
        return {"review_questions", "review_question_attempts"}.issubset(self.tables)


def create_db_snapshot(
    db_path: str | Path,
    output_path: str | Path | None = None,
    prefix: str = "cronograma-backup-",
    suffix: str = ".db",
) -> Path:
    source_path = Path(db_path).expanduser().resolve()
    if output_path is None:
        temp_fd, temp_name = tempfile.mkstemp(prefix=prefix, suffix=suffix)
        os.close(temp_fd)
        destination_path = Path(temp_name)
    else:
        destination_path = Path(output_path).expanduser().resolve()
        destination_path.parent.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(destination_path)
    with destination:
        source.backup(destination)
    destination.close()
    source.close()
    return destination_path


def create_backup_archive(
    db_path: str | Path,
    output_path: str | Path | None = None,
    prefix: str = "cronograma-backup-",
    suffix: str = ".zip",
) -> Path:
    ensure_review_question_storage_dirs()
    if output_path is None:
        temp_fd, temp_name = tempfile.mkstemp(prefix=prefix, suffix=suffix)
        os.close(temp_fd)
        archive_path = Path(temp_name)
    else:
        archive_path = Path(output_path).expanduser().resolve()
        archive_path.parent.mkdir(parents=True, exist_ok=True)

    snapshot_path = create_db_snapshot(db_path=db_path)
    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot_path, BACKUP_DB_ENTRY_NAME)
            _write_review_uploads_to_archive(archive, REVIEW_QUESTION_UPLOADS_DIR)
            archive.writestr(
                BACKUP_MANIFEST_NAME,
                json.dumps(
                    {
                        "format": "cronograma-backup",
                        "version": BACKUP_FORMAT_VERSION,
                        "created_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
                        "db_entry": BACKUP_DB_ENTRY_NAME,
                        "review_uploads_prefix": BACKUP_REVIEW_UPLOADS_PREFIX,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
    finally:
        snapshot_path.unlink(missing_ok=True)
    return archive_path


def create_timestamped_backup(
    db_path: str | Path,
    backup_dir: str | Path,
    prefix: str = "cronograma-backup-",
) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_directory = Path(backup_dir).expanduser().resolve()
    backup_directory.mkdir(parents=True, exist_ok=True)
    destination_path = backup_directory / f"{prefix}{timestamp}.zip"
    return create_backup_archive(db_path=db_path, output_path=destination_path)


def inspect_backup(backup_path: str | Path) -> BackupInspection:
    resolved_path = Path(backup_path).expanduser().resolve()
    with extract_backup_database(resolved_path) as extracted_db_path:
        with sqlite3.connect(extracted_db_path) as conn:
            conn.row_factory = sqlite3.Row
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }

            lesson_rows = _scalar_count(conn, "SELECT COUNT(*) FROM lessons") if "lessons" in tables else 0
            seen_lessons = (
                _scalar_count(conn, "SELECT COUNT(*) FROM lessons WHERE is_seen = 1")
                if "lessons" in tables
                else 0
            )
            exercise_task_rows = (
                _scalar_count(conn, "SELECT COUNT(*) FROM exercise_tasks")
                if "exercise_tasks" in tables
                else None
            )
            exercise_done_rows = (
                _scalar_count(conn, "SELECT COUNT(*) FROM exercise_tasks WHERE status = 'done'")
                if "exercise_tasks" in tables
                else None
            )
            review_question_rows = (
                _scalar_count(conn, "SELECT COUNT(*) FROM review_questions")
                if "review_questions" in tables
                else None
            )
            review_attempt_rows = (
                _scalar_count(conn, "SELECT COUNT(*) FROM review_question_attempts")
                if "review_question_attempts" in tables
                else None
            )

    includes_review_uploads, review_upload_file_count = inspect_review_uploads(backup_path)
    return BackupInspection(
        path=resolved_path,
        kind="zip" if is_backup_archive(resolved_path) else "db",
        tables=tables,
        lesson_rows=lesson_rows,
        seen_lessons=seen_lessons,
        exercise_task_rows=exercise_task_rows,
        exercise_done_rows=exercise_done_rows,
        review_question_rows=review_question_rows,
        review_attempt_rows=review_attempt_rows,
        includes_review_uploads=includes_review_uploads,
        review_upload_file_count=review_upload_file_count,
    )


def validate_backup_schema(
    backup_path: str | Path,
    required_tables: set[str] | None = None,
) -> BackupInspection:
    inspection = inspect_backup(backup_path)
    expected_tables = required_tables or MINIMUM_REQUIRED_TABLES
    missing_tables = sorted(expected_tables - inspection.tables)
    if missing_tables:
        joined = ", ".join(missing_tables)
        raise SystemExit(f"Arquivo invalido: {inspection.path} nao contem tabela(s): {joined}")
    return inspection


def describe_backup_contents(inspection: BackupInspection) -> list[str]:
    lines = [
        f"arquivo: {inspection.path}",
        f"tipo: {inspection.kind}",
        f"tabelas: {', '.join(sorted(inspection.tables))}",
        f"aulas totais: {inspection.lesson_rows}",
        f"aulas vistas: {inspection.seen_lessons}",
    ]
    if inspection.includes_exercise_tasks:
        lines.append(f"exercicios salvos: {inspection.exercise_task_rows or 0}")
        lines.append(f"exercicios feitos: {inspection.exercise_done_rows or 0}")
    else:
        lines.append("exercicios salvos: tabela exercise_tasks ausente")
    if inspection.includes_review_question_tables:
        lines.append(f"questoes de revisao: {inspection.review_question_rows or 0}")
        lines.append(f"tentativas de revisao: {inspection.review_attempt_rows or 0}")
    else:
        lines.append("questoes de revisao: tabelas review_questions/review_question_attempts ausentes")
    if inspection.kind == "zip":
        lines.append(
            "uploads questoes revisao: "
            + (str(inspection.review_upload_file_count) if inspection.includes_review_uploads else "prefixo ausente")
        )
    return lines


def inspect_review_uploads(backup_path: str | Path) -> tuple[bool, int]:
    resolved_path = Path(backup_path).expanduser().resolve()
    if not is_backup_archive(resolved_path):
        return False, 0
    with zipfile.ZipFile(resolved_path) as archive:
        file_members = [
            name
            for name in archive.namelist()
            if _match_review_uploads_prefix(name) and not name.endswith("/")
        ]
        includes_prefix = any(_match_review_uploads_prefix(name) for name in archive.namelist())
    return includes_prefix, len(file_members)


def is_backup_archive(path: str | Path) -> bool:
    return Path(path).expanduser().resolve().suffix.lower() == ".zip"


@contextmanager
def extract_backup_database(backup_path: str | Path) -> Iterator[Path]:
    resolved_path = Path(backup_path).expanduser().resolve()
    if not is_backup_archive(resolved_path):
        yield resolved_path
        return

    with tempfile.TemporaryDirectory(prefix="cronograma-backup-db-") as temp_dir:
        temp_root = Path(temp_dir)
        extracted_db_path = temp_root / BACKUP_DB_ENTRY_NAME
        with zipfile.ZipFile(resolved_path) as archive:
            db_member = _find_backup_db_member(archive)
            with archive.open(db_member) as source, extracted_db_path.open("wb") as destination:
                shutil.copyfileobj(source, destination)
        yield extracted_db_path


def restore_review_uploads_from_backup(
    backup_path: str | Path,
    destination_dir: str | Path | None = None,
) -> bool:
    resolved_path = Path(backup_path).expanduser().resolve()
    target_dir = Path(destination_dir).expanduser().resolve() if destination_dir else REVIEW_QUESTION_UPLOADS_DIR
    if not is_backup_archive(resolved_path):
        return False

    extracted_any = False
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(resolved_path) as archive:
        for member in archive.namelist():
            matched_prefix = _matched_review_uploads_prefix(member)
            if not matched_prefix or member.endswith("/"):
                continue
            relative_path = Path(member).relative_to(matched_prefix.rstrip("/"))
            output_path = target_dir / relative_path
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, output_path.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            extracted_any = True
    return extracted_any


def _write_review_uploads_to_archive(archive: zipfile.ZipFile, uploads_dir: Path) -> None:
    uploads_dir = uploads_dir.expanduser().resolve()
    files = [path for path in uploads_dir.rglob("*") if path.is_file()] if uploads_dir.exists() else []
    if not files:
        archive.writestr(BACKUP_REVIEW_UPLOADS_PREFIX, "")
        return

    for path in files:
        relative_path = path.relative_to(uploads_dir).as_posix()
        archive.write(path, f"{BACKUP_REVIEW_UPLOADS_PREFIX}{relative_path}")


def _match_review_uploads_prefix(member_name: str) -> bool:
    return _matched_review_uploads_prefix(member_name) is not None


def _matched_review_uploads_prefix(member_name: str) -> str | None:
    for prefix in LEGACY_BACKUP_REVIEW_UPLOADS_PREFIXES:
        if member_name.startswith(prefix):
            return prefix
    return None


def _find_backup_db_member(archive: zipfile.ZipFile) -> str:
    if BACKUP_DB_ENTRY_NAME in archive.namelist():
        return BACKUP_DB_ENTRY_NAME
    for name in archive.namelist():
        if name.endswith(".db") or name.endswith(".sqlite"):
            return name
    raise SystemExit("Arquivo de backup invalido: banco SQLite ausente no .zip")


def _scalar_count(conn: sqlite3.Connection, query: str) -> int:
    row = conn.execute(query).fetchone()
    return int(row[0] if row else 0)
