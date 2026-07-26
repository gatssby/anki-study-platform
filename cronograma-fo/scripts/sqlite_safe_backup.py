#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class SQLiteValidation:
    path: Path
    integrity_check: str
    foreign_key_violations: int
    size_bytes: int
    sha256: str

    @property
    def is_valid(self) -> bool:
        return self.integrity_check == "ok" and self.foreign_key_violations == 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_sqlite_database(db_path: str | Path) -> SQLiteValidation:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Banco SQLite ausente ou vazio: {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
            integrity = "ok" if integrity_rows == [("ok",)] else "; ".join(str(row[0]) for row in integrity_rows)
            foreign_key_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"Falha ao validar SQLite {path}: {exc}") from exc
    result = SQLiteValidation(
        path=path,
        integrity_check=integrity,
        foreign_key_violations=foreign_key_violations,
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
    )
    if not result.is_valid:
        raise RuntimeError(
            f"Banco SQLite invalido: {path}; integrity_check={integrity}; "
            f"foreign_key_violations={foreign_key_violations}"
        )
    return result


def timestamped_snapshot_path(
    backup_dir: str | Path,
    *,
    prefix: str,
) -> Path:
    directory = Path(backup_dir).expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return directory / f"{prefix}{timestamp}.db"


def create_sqlite_snapshot(
    source_path: str | Path,
    output_path: str | Path,
) -> SQLiteValidation:
    source = Path(source_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()
    if source == destination:
        raise RuntimeError("Origem e destino do snapshot SQLite devem ser diferentes")
    if not source.is_file():
        raise RuntimeError(f"Banco SQLite de origem ausente: {source}")
    if destination.exists():
        raise RuntimeError(f"Destino do snapshot ja existe; nada foi sobrescrito: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
        destination_conn.commit()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"Falha ao criar snapshot SQLite {destination}: {exc}") from exc
    finally:
        destination_conn.close()
        source_conn.close()
    return validate_sqlite_database(destination)


def restore_sqlite_snapshot(
    source_path: str | Path,
    destination_path: str | Path,
) -> SQLiteValidation:
    source = Path(source_path).expanduser().resolve()
    destination = Path(destination_path).expanduser().resolve()
    validate_sqlite_database(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        assert_exclusive_access(destination)
    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
        destination_conn.commit()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"Falha ao restaurar SQLite em {destination}: {exc}") from exc
    finally:
        destination_conn.close()
        source_conn.close()
    return validate_sqlite_database(destination)


def assert_exclusive_access(db_path: str | Path) -> None:
    path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(path, timeout=1)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.rollback()
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"Banco possui conexao ativa; acesso exclusivo negado: {path}: {exc}") from exc
    finally:
        conn.close()


def print_validation(label: str, result: SQLiteValidation) -> None:
    print(f"{label}_path={result.path}")
    print(f"{label}_integrity_check={result.integrity_check}")
    print(f"{label}_foreign_key_violations={result.foreign_key_violations}")
    print(f"{label}_size_bytes={result.size_bytes}")
    print(f"{label}_sha256={result.sha256}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backup e restore SQLite consistentes")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--source", required=True)
    backup.add_argument("--output", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--db", required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--source", required=True)
    restore.add_argument("--destination", required=True)
    exclusive = subparsers.add_parser("assert-exclusive")
    exclusive.add_argument("--db", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "backup":
            result = create_sqlite_snapshot(args.source, args.output)
            print_validation("snapshot", result)
        elif args.command == "validate":
            result = validate_sqlite_database(args.db)
            print_validation("database", result)
        elif args.command == "restore":
            result = restore_sqlite_snapshot(args.source, args.destination)
            print_validation("restored", result)
        else:
            assert_exclusive_access(args.db)
            print(f"exclusive_access=ok")
    except RuntimeError as exc:
        print(f"sqlite_safe_backup_error={exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
