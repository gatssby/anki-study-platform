#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class DailyAssignmentValidation:
    total_count: int
    exact_count: int
    substitution_count: int
    duplicate_count: int
    historical_duplicate_count: int
    slot_without_target_count: int
    incompatible_target_count: int
    errors: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.errors


def _valid_iso_date(value: object) -> bool:
    try:
        date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return False
    return True


def current_local_date() -> date:
    try:
        tz = ZoneInfo("America/Sao_Paulo")
    except Exception:
        tz = timezone(timedelta(hours=-3))
    return datetime.now(tz).date()


def validate_daily_assignments(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | None = None,
) -> DailyAssignmentValidation:
    validation_date = as_of_date or current_local_date()
    rows = conn.execute(
        """
        SELECT
          da.rowid AS assignment_rowid,
          da.dashboard_date,
          da.planned_slot_key,
          da.assigned_lesson_code,
          planned.lesson_code AS planned_lesson_code,
          planned.track_code AS planned_track_code,
          planned.lesson_type AS planned_lesson_type,
          planned.subject_prefix AS planned_subject_prefix,
          planned.module_number AS planned_module_number,
          planned.recommended_date AS planned_recommended_date,
          planned.is_cut AS planned_is_cut,
          assigned.track_code AS assigned_track_code,
          assigned.lesson_type AS assigned_lesson_type,
          assigned.subject_prefix AS assigned_subject_prefix,
          assigned.module_number AS assigned_module_number,
          assigned.recommended_date AS assigned_recommended_date,
          assigned.is_cut AS assigned_is_cut
        FROM daily_assignments da
        LEFT JOIN lessons planned ON planned.slot_key = da.planned_slot_key
        LEFT JOIN lessons assigned ON assigned.lesson_code = da.assigned_lesson_code
        ORDER BY da.dashboard_date, da.planned_slot_key, da.rowid
        """
    ).fetchall()

    column_names = (
        "assignment_rowid",
        "dashboard_date",
        "planned_slot_key",
        "assigned_lesson_code",
        "planned_lesson_code",
        "planned_track_code",
        "planned_lesson_type",
        "planned_subject_prefix",
        "planned_module_number",
        "planned_recommended_date",
        "planned_is_cut",
        "assigned_track_code",
        "assigned_lesson_type",
        "assigned_subject_prefix",
        "assigned_module_number",
        "assigned_recommended_date",
        "assigned_is_cut",
    )
    assignments = [dict(zip(column_names, row, strict=True)) for row in rows]
    errors: list[str] = []
    exact_count = 0
    substitution_count = 0

    key_counts = Counter(
        (row["dashboard_date"], row["planned_slot_key"])
        for row in assignments
    )
    for (dashboard_date, planned_slot_key), count in sorted(key_counts.items()):
        if count > 1:
            errors.append(
                f"assignment duplicado para dashboard_date={dashboard_date} "
                f"planned_slot_key={planned_slot_key}: count={count}"
            )

    assignments_by_date_and_lesson: dict[
        tuple[str, str],
        list[dict[str, object]],
    ] = defaultdict(list)
    slot_without_target_count = 0
    incompatible_target_count = 0

    for row in assignments:
        row_label = (
            f"dashboard_date={row['dashboard_date']} "
            f"planned_slot_key={row['planned_slot_key']} "
            f"assigned_lesson_code={row['assigned_lesson_code']}"
        )
        planned_exists = row["planned_lesson_code"] is not None
        assigned_code = str(row["assigned_lesson_code"] or "").strip()
        has_assigned_code = bool(assigned_code)
        assigned_exists = row["assigned_track_code"] is not None

        if not planned_exists:
            errors.append(f"slot planejado inexistente: {row_label}")
        if not has_assigned_code:
            slot_without_target_count += 1
            errors.append(f"slot sem alvo: {row_label}")
            continue
        assignments_by_date_and_lesson[
            (str(row["dashboard_date"]), assigned_code)
        ].append(row)
        if not assigned_exists:
            errors.append(f"aula atribuida inexistente: {row_label}")
        if not planned_exists or not assigned_exists:
            continue

        if row["planned_track_code"] != "FO":
            errors.append(f"slot planejado nao pertence a FO: {row_label}")
        if row["assigned_track_code"] != "FO":
            errors.append(f"aula atribuida nao pertence a FO: {row_label}")
        if row["planned_lesson_type"] != "lesson":
            errors.append(f"slot planejado nao e videoaula: {row_label}")
        if row["assigned_lesson_type"] != "lesson":
            errors.append(f"aula atribuida nao e videoaula: {row_label}")
        if row["planned_is_cut"] != 0:
            errors.append(f"slot planejado esta cortado: {row_label}")
        if row["assigned_is_cut"] != 0:
            errors.append(f"aula atribuida esta cortada: {row_label}")
        dashboard_date_is_valid = _valid_iso_date(row["dashboard_date"])
        if not dashboard_date_is_valid:
            errors.append(f"dashboard_date invalida: {row_label}")
        is_active_assignment = (
            dashboard_date_is_valid
            and date.fromisoformat(str(row["dashboard_date"])) >= validation_date
        )
        if (
            is_active_assignment
            and row["planned_recommended_date"] != row["dashboard_date"]
        ):
            errors.append(
                f"slot planejado fora da data exibida: {row_label} "
                f"planned_recommended_date={row['planned_recommended_date']}"
            )

        if row["assigned_lesson_code"] == row["planned_lesson_code"]:
            exact_count += 1
            continue

        substitution_count += 1
        same_subject = (
            row["planned_subject_prefix"] is not None
            and row["planned_subject_prefix"] == row["assigned_subject_prefix"]
        )
        same_module = row["planned_module_number"] == row["assigned_module_number"]
        if not same_subject or not same_module:
            incompatible_target_count += 1
            errors.append(
                f"substituicao incompatível com disciplina/modulo da home: {row_label} "
                f"planned={row['planned_subject_prefix']}/{row['planned_module_number']} "
                f"assigned={row['assigned_subject_prefix']}/{row['assigned_module_number']}"
            )
        if not _valid_iso_date(row["assigned_recommended_date"]):
            errors.append(f"recommended_date da aula atribuida invalida: {row_label}")
        elif is_active_assignment and (
            str(row["assigned_recommended_date"]) > str(row["dashboard_date"])
        ):
            errors.append(
                f"aula atribuida ainda nao era elegivel na data exibida: {row_label} "
                f"assigned_recommended_date={row['assigned_recommended_date']}"
            )

    duplicate_count = 0
    historical_duplicate_count = 0
    for (
        dashboard_date,
        assigned_lesson_code,
    ), duplicate_rows in sorted(assignments_by_date_and_lesson.items()):
        if len(duplicate_rows) < 2:
            continue
        if _valid_iso_date(dashboard_date) and date.fromisoformat(dashboard_date) < validation_date:
            historical_duplicate_count += 1
        else:
            duplicate_count += 1
            planned_slots = ",".join(
                sorted(str(row["planned_slot_key"]) for row in duplicate_rows)
            )
            errors.append(
                "assigned_lesson_code duplicado na mesma data: "
                f"dashboard_date={dashboard_date} "
                f"assigned_lesson_code={assigned_lesson_code} "
                f"planned_slot_keys={planned_slots}"
            )

    return DailyAssignmentValidation(
        total_count=len(assignments),
        exact_count=exact_count,
        substitution_count=substitution_count,
        duplicate_count=duplicate_count,
        historical_duplicate_count=historical_duplicate_count,
        slot_without_target_count=slot_without_target_count,
        incompatible_target_count=incompatible_target_count,
        errors=tuple(errors),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Valida o contrato FO entre a home e daily_assignments.",
    )
    parser.add_argument("--db", required=True, help="Caminho do banco SQLite")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Reporta inconsistencias sem retornar erro, como o dry-run do sync semanal.",
    )
    parser.add_argument(
        "--as-of-date",
        type=date.fromisoformat,
        help="Data local de referencia em YYYY-MM-DD; padrao: hoje em America/Sao_Paulo.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    db_path = Path(args.db).resolve()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        report = validate_daily_assignments(conn, as_of_date=args.as_of_date)
    finally:
        conn.close()

    print(f"daily_assignment_total={report.total_count}")
    print(f"daily_assignment_exact={report.exact_count}")
    print(f"daily_assignment_substitution={report.substitution_count}")
    print(f"daily_assignment_duplicate={report.duplicate_count}")
    print(
        "daily_assignment_historical_duplicate_preserved="
        f"{report.historical_duplicate_count}"
    )
    print(f"daily_assignment_slot_without_target={report.slot_without_target_count}")
    print(f"daily_assignment_incompatible_target={report.incompatible_target_count}")
    print(f"daily_assignment_contract_errors={len(report.errors)}")
    for error in report.errors:
        print(f"daily_assignment_error={error}")
    if report.errors and not args.dry_run:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
