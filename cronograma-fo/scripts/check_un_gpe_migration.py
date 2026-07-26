#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AFTER_DB = PROJECT_ROOT / "data" / "cronograma.db"
DEFAULT_REPORT = PROJECT_ROOT / "output" / "gpe_un_migration" / "plan_migracao_un.json"
DEFAULT_CSV = PROJECT_ROOT / "output" / "gpe_un_migration" / "universo_narrado_gpe.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "gpe_un_migration" / "validacao_migracao_un.json"
UN_SOURCE_SHEET = "universo_narrado_csv"


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def latest_backup() -> Path | None:
    backup_dir = PROJECT_ROOT / "data" / "backups" / "manual"
    candidates = sorted(backup_dir.glob("cronograma-before-un-gpe-*.db"))
    return candidates[-1] if candidates else None


def fetch_dicts(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def fetch_by_key(
    conn: sqlite3.Connection,
    query: str,
    key: str,
    params: tuple[Any, ...] = (),
) -> dict[str, dict[str, Any]]:
    return {row[key]: row for row in fetch_dicts(conn, query, params)}


def canonical_rows(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)


def derive_module_path(relative_path: str | None) -> str:
    path = unicodedata.normalize("NFC", (relative_path or "").strip()).replace("\\", "/")
    for marker in ("/Aulas/", "/Material de Apoio/"):
        if marker in path:
            return path.split(marker, 1)[0].rstrip("/")
    if path.endswith("/Lista.pdf"):
        return path[: -len("/Lista.pdf")].rstrip("/")
    if "/" in path:
        return path.rsplit("/", 1)[0].rstrip("/")
    return path


def normalize_path_key(path: str | None) -> str:
    text = unicodedata.normalize("NFKC", path or "")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text.lower().strip().replace("\\", "/")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def load_csv_relative_paths(path: Path) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        return [(row.get("relative_path") or "").strip() for row in reader if (row.get("relative_path") or "").strip()]


def validate(before_db: Path, after_db: Path, report_path: Path, csv_path: Path) -> dict[str, Any]:
    before = connect(before_db)
    after = connect(after_db)
    try:
        before_un = fetch_by_key(
            before,
            """
            SELECT slot_key, lesson_code, is_seen, seen_at, is_cut, cut_reason
            FROM lessons
            WHERE track_code = 'UN'
              AND source_sheet = ?
            """,
            "slot_key",
            (UN_SOURCE_SHEET,),
        )
        after_un = fetch_by_key(
            after,
            """
            SELECT slot_key, lesson_code, is_seen, seen_at, is_cut, cut_reason
            FROM lessons
            WHERE track_code = 'UN'
              AND source_sheet = ?
            """,
            "slot_key",
            (UN_SOURCE_SHEET,),
        )

        missing_un_slots = sorted(set(before_un) - set(after_un))
        changed_seen = []
        changed_cut = []
        for slot_key in sorted(set(before_un) & set(after_un)):
            old = before_un[slot_key]
            new = after_un[slot_key]
            if (old["is_seen"], old["seen_at"]) != (new["is_seen"], new["seen_at"]):
                changed_seen.append(
                    {
                        "slot_key": slot_key,
                        "before": {"is_seen": old["is_seen"], "seen_at": old["seen_at"]},
                        "after": {"is_seen": new["is_seen"], "seen_at": new["seen_at"]},
                    }
                )
            if (old["is_cut"], old["cut_reason"]) != (new["is_cut"], new["cut_reason"]):
                changed_cut.append(
                    {
                        "slot_key": slot_key,
                        "before": {"is_cut": old["is_cut"], "cut_reason": old["cut_reason"]},
                        "after": {"is_cut": new["is_cut"], "cut_reason": new["cut_reason"]},
                    }
                )

        before_fo_lessons = fetch_dicts(before, "SELECT * FROM lessons WHERE track_code = 'FO' ORDER BY slot_key")
        after_fo_lessons = fetch_dicts(after, "SELECT * FROM lessons WHERE track_code = 'FO' ORDER BY slot_key")
        fo_lessons_changed = canonical_rows(before_fo_lessons) != canonical_rows(after_fo_lessons)

        before_daily = fetch_dicts(before, "SELECT * FROM daily_assignments ORDER BY dashboard_date, planned_slot_key")
        after_daily = fetch_dicts(after, "SELECT * FROM daily_assignments ORDER BY dashboard_date, planned_slot_key")
        daily_assignments_changed = canonical_rows(before_daily) != canonical_rows(after_daily)

        before_un_daily_count = before.execute("SELECT COUNT(*) AS total FROM un_daily_assignments").fetchone()["total"]
        after_un_daily_count = after.execute("SELECT COUNT(*) AS total FROM un_daily_assignments").fetchone()["total"]

        migration_report = load_json(report_path)
        csv_relative_paths = load_csv_relative_paths(csv_path)
        after_relative_paths = {
            row["relative_path"]
            for row in fetch_dicts(
                after,
                """
                SELECT relative_path
                FROM lessons
                WHERE track_code = 'UN'
                  AND source_sheet = ?
                  AND relative_path IS NOT NULL
                """,
                (UN_SOURCE_SHEET,),
            )
        }
        missing_csv_paths = sorted(set(csv_relative_paths) - after_relative_paths)

        found_modules = migration_report.get("modulos_gpe_encontrados") or []
        after_module_path_keys = {
            normalize_path_key(derive_module_path(row["relative_path"]))
            for row in fetch_dicts(
                after,
                """
                SELECT relative_path
                FROM lessons
                WHERE track_code = 'UN'
                  AND source_sheet = ?
                  AND relative_path IS NOT NULL
                """,
                (UN_SOURCE_SHEET,),
            )
        }
        missing_imported_modules = sorted(
            {
                module.get("module_path")
                for module in found_modules
                if module.get("module_path")
                and normalize_path_key(module.get("module_path")) not in after_module_path_keys
            }
        )

        failures = []
        if missing_un_slots:
            failures.append("slot_keys_un_apagados")
        if changed_seen:
            failures.append("vistos_alterados")
        if changed_cut:
            failures.append("cortados_alterados")
        if fo_lessons_changed:
            failures.append("lessons_fo_alteradas")
        if daily_assignments_changed:
            failures.append("daily_assignments_alterada")
        if missing_csv_paths:
            failures.append("paths_do_csv_nao_importados")
        if missing_imported_modules:
            failures.append("modulos_gpe_encontrados_nao_importados")
        if migration_report.get("modulos_gpe_nao_encontrados"):
            failures.append("modulos_gpe_nao_encontrados_no_plano")

        return {
            "status": "ok" if not failures else "fail",
            "failures": failures,
            "before_db": str(before_db),
            "after_db": str(after_db),
            "report_path": str(report_path),
            "csv_path": str(csv_path),
            "counts": {
                "un_lessons_before": len(before_un),
                "un_lessons_after": len(after_un),
                "un_lessons_added": len(set(after_un) - set(before_un)),
                "un_lessons_deleted": len(missing_un_slots),
                "fo_lessons_before": len(before_fo_lessons),
                "fo_lessons_after": len(after_fo_lessons),
                "daily_assignments_before": len(before_daily),
                "daily_assignments_after": len(after_daily),
                "un_daily_assignments_before": before_un_daily_count,
                "un_daily_assignments_after": after_un_daily_count,
                "csv_relative_paths": len(csv_relative_paths),
            },
            "deleted_un_slot_keys": missing_un_slots,
            "changed_seen": changed_seen,
            "changed_cut": changed_cut,
            "fo_lessons_changed": fo_lessons_changed,
            "daily_assignments_changed": daily_assignments_changed,
            "un_daily_assignments_note": "Pode mudar/zerar: cache da home UN.",
            "missing_csv_relative_paths": missing_csv_paths,
            "missing_imported_modules": missing_imported_modules,
            "gpe_modules_not_found": migration_report.get("modulos_gpe_nao_encontrados") or [],
        }
    finally:
        before.close()
        after.close()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        json.dump(payload, file_obj, ensure_ascii=False, indent=2)
        file_obj.write("\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Valida a migração segura do Universo Narrado por GPE.")
    parser.add_argument("--before", type=Path, help="Backup do banco antes do import. Padrão: último cronograma-before-un-gpe-*.db")
    parser.add_argument("--after", type=Path, default=DEFAULT_AFTER_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    before_db = args.before or latest_backup()
    if before_db is None:
        print("Nenhum backup encontrado. Informe --before PATH.", file=sys.stderr)
        return 2
    if not before_db.exists():
        print(f"Backup não encontrado: {before_db}", file=sys.stderr)
        return 2
    if not args.after.exists():
        print(f"Banco pós-migração não encontrado: {args.after}", file=sys.stderr)
        return 2

    result = validate(before_db=before_db, after_db=args.after, report_path=args.report, csv_path=args.csv)
    write_json(args.output, result)

    print(f"Validação: {result['status']}")
    print(f"Relatório: {args.output}")
    if result["failures"]:
        print("Falhas: " + ", ".join(result["failures"]))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
