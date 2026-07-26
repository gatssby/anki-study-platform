#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sqlite_safe_backup import validate_sqlite_database


@dataclass(frozen=True)
class StateDifference:
    category: str
    key: str
    baseline: object
    candidate: object


@dataclass(frozen=True)
class StateComparison:
    safe: bool
    differences: tuple[StateDifference, ...]
    baseline_counts: dict[str, int]
    candidate_counts: dict[str, int]
    referenced_uploads: tuple[str, ...]
    missing_uploads: tuple[str, ...]


REQUIRED_TABLES = {
    "lessons",
    "exercise_tasks",
    "daily_assignments",
    "un_daily_assignments",
    "schedule_settings",
    "schedule_unavailability",
    "app_settings",
    "review_questions",
    "review_question_attempts",
}


def connect_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def row_map(
    conn: sqlite3.Connection,
    table: str,
    key_columns: tuple[str, ...],
    *,
    where: str = "",
) -> dict[tuple[object, ...], dict[str, object]]:
    query = f'SELECT * FROM "{table}"' + (f" WHERE {where}" if where else "")
    rows = [dict(row) for row in conn.execute(query).fetchall()]
    return {tuple(row[column] for column in key_columns): row for row in rows}


def stable_row(row: dict[str, object], *, ignored: set[str] | None = None) -> dict[str, object]:
    ignored_columns = ignored or set()
    return {key: row[key] for key in sorted(row) if key not in ignored_columns}


def compare_baseline_subset(
    differences: list[StateDifference],
    *,
    category: str,
    baseline: dict[tuple[object, ...], dict[str, object]],
    candidate: dict[tuple[object, ...], dict[str, object]],
    ignored: set[str] | None = None,
) -> None:
    for key, baseline_row in baseline.items():
        key_text = "|".join(str(value) for value in key)
        candidate_row = candidate.get(key)
        if candidate_row is None:
            differences.append(StateDifference(category, key_text, stable_row(baseline_row), None))
            continue
        baseline_state = stable_row(baseline_row, ignored=ignored)
        candidate_state = stable_row(candidate_row, ignored=ignored)
        if baseline_state != candidate_state:
            differences.append(StateDifference(category, key_text, baseline_state, candidate_state))


def compare_exact_table(
    differences: list[StateDifference],
    *,
    table: str,
    baseline: dict[tuple[object, ...], dict[str, object]],
    candidate: dict[tuple[object, ...], dict[str, object]],
) -> None:
    compare_baseline_subset(
        differences,
        category=table,
        baseline=baseline,
        candidate=candidate,
    )
    for key, candidate_row in candidate.items():
        if key not in baseline:
            differences.append(
                StateDifference(
                    table,
                    "|".join(str(value) for value in key),
                    None,
                    stable_row(candidate_row),
                )
            )


def compare_exact_state(
    differences: list[StateDifference],
    *,
    category: str,
    baseline: dict[tuple[object, ...], dict[str, object]],
    candidate: dict[tuple[object, ...], dict[str, object]],
    ignored: set[str] | None = None,
) -> None:
    compare_baseline_subset(
        differences,
        category=category,
        baseline=baseline,
        candidate=candidate,
        ignored=ignored,
    )
    for key, candidate_row in candidate.items():
        if key not in baseline:
            differences.append(
                StateDifference(
                    category,
                    "|".join(str(value) for value in key),
                    None,
                    stable_row(candidate_row, ignored=ignored),
                )
            )


def referenced_upload_paths(conn: sqlite3.Connection) -> set[str]:
    references: set[str] = set()
    for row in conn.execute(
        "SELECT question_image_path, question_image_paths, answer_image_path FROM review_questions"
    ).fetchall():
        for value in (row["question_image_path"], row["answer_image_path"]):
            if value:
                references.add(str(value))
        raw_paths = row["question_image_paths"]
        if raw_paths:
            try:
                parsed = json.loads(str(raw_paths))
            except json.JSONDecodeError:
                references.add(str(raw_paths))
            else:
                if isinstance(parsed, list):
                    references.update(str(value) for value in parsed if value)
    return references


def missing_upload_paths(references: set[str], uploads_root: Path) -> list[str]:
    root = uploads_root.expanduser().resolve()
    missing: list[str] = []
    for reference in sorted(references):
        relative = Path(reference)
        if relative.is_absolute():
            missing.append(reference)
            continue
        resolved = (root / relative).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            missing.append(reference)
            continue
        if not resolved.is_file():
            missing.append(reference)
    return missing


def compare_database_state(
    baseline_db: str | Path,
    candidate_db: str | Path,
    *,
    uploads_root: str | Path,
) -> StateComparison:
    baseline_path = Path(baseline_db).expanduser().resolve()
    candidate_path = Path(candidate_db).expanduser().resolve()
    validate_sqlite_database(baseline_path)
    validate_sqlite_database(candidate_path)
    baseline_conn = connect_read_only(baseline_path)
    candidate_conn = connect_read_only(candidate_path)
    differences: list[StateDifference] = []
    try:
        for label, conn in (("baseline", baseline_conn), ("candidate", candidate_conn)):
            missing_tables = sorted(REQUIRED_TABLES - table_names(conn))
            if missing_tables:
                differences.append(
                    StateDifference("schema", label, sorted(REQUIRED_TABLES), {"missing": missing_tables})
                )
        if any(difference.category == "schema" for difference in differences):
            return StateComparison(False, tuple(differences), {}, {}, (), ())

        baseline_counts = {
            table: int(baseline_conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in sorted(REQUIRED_TABLES)
        }
        candidate_counts = {
            table: int(candidate_conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in sorted(REQUIRED_TABLES)
        }

        compare_baseline_subset(
            differences,
            category="lesson_identity",
            baseline=row_map(baseline_conn, "lessons", ("lesson_code",)),
            candidate=row_map(candidate_conn, "lessons", ("lesson_code",)),
            ignored={
                "title_raw", "portal_title", "relative_path", "external_url",
                "duration_seconds", "subject_name", "subject_prefix", "module_label",
                "module_number", "lesson_number", "week_number", "day_index", "day_name",
                "slot_index", "recommended_date", "is_seen", "seen_at", "is_cut",
                "cut_reason", "cut_source", "source_sheet", "created_at", "updated_at",
            },
        )
        compare_exact_state(
            differences,
            category="seen_lessons",
            baseline=row_map(baseline_conn, "lessons", ("lesson_code",), where="is_seen = 1"),
            candidate=row_map(candidate_conn, "lessons", ("lesson_code",), where="is_seen = 1"),
            ignored=set(REQUIRED_LESSON_NON_SEEN_COLUMNS),
        )
        compare_exact_state(
            differences,
            category="cuts",
            baseline=row_map(baseline_conn, "lessons", ("lesson_code",), where="is_cut = 1"),
            candidate=row_map(candidate_conn, "lessons", ("lesson_code",), where="is_cut = 1"),
            ignored=set(REQUIRED_LESSON_NON_CUT_COLUMNS),
        )
        compare_baseline_subset(
            differences,
            category="exercise_tasks",
            baseline=row_map(baseline_conn, "exercise_tasks", ("source_lesson_code",)),
            candidate=row_map(candidate_conn, "exercise_tasks", ("source_lesson_code",)),
            ignored={"id", "created_at", "updated_at"},
        )
        compare_baseline_subset(
            differences,
            category="daily_assignments",
            baseline=row_map(baseline_conn, "daily_assignments", ("dashboard_date", "planned_slot_key")),
            candidate=row_map(candidate_conn, "daily_assignments", ("dashboard_date", "planned_slot_key")),
            ignored={"created_at", "updated_at"},
        )
        compare_baseline_subset(
            differences,
            category="un_daily_assignments",
            baseline=row_map(baseline_conn, "un_daily_assignments", ("dashboard_date", "row_index")),
            candidate=row_map(candidate_conn, "un_daily_assignments", ("dashboard_date", "row_index")),
            ignored={"created_at", "updated_at"},
        )
        for table, key in (
            ("schedule_settings", ("id",)),
            ("schedule_unavailability", ("id",)),
            ("app_settings", ("setting_key",)),
        ):
            compare_exact_table(
                differences,
                table=table,
                baseline=row_map(baseline_conn, table, key),
                candidate=row_map(candidate_conn, table, key),
            )
        compare_baseline_subset(
            differences,
            category="review_questions",
            baseline=row_map(baseline_conn, "review_questions", ("id",)),
            candidate=row_map(candidate_conn, "review_questions", ("id",)),
        )
        compare_baseline_subset(
            differences,
            category="review_question_attempts",
            baseline=row_map(baseline_conn, "review_question_attempts", ("id",)),
            candidate=row_map(candidate_conn, "review_question_attempts", ("id",)),
        )

        references = referenced_upload_paths(candidate_conn) | referenced_upload_paths(baseline_conn)
        missing_uploads = missing_upload_paths(references, Path(uploads_root))
        for reference in missing_uploads:
            differences.append(StateDifference("referenced_upload", reference, "present", "missing"))
        return StateComparison(
            safe=not differences,
            differences=tuple(differences),
            baseline_counts=baseline_counts,
            candidate_counts=candidate_counts,
            referenced_uploads=tuple(sorted(references)),
            missing_uploads=tuple(missing_uploads),
        )
    finally:
        candidate_conn.close()
        baseline_conn.close()


REQUIRED_LESSON_NON_SEEN_COLUMNS = {
    "slot_key", "track_code", "lesson_type", "title_raw", "portal_title",
    "relative_path", "external_url", "duration_seconds", "subject_name",
    "subject_prefix", "module_label", "module_number", "lesson_number",
    "week_number", "day_index", "day_name", "slot_index", "recommended_date",
    "is_cut", "cut_reason", "cut_source", "source_sheet", "created_at", "updated_at",
}
REQUIRED_LESSON_NON_CUT_COLUMNS = {
    "slot_key", "track_code", "lesson_type", "title_raw", "portal_title",
    "relative_path", "external_url", "duration_seconds", "subject_name",
    "subject_prefix", "module_label", "module_number", "lesson_number",
    "week_number", "day_index", "day_name", "slot_index", "recommended_date",
    "is_seen", "seen_at", "source_sheet", "created_at", "updated_at",
}


def write_report(report: StateComparison, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "safe": report.safe,
                "baseline_counts": report.baseline_counts,
                "candidate_counts": report.candidate_counts,
                "referenced_uploads": report.referenced_uploads,
                "missing_uploads": report.missing_uploads,
                "differences": [asdict(difference) for difference in report.differences],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compara estado protegido antes de --with-db")
    parser.add_argument("--baseline-db", required=True)
    parser.add_argument("--candidate-db", required=True)
    parser.add_argument("--uploads-root", required=True)
    parser.add_argument("--report", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        comparison = compare_database_state(
            args.baseline_db,
            args.candidate_db,
            uploads_root=args.uploads_root,
        )
        report_path = Path(args.report).expanduser().resolve()
        write_report(comparison, report_path)
    except (RuntimeError, sqlite3.DatabaseError) as exc:
        print(f"state_comparison_error={exc}")
        return 1
    print(f"state_comparison_report={report_path}")
    print(f"state_comparison_safe={comparison.safe}")
    print(f"state_comparison_differences={len(comparison.differences)}")
    print(f"referenced_uploads={len(comparison.referenced_uploads)}")
    print(f"missing_uploads={len(comparison.missing_uploads)}")
    for difference in comparison.differences[:50]:
        print(f"difference={difference.category}|{difference.key}")
    return 0 if comparison.safe else 10


if __name__ == "__main__":
    raise SystemExit(main())
