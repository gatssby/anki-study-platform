#!/usr/bin/env python3
"""Build and verify the Federal Online transcript FTS5 search index."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid


DEFAULT_QUEUE_DB = Path(os.environ.get("FO_TRANSCRIPTS_DB_PATH", "/home/ubuntu/fo-transcricoes-system/queue.sqlite"))
DEFAULT_ROOT = Path(os.environ.get("FO_TRANSCRIPTS_ROOT", "/home/ubuntu/fo-transcricoes/Federal Online Transcrições"))
DEFAULT_INDEX = Path(os.environ.get("FO_SEARCH_INDEX_PATH", "/home/ubuntu/anki-gpt-sync/state/federal_online/fo_transcripts_fts.sqlite"))
PREFIX = "Federal Online Transcrições/"


def parse_args():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rebuild", action="store_true")
    mode.add_argument("--incremental", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--queue-db", type=Path, default=DEFAULT_QUEUE_DB)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    return parser.parse_args()


def relative_output_path(value):
    value = str(value or "")
    if value.startswith(PREFIX):
        value = value[len(PREFIX):]
    path = Path(value)
    if not value or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("invalid_relative_path")
    return path


def file_record(root: Path, row: sqlite3.Row):
    relative = relative_output_path(row["output_relative_path"])
    root = root.resolve()
    path = (root / relative).resolve()
    path.relative_to(root)
    if not path.is_file() or path.is_symlink():
        return None
    body = path.read_bytes()
    text = body.decode("utf-8", errors="replace")
    stat = path.stat()
    return {
        "path": relative.as_posix(),
        "disciplina": str(row["materia"] or ""),
        "frente": str(row["frente"] or ""),
        "aula": str(row["aula_number"] or ""),
        "title": str(row["aula_title"] or ""),
        "tipo": str(row["tipo"] or ""),
        "content": text,
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def source_records(queue_db: Path, root: Path):
    connection = sqlite3.connect(f"file:{queue_db.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            select output_relative_path, materia, frente, aula_number, aula_title, tipo
            from transcription_queue
            where status = 'done'
            order by output_relative_path
            """
        ).fetchall()
    finally:
        connection.close()
    records = []
    missing = 0
    for row in rows:
        try:
            record = file_record(root, row)
        except (ValueError, OSError):
            record = None
        if record is None:
            missing += 1
        else:
            records.append(record)
    return records, missing


def create_schema(connection):
    connection.executescript(
        """
        create table documents (
            path text primary key,
            disciplina text not null,
            frente text not null,
            aula text not null,
            title text not null,
            tipo text not null,
            mtime_ns integer not null,
            size integer not null,
            sha256 text not null
        );
        create virtual table transcripts_fts using fts5(
            path unindexed,
            disciplina,
            frente,
            aula,
            title,
            tipo,
            content,
            tokenize='unicode61 remove_diacritics 2'
        );
        create table metadata (key text primary key, value text not null);
        """
    )


def upsert_record(connection, record):
    connection.execute("delete from transcripts_fts where path = ?", (record["path"],))
    connection.execute(
        """insert or replace into documents
        (path, disciplina, frente, aula, title, tipo, mtime_ns, size, sha256)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        tuple(record[key] for key in ("path", "disciplina", "frente", "aula", "title", "tipo", "mtime_ns", "size", "sha256")),
    )
    connection.execute(
        """insert into transcripts_fts
        (path, disciplina, frente, aula, title, tipo, content)
        values (?, ?, ?, ?, ?, ?, ?)""",
        tuple(record[key] for key in ("path", "disciplina", "frente", "aula", "title", "tipo", "content")),
    )


def verify_index(index_path: Path):
    connection = sqlite3.connect(f"file:{index_path.resolve()}?mode=ro", uri=True)
    try:
        integrity = connection.execute("pragma integrity_check").fetchone()[0]
        docs = connection.execute("select count(*) from documents").fetchone()[0]
        fts = connection.execute("select count(*) from transcripts_fts").fetchone()[0]
        if integrity != "ok" or docs != fts:
            raise RuntimeError("fo_search_index_invalid")
        return {"integrity": integrity, "documents": docs, "fts_rows": fts}
    finally:
        connection.close()


def rebuild(index_path: Path, records):
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = index_path.with_name(f".{index_path.name}.{uuid.uuid4().hex}.tmp")
    connection = sqlite3.connect(temporary)
    try:
        create_schema(connection)
        with connection:
            for record in records:
                upsert_record(connection, record)
            connection.execute("insert into metadata values (?, ?)", ("schema_version", "1"))
            connection.execute("insert into metadata values (?, ?)", ("built_at", datetime.now(timezone.utc).isoformat()))
        connection.execute("pragma wal_checkpoint(truncate)")
    finally:
        connection.close()
    verify_index(temporary)
    temporary.replace(index_path)


def incremental(index_path: Path, records):
    if not index_path.exists():
        rebuild(index_path, records)
        return {"created": len(records), "updated": 0, "removed": 0}
    connection = sqlite3.connect(index_path)
    try:
        existing = {row[0]: (row[1], row[2], row[3]) for row in connection.execute("select path, mtime_ns, size, sha256 from documents")}
        current = {record["path"]: record for record in records}
        created = updated = removed = 0
        with connection:
            for path in sorted(set(existing) - set(current)):
                connection.execute("delete from transcripts_fts where path = ?", (path,))
                connection.execute("delete from documents where path = ?", (path,))
                removed += 1
            for path, record in current.items():
                signature = (record["mtime_ns"], record["size"], record["sha256"])
                if path not in existing:
                    upsert_record(connection, record)
                    created += 1
                elif existing[path] != signature:
                    upsert_record(connection, record)
                    updated += 1
            connection.execute("insert or replace into metadata values (?, ?)", ("built_at", datetime.now(timezone.utc).isoformat()))
        return {"created": created, "updated": updated, "removed": removed}
    finally:
        connection.close()


def main():
    args = parse_args()
    if args.verify:
        print(json.dumps(verify_index(args.index), sort_keys=True))
        return 0
    records, missing = source_records(args.queue_db, args.root)
    summary = {"records": len(records), "missing": missing, "mode": "rebuild" if args.rebuild else "incremental", "dry_run": args.dry_run}
    if args.dry_run:
        print(json.dumps(summary, sort_keys=True))
        return 0
    if args.rebuild:
        rebuild(args.index, records)
        summary.update(verify_index(args.index))
    else:
        summary.update(incremental(args.index, records))
        summary.update(verify_index(args.index))
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
