#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


DEFAULT_STATE_ROOT = PROJECT_ROOT / "state" / "federal_online"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "cronograma.db"
CRONOGRAMA_FO_SUBDIR = "cronograma_fo"

REPORT_COLUMNS = [
    "action",
    "applied",
    "lesson_code",
    "slot_key",
    "title_raw",
    "subject_name",
    "module_number",
    "lesson_number",
    "target_label",
    "portal_disciplina",
    "portal_subject",
    "source_ref",
    "status",
    "old_portal_title",
    "new_portal_title",
    "old_duration_seconds",
    "new_duration_seconds",
    "media_id",
    "item_id",
    "error",
]

REGULAR_DISCIPLINE_MAP: dict[str, tuple[str, int | None, str]] = {
    "Biologia I": ("Biologia", 1, "Biologia I - Federal"),
    "Biologia II": ("Biologia", 2, "Biologia II - Federal"),
    "Biologia III": ("Biologia", 3, "Biologia III - Federal"),
    "Física I": ("Física", 1, "Física I - Federal"),
    "Física II": ("Física", 2, "Física II - Federal"),
    "Física III": ("Física", 3, "Física III - Federal"),
    "Geografia I": ("Geografia", 1, "Geografia I - Federal"),
    "Geografia II": ("Geografia", 2, "Geografia II - Federal"),
    "Geografia III": ("Geografia", 3, "Geografia III - Federal"),
    "História I": ("História", 1, "História I - Federal"),
    "História II": ("História", 2, "História II - Federal"),
    "Matemática I": ("Matemática", 1, "Matemática I - Federal"),
    "Matemática II": ("Matemática", 2, "Matemática II - Federal"),
    "Matemática III": ("Matemática", 3, "Matemática III - Federal"),
    "Português I": ("Português", 1, "Português I - Federal"),
    "Português II": ("Português", 2, "Português II - Federal"),
    "Química I": ("Química", 1, "Química I - Federal"),
    "Química II": ("Química", 2, "Química II - Federal"),
    "Química III": ("Química", 3, "Química III - Federal"),
}

OBRAS_UFPR_DISCIPLINE_MAP: dict[str, tuple[str, int | None, str]] = {
    "Filosofia UFPR": ("Filosofia UFPR", None, "Filosofia - Federal"),
    "Literatura UFPR": ("Literatura UFPR", None, "Literatura - Federal"),
    "Sociologia UFPR": ("Sociologia UFPR", None, "Sociologia - Federal"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge controlado de metadados das aulas FO no cronograma existente."
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=DEFAULT_STATE_ROOT,
        help=f"State root do Cronograma FO. Padrao: {DEFAULT_STATE_ROOT}",
    )
    parser.add_argument(
        "--index-json",
        type=Path,
        default=None,
        help="Caminho do aulas_index.json. Padrao: <state-root>/cronograma_fo/aulas_index.json",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Banco cronograma.db. Padrao: {DEFAULT_DB_PATH}",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Relatorio TSV. Padrao: <state-root>/cronograma_fo/merge_report.tsv",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--apply",
        action="store_true",
        help="Aplica updates no banco. Sem esta flag, roda em dry-run.",
    )
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Forca modo dry-run. E o comportamento padrao.",
    )
    return parser.parse_args()


def parse_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(round(float(text)))
    except ValueError:
        return None


def portal_title_from_entry(entry: dict[str, Any]) -> str | None:
    title = str(entry.get("titulo_original") or entry.get("lesson_title") or "").strip()
    return title or None


def ensure_portal_title_column(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(lessons)").fetchall()}
    if "portal_title" not in columns:
        conn.execute("ALTER TABLE lessons ADD COLUMN portal_title TEXT")


def normalize_text(value: str) -> str:
    lowered = value.strip().lower()
    normalized = unicodedata.normalize("NFKD", lowered)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def build_fo_metadata_key(
    subject_name: str | None,
    module_number: int | None,
    lesson_number: int | None,
) -> tuple[str, int | None, int] | None:
    if not subject_name or lesson_number is None:
        return None
    return (normalize_text(subject_name), module_number, lesson_number)


def source_parts(source_ref: str) -> list[str]:
    return [part.strip() for part in (source_ref or "").split(">") if part.strip()]


def is_obras_ufpr(entry: dict[str, Any]) -> bool:
    parts = source_parts(str(entry.get("source_ref") or ""))
    return len(parts) >= 4 and parts[0] == "Específicas" and parts[1] == "Obras" and parts[2] == "UFPR"


def strip_federal_suffix(value: str) -> str:
    return value.removesuffix(" - Federal").strip()


def candidate_labels(entry: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for value in (
        entry.get("disciplina"),
        entry.get("portal_subject"),
        *reversed(source_parts(str(entry.get("source_ref") or ""))),
    ):
        label = strip_federal_suffix(str(value or "").strip())
        if label and label not in labels:
            labels.append(label)
    return labels


def find_regular_mapping(entry: dict[str, Any]) -> tuple[str, tuple[str, int | None, str]] | None:
    for label in candidate_labels(entry):
        mapping = REGULAR_DISCIPLINE_MAP.get(label)
        if mapping:
            return label, mapping
    return None


def find_obras_mapping(entry: dict[str, Any]) -> tuple[str, tuple[str, int | None, str]] | None:
    if not is_obras_ufpr(entry):
        return None
    for label in candidate_labels(entry):
        mapping = OBRAS_UFPR_DISCIPLINE_MAP.get(label)
        if mapping:
            return label, mapping
    return None


def map_index_entry(entry: dict[str, Any]) -> tuple[tuple[str, int | None, int] | None, str, str]:
    lesson_number = parse_optional_int(entry.get("ordem"))
    if lesson_number is None:
        return None, "", "missing_lesson_number"

    regular_match = find_regular_mapping(entry)
    if regular_match:
        _, mapping = regular_match
        subject_name, module_number, target_label = mapping
        key = build_fo_metadata_key(subject_name, module_number, lesson_number)
        return key, target_label, ""

    obras_match = find_obras_mapping(entry)
    if obras_match:
        _, obras_mapping = obras_match
        subject_name, module_number, target_label = obras_mapping
        key = build_fo_metadata_key(subject_name, module_number, lesson_number)
        return key, target_label, ""

    for label in candidate_labels(entry):
        obras_mapping = OBRAS_UFPR_DISCIPLINE_MAP.get(label)
        if obras_mapping:
            return None, obras_mapping[2], "not_obras_ufpr"

    return None, "", "discipline_not_in_main_track"


def load_index(index_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    aulas = payload.get("aulas")
    if not isinstance(aulas, list):
        raise ValueError(f"Indice invalido, campo 'aulas' ausente ou invalido: {index_path}")
    return [item for item in aulas if isinstance(item, dict)]


def candidate_signature(candidate: dict[str, Any]) -> tuple[str, str, str, str, str]:
    entry = candidate["entry"]
    return (
        str(entry.get("media_id") or ""),
        str(entry.get("item_id") or ""),
        str(entry.get("duracao_segundos") or ""),
        str(entry.get("titulo_original") or ""),
        str(entry.get("source_ref") or ""),
    )


def choose_candidate(candidates: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    ok_candidates = [candidate for candidate in candidates if candidate["entry"].get("status") == "ok"]
    if len(ok_candidates) == 1:
        return ok_candidates[0], ""
    if len(ok_candidates) > 1:
        signatures = {candidate_signature(candidate) for candidate in ok_candidates}
        if len(signatures) == 1:
            return ok_candidates[0], ""
        return None, "ambiguous"
    if candidates:
        return candidates[0], "no_ok_candidate"
    return None, "unmatched"


def report_row(
    *,
    action: str,
    applied: bool,
    db_row: sqlite3.Row | None = None,
    candidate: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    entry = candidate["entry"] if candidate else {}
    return {
        "action": action,
        "applied": int(applied),
        "lesson_code": db_row["lesson_code"] if db_row else "",
        "slot_key": db_row["slot_key"] if db_row else "",
        "title_raw": db_row["title_raw"] if db_row else "",
        "subject_name": db_row["subject_name"] if db_row else "",
        "module_number": "" if not db_row or db_row["module_number"] is None else db_row["module_number"],
        "lesson_number": "" if not db_row or db_row["lesson_number"] is None else db_row["lesson_number"],
        "target_label": candidate.get("target_label", "") if candidate else "",
        "portal_disciplina": entry.get("disciplina", ""),
        "portal_subject": entry.get("portal_subject", ""),
        "source_ref": entry.get("source_ref", ""),
        "status": entry.get("status", ""),
        "old_portal_title": "" if not db_row else db_row["portal_title"] or "",
        "new_portal_title": portal_title_from_entry(entry) or "",
        "old_duration_seconds": "" if not db_row or db_row["duration_seconds"] is None else db_row["duration_seconds"],
        "new_duration_seconds": "" if entry.get("duracao_segundos") is None else entry.get("duracao_segundos"),
        "media_id": entry.get("media_id", ""),
        "item_id": entry.get("item_id", ""),
        "error": error or entry.get("error", ""),
    }


def write_report(report_path: Path, rows: list[dict[str, Any]]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = report_path.with_name(f".{report_path.name}.tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(tmp_path, report_path)


def build_candidates(
    aulas: list[dict[str, Any]],
    report_rows: list[dict[str, Any]],
) -> dict[tuple[str, int | None, int], list[dict[str, Any]]]:
    candidates_by_key: dict[tuple[str, int | None, int], list[dict[str, Any]]] = defaultdict(list)
    for entry in aulas:
        key, target_label, skip_reason = map_index_entry(entry)
        if key is None:
            report_rows.append(
                report_row(
                    action="skipped_non_main_track",
                    applied=False,
                    candidate={"entry": entry, "target_label": target_label},
                    error=skip_reason,
                )
            )
            continue
        candidates_by_key[key].append({"entry": entry, "target_label": target_label})
    return candidates_by_key


def merge_metadata(
    *,
    db_path: Path,
    index_path: Path,
    report_path: Path,
    apply_updates: bool,
) -> Counter:
    aulas = load_index(index_path)
    report_rows: list[dict[str, Any]] = []
    candidates_by_key = build_candidates(aulas, report_rows)
    counts: Counter = Counter(row["action"] for row in report_rows)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_portal_title_column(conn)
        db_rows = conn.execute(
            """
            SELECT
                slot_key,
                lesson_code,
                title_raw,
                portal_title,
                duration_seconds,
                subject_name,
                module_number,
                lesson_number,
                recommended_date,
                week_number,
                day_index,
                slot_index
            FROM lessons
            WHERE track_code = 'FO'
              AND lesson_type = 'lesson'
            ORDER BY recommended_date, week_number, day_index, slot_index, slot_key
            """
        ).fetchall()

        for db_row in db_rows:
            key = build_fo_metadata_key(
                db_row["subject_name"],
                db_row["module_number"],
                db_row["lesson_number"],
            )
            candidates = candidates_by_key.get(key or (), [])
            if not candidates:
                action = "unmatched"
                counts[action] += 1
                report_rows.append(report_row(action=action, applied=False, db_row=db_row))
                continue

            candidate, reason = choose_candidate(candidates)
            if reason == "ambiguous" or candidate is None:
                action = "ambiguous"
                counts[action] += 1
                report_rows.append(
                    report_row(action=action, applied=False, db_row=db_row, error=reason)
                )
                continue

            entry = candidate["entry"]
            new_portal_title = portal_title_from_entry(entry)
            status = str(entry.get("status") or "")
            if status == "not_launched":
                action = "matched_kept_previous_due_not_launched"
                counts[action] += 1
                report_rows.append(report_row(action=action, applied=False, db_row=db_row, candidate=candidate))
                continue
            if status != "ok":
                action = "matched_kept_previous_due_error"
                counts[action] += 1
                report_rows.append(report_row(action=action, applied=False, db_row=db_row, candidate=candidate))
                continue

            new_duration = parse_optional_int(entry.get("duracao_segundos"))
            if new_duration is None:
                action = "matched_kept_previous_due_error"
                counts[action] += 1
                report_rows.append(
                    report_row(
                        action=action,
                        applied=False,
                        db_row=db_row,
                        candidate=candidate,
                        error="missing_duration_seconds",
                    )
                )
                continue

            applied = False
            should_update_title = bool(new_portal_title) and db_row["portal_title"] != new_portal_title
            should_update_duration = db_row["duration_seconds"] != new_duration
            if apply_updates and (should_update_duration or should_update_title):
                conn.execute(
                    """
                    UPDATE lessons
                    SET duration_seconds = ?,
                        portal_title = COALESCE(?, portal_title),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE slot_key = ?
                    """,
                    (new_duration, new_portal_title, db_row["slot_key"]),
                )
                applied = True

            action = "matched_updated"
            counts[action] += 1
            report_rows.append(report_row(action=action, applied=applied, db_row=db_row, candidate=candidate))

        if apply_updates:
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()

    write_report(report_path, report_rows)
    return counts


def main() -> int:
    args = parse_args()
    state_root = args.state_root.resolve()
    index_path = (args.index_json or state_root / CRONOGRAMA_FO_SUBDIR / "aulas_index.json").resolve()
    db_path = args.db.resolve()
    report_path = (args.report or state_root / CRONOGRAMA_FO_SUBDIR / "merge_report.tsv").resolve()
    apply_updates = bool(args.apply)

    print(f"Modo: {'APPLY' if apply_updates else 'DRY-RUN'}")
    print(f"Indice: {index_path}")
    print(f"Banco: {db_path}")
    print(f"Relatorio: {report_path}")

    counts = merge_metadata(
        db_path=db_path,
        index_path=index_path,
        report_path=report_path,
        apply_updates=apply_updates,
    )

    print("Resumo:")
    for action, count in sorted(counts.items()):
        print(f"  {action}: {count}")
    if not apply_updates:
        print("Dry-run: nenhum update foi aplicado ao banco.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
