from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .backup import create_timestamped_backup, extract_backup_database
from .db import DEFAULT_DB_PATH
from .exercises import reschedule_unmoved_exercise_tasks, sync_fo_exercise_tasks
from .fo_planner import _pedagogical_order, build_fo_plan, select_eligible_fo_lessons


PORTUGUESE_DAY_NAMES = {
    1: "Segunda",
    2: "Terça",
    3: "Quarta",
    4: "Quinta",
    5: "Sexta",
    6: "Sábado",
    7: "Domingo",
}

TRACK_ORDER = {"FO": 0, "UN": 1}
DEFAULT_WEEKDAY_MINUTES = 240
DEFAULT_SATURDAY_MINUTES = 180
DEFAULT_SUNDAY_MINUTES = 180
DEFAULT_FO_LESSON_MINUTES = 45
DEFAULT_UN_LESSON_MINUTES = 10
TRACK_DATE_SOURCES = {
    "FO": {
        "source_table": "lessons",
        "source_field": "recommended_date",
        "notes": "Fonte real da agenda FO no dashboard.",
    },
    "UN": {
        "source_table": "lessons",
        "source_field": "recommended_date",
        "notes": "Fonte real da agenda UN; un_daily_assignments e cache diario da home.",
    },
}


@dataclass(frozen=True)
class ScheduleSettings:
    exam_date: date | None = None
    target_finish_date: date | None = None
    finish_offset_days_before_exam: int | None = None
    include_weekends: bool = False
    include_vacations: bool = True
    cut_review_free: bool = True
    preserve_english_cut: bool = True
    auto_adapt_enabled: bool = True
    max_daily_minutes_weekday: int = DEFAULT_WEEKDAY_MINUTES
    max_daily_minutes_saturday: int = DEFAULT_SATURDAY_MINUTES
    max_daily_minutes_sunday: int = DEFAULT_SUNDAY_MINUTES

    def effective_target_finish_date(self) -> date | None:
        if self.target_finish_date:
            return self.target_finish_date
        if self.exam_date and self.finish_offset_days_before_exam is not None:
            return self.exam_date - timedelta(days=self.finish_offset_days_before_exam)
        return None


@dataclass(frozen=True)
class ScheduleUnavailability:
    id: int
    start_date: date
    end_date: date
    capacity_percent: int
    reason: str | None = None


@dataclass
class LessonCandidate:
    lesson_code: str
    track_code: str
    subject_prefix: str
    group_label: str
    weight_units: int
    original_order: int
    current_date: date
    row: dict[str, Any]


@dataclass
class PlannedAssignment:
    lesson: LessonCandidate
    date_value: date


@dataclass
class PlannedDay:
    date_value: date
    is_available: bool
    capacity_units: int
    effective_capacity_percent: int
    assignments: list[PlannedAssignment] = field(default_factory=list)

    @property
    def assigned_units(self) -> int:
        return sum(item.lesson.weight_units for item in self.assignments)

    @property
    def overflow_units(self) -> int:
        return max(self.assigned_units - self.capacity_units, 0)


@dataclass
class ReprogramReport:
    settings: ScheduleSettings
    as_of_date: date
    target_finish_date: date
    exam_date: date | None
    available_days: list[PlannedDay]
    explicit_unavailability: list[ScheduleUnavailability]
    total_remaining_units: int
    remaining_units_by_track: dict[str, int]
    remaining_lesson_count_by_track: dict[str, int]
    distributed_lesson_count_by_track: dict[str, int]
    unallocated_lesson_count_by_track: dict[str, int]
    duration_diagnostics: dict[str, dict[str, int]]
    cut_summary: dict[str, int]
    weekly_distribution: list[dict[str, Any]]
    track_distribution: list[dict[str, Any]]
    group_distribution: list[dict[str, Any]]
    first_days: list[dict[str, Any]]
    last_days: list[dict[str, Any]]
    required_average_units: float
    total_capacity_units: int
    capacity_deficit_units: int
    overflow_days: list[dict[str, Any]]
    assignment_count: int
    pending_lesson_count: int
    available_day_count: int
    unavailable_day_count: int
    simulation_token: str
    fo_plan_summary: dict[str, Any] = field(default_factory=dict)
    lesson_order_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    distribution_diagnostics: dict[str, Any] = field(default_factory=dict)
    validation_errors: list[str] = field(default_factory=list)
    backup_path: Path | None = None

    @property
    def feasible(self) -> bool:
        return (
            self.assignment_count == self.pending_lesson_count
            and not any(self.unallocated_lesson_count_by_track.values())
            and not self.validation_errors
        )

    @property
    def average_lessons_per_day(self) -> float:
        return self.assignment_count / self.available_day_count if self.available_day_count else 0.0

    @property
    def average_minutes_per_day(self) -> float:
        return self.total_remaining_units / self.available_day_count if self.available_day_count else 0.0

    @property
    def max_daily_load_units(self) -> int:
        return max((day.assigned_units for day in self.available_days if day.is_available), default=0)

    @property
    def min_daily_load_units(self) -> int:
        return min((day.assigned_units for day in self.available_days if day.is_available), default=0)


def current_local_date() -> date:
    try:
        tz = ZoneInfo("America/Sao_Paulo")
    except Exception:
        tz = timezone(timedelta(hours=-3))
    return datetime.now(tz).date()


def get_schedule_settings(conn: sqlite3.Connection) -> ScheduleSettings:
    row = conn.execute(
        """
        SELECT
            exam_date,
            target_finish_date,
            finish_offset_days_before_exam,
            include_weekends,
            include_vacations,
            cut_review_free,
            preserve_english_cut,
            auto_adapt_enabled,
            max_daily_minutes_weekday,
            max_daily_minutes_saturday,
            max_daily_minutes_sunday
        FROM schedule_settings
        WHERE id = 1
        """
    ).fetchone()
    if not row:
        return normalized_settings(ScheduleSettings())

    return normalized_settings(
        ScheduleSettings(
            exam_date=parse_date(row["exam_date"]),
            target_finish_date=parse_date(row["target_finish_date"]),
            finish_offset_days_before_exam=parse_optional_int(row["finish_offset_days_before_exam"]),
            include_weekends=bool(row["include_weekends"]),
            include_vacations=bool(row["include_vacations"]),
            cut_review_free=bool(row["cut_review_free"]),
            preserve_english_cut=bool(row["preserve_english_cut"]),
            auto_adapt_enabled=bool(row["auto_adapt_enabled"]),
            max_daily_minutes_weekday=parse_optional_int(row["max_daily_minutes_weekday"]) or DEFAULT_WEEKDAY_MINUTES,
            max_daily_minutes_saturday=parse_optional_int(row["max_daily_minutes_saturday"]) or DEFAULT_SATURDAY_MINUTES,
            max_daily_minutes_sunday=parse_optional_int(row["max_daily_minutes_sunday"]) or DEFAULT_SUNDAY_MINUTES,
        )
    )


def save_schedule_settings(conn: sqlite3.Connection, settings: ScheduleSettings) -> None:
    normalized = normalized_settings(settings)
    conn.execute(
        """
        INSERT INTO schedule_settings (
            id,
            exam_date,
            target_finish_date,
            finish_offset_days_before_exam,
            include_weekends,
            include_vacations,
            cut_review_free,
            preserve_english_cut,
            auto_adapt_enabled,
            max_daily_minutes_weekday,
            max_daily_minutes_saturday,
            max_daily_minutes_sunday,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            exam_date = excluded.exam_date,
            target_finish_date = excluded.target_finish_date,
            finish_offset_days_before_exam = excluded.finish_offset_days_before_exam,
            include_weekends = excluded.include_weekends,
            include_vacations = excluded.include_vacations,
            cut_review_free = excluded.cut_review_free,
            preserve_english_cut = excluded.preserve_english_cut,
            auto_adapt_enabled = excluded.auto_adapt_enabled,
            max_daily_minutes_weekday = excluded.max_daily_minutes_weekday,
            max_daily_minutes_saturday = excluded.max_daily_minutes_saturday,
            max_daily_minutes_sunday = excluded.max_daily_minutes_sunday,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            1,
            format_date(normalized.exam_date),
            format_date(normalized.target_finish_date),
            normalized.finish_offset_days_before_exam,
            int(normalized.include_weekends),
            int(normalized.include_vacations),
            int(normalized.cut_review_free),
            int(normalized.preserve_english_cut),
            int(normalized.auto_adapt_enabled),
            normalized.max_daily_minutes_weekday,
            normalized.max_daily_minutes_saturday,
            normalized.max_daily_minutes_sunday,
        ),
    )


def normalized_settings(settings: ScheduleSettings) -> ScheduleSettings:
    target_finish = settings.effective_target_finish_date()
    return ScheduleSettings(
        exam_date=settings.exam_date,
        target_finish_date=target_finish,
        finish_offset_days_before_exam=None,
        include_weekends=bool(settings.include_weekends),
        include_vacations=True,
        cut_review_free=True,
        preserve_english_cut=True,
        auto_adapt_enabled=bool(settings.auto_adapt_enabled),
        max_daily_minutes_weekday=settings.max_daily_minutes_weekday,
        max_daily_minutes_saturday=settings.max_daily_minutes_saturday,
        max_daily_minutes_sunday=settings.max_daily_minutes_sunday,
    )


def list_unavailability(conn: sqlite3.Connection) -> list[ScheduleUnavailability]:
    rows = conn.execute(
        """
        SELECT id, start_date, end_date, capacity_percent, reason
        FROM schedule_unavailability
        ORDER BY start_date, end_date, id
        """
    ).fetchall()
    return [
        ScheduleUnavailability(
            id=int(row["id"]),
            start_date=date.fromisoformat(row["start_date"]),
            end_date=date.fromisoformat(row["end_date"]),
            capacity_percent=int(row["capacity_percent"]),
            reason=(row["reason"] or "").strip() or None,
        )
        for row in rows
    ]


def add_unavailability(
    conn: sqlite3.Connection,
    start_date_value: date,
    end_date_value: date,
    capacity_percent: int,
    reason: str | None = None,
) -> int:
    normalized_capacity = max(0, min(int(capacity_percent), 100))
    existing = conn.execute(
        """
        SELECT id
        FROM schedule_unavailability
        WHERE start_date = ?
          AND end_date = ?
        LIMIT 1
        """,
        (start_date_value.isoformat(), end_date_value.isoformat()),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE schedule_unavailability
            SET capacity_percent = ?,
                reason = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (normalized_capacity, (reason or "").strip() or None, existing["id"]),
        )
        return int(existing["id"])

    cursor = conn.execute(
        """
        INSERT INTO schedule_unavailability (
            start_date,
            end_date,
            capacity_percent,
            reason,
            updated_at
        ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            start_date_value.isoformat(),
            end_date_value.isoformat(),
            normalized_capacity,
            (reason or "").strip() or None,
        ),
    )
    return int(cursor.lastrowid)


def remove_unavailability(conn: sqlite3.Connection, entry_id: int) -> int:
    cursor = conn.execute("DELETE FROM schedule_unavailability WHERE id = ?", (entry_id,))
    return int(cursor.rowcount or 0)


def list_cut_lessons(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT lesson_code, track_code, lesson_type, subject_prefix, module_label, title_raw, is_cut, cut_reason, cut_source
        FROM lessons
        WHERE is_cut = 1
        ORDER BY recommended_date, slot_index, CASE track_code WHEN 'FO' THEN 0 ELSE 1 END, lesson_code
        """
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def cut_lesson(conn: sqlite3.Connection, lesson_code: str, reason: str | None = None) -> int:
    cursor = conn.execute(
        """
        UPDATE lessons
        SET is_cut = 1,
            cut_source = 'manual',
            cut_reason = COALESCE(?, 'manual'),
            updated_at = CURRENT_TIMESTAMP
        WHERE lesson_code = ?
        """,
        ((reason or "").strip() or None, lesson_code),
    )
    return int(cursor.rowcount or 0)


def uncut_lesson(conn: sqlite3.Connection, lesson_code: str) -> int:
    cursor = conn.execute(
        """
        UPDATE lessons
        SET is_cut = 0,
            cut_source = NULL,
            cut_reason = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE lesson_code = ?
        """,
        (lesson_code,),
    )
    return int(cursor.rowcount or 0)


def build_reprogram_report(
    conn: sqlite3.Connection,
    settings: ScheduleSettings,
    as_of_date: date | None = None,
    diagnostic_lesson_prefixes: tuple[str, ...] = (),
) -> ReprogramReport:
    effective_as_of = as_of_date or current_local_date()
    normalized = normalized_settings(settings)
    effective_target = normalized.effective_target_finish_date()
    if effective_target is None:
        raise ValueError("Defina a data-alvo para terminar todas as aulas antes da simulação.")
    if effective_target < effective_as_of:
        raise ValueError(
            f"Data-alvo {effective_target.isoformat()} anterior à data atual {effective_as_of.isoformat()}."
        )

    unavailability = list_unavailability(conn)
    rows = fetch_schedulable_rows(conn)
    fo_rows = [row for row in rows if row["track_code"] == "FO"]
    un_rows = [row for row in rows if row["track_code"] == "UN"]
    fo_lessons, _, _ = select_eligible_fo_lessons(fo_rows)
    row_by_code = {row["lesson_code"]: row for row in fo_rows}
    ordered_fo_lessons = _pedagogical_order(fo_lessons, effective_as_of)
    fo_candidates = [
        LessonCandidate(
            lesson_code=lesson.lesson_code,
            track_code="FO",
            subject_prefix=lesson.subject_prefix,
            group_label=build_group_label(row_by_code[lesson.lesson_code]),
            weight_units=lesson.minutes,
            original_order=index,
            current_date=max(lesson.recommended_date, effective_as_of),
            row=row_by_code[lesson.lesson_code],
        )
        for index, lesson in enumerate(ordered_fo_lessons, start=1)
    ]
    un_candidates, un_cut_summary = build_lesson_candidates(rows=un_rows, as_of_date=effective_as_of)
    _, fo_cut_summary = build_lesson_candidates(rows=fo_rows, as_of_date=effective_as_of)
    cut_summary = {key: fo_cut_summary.get(key, 0) + un_cut_summary.get(key, 0) for key in fo_cut_summary}
    planned_days = build_planned_days(
        settings=normalized,
        as_of_date=effective_as_of,
        target_finish_date=effective_target,
        unavailability=unavailability,
    )
    if not any(day.is_available for day in planned_days):
        raise ValueError(
            "Nenhum dia disponível entre a data de referência e a data-alvo. "
            "Revise fins de semana e indisponibilidades reais."
        )
    capacity_by_date: dict[date, int] = {}
    for entry in unavailability:
        current = entry.start_date
        while current <= entry.end_date:
            capacity_by_date[current] = min(capacity_by_date.get(current, 100), entry.capacity_percent)
            current += timedelta(days=1)
    fo_plan = build_fo_plan(
        fo_lessons,
        start_date=effective_as_of,
        end_date=effective_target,
        include_weekends=normalized.include_weekends,
        capacity_percent_by_date=capacity_by_date,
        max_daily_minutes_weekday=normalized.max_daily_minutes_weekday,
        max_daily_minutes_saturday=normalized.max_daily_minutes_saturday,
        max_daily_minutes_sunday=normalized.max_daily_minutes_sunday,
    )
    candidates = fo_candidates + un_candidates
    unallocated_candidates = distribute_tracks_without_limits(
        fo_candidates=fo_candidates,
        un_candidates=un_candidates,
        planned_days=planned_days,
    )
    total_remaining_units = sum(candidate.weight_units for candidate in candidates)
    available_day_count = sum(1 for day in planned_days if day.is_available)
    unavailable_day_count = len(planned_days) - available_day_count

    remaining_units_by_track = summarize_remaining_units_by_track(candidates)
    remaining_lesson_count_by_track = summarize_candidate_counts_by_track(candidates)
    distributed_lesson_count_by_track = summarize_candidate_counts_by_track(
        [assignment.lesson for day in planned_days for assignment in day.assignments]
    )
    unallocated_lesson_count_by_track = summarize_candidate_counts_by_track(unallocated_candidates)
    duration_diagnostics = summarize_duration_diagnostics(candidates)
    generated_days = [day for day in planned_days if day.assignments]
    required_average_units = total_remaining_units / available_day_count if available_day_count else 0.0
    distribution_diagnostics = build_planned_distribution_diagnostics(
        planned_days=planned_days,
        as_of_date=effective_as_of,
        target_finish_date=effective_target,
    )
    validation_errors = validate_distribution_diagnostics(
        distribution_diagnostics,
        expected_track_counts=summarize_candidate_counts_by_track(candidates),
    )
    validation_errors.extend(
        validate_planned_pedagogical_order(
            ordered_fo_lessons=ordered_fo_lessons,
            planned_days=planned_days,
        )
    )
    fo_plan_summary = {
        "scope": "standalone_fo_only_without_un_capacity_competition",
        "standalone_is_feasible": fo_plan.is_feasible,
        "capacity_mode": fo_plan.capacity_mode,
        "configured_daily_minutes_ignored": True,
        "total_load_seconds": fo_plan.total_load_seconds,
        "estimated_duration_lesson_count": len(fo_plan.estimated_duration_lesson_codes),
        "estimated_duration_lesson_codes": list(fo_plan.estimated_duration_lesson_codes),
        "total_capacity_seconds": fo_plan.total_capacity_seconds,
        "deficit_seconds": fo_plan.deficit_seconds,
        "raw_capacity_deficit_seconds": fo_plan.raw_capacity_deficit_seconds,
        "allocated_lesson_count": len(fo_plan.assignments),
        "standalone_unallocated_lesson_count": len(fo_plan.unallocated_lessons),
        "standalone_unallocated_lessons": [
            {
                "lesson_code": item.lesson.lesson_code,
                "duration_seconds": item.duration_seconds,
                "max_capacity_seconds": item.max_capacity_seconds,
                "reason": item.reason,
            }
            for item in fo_plan.unallocated_lessons
        ],
        "first_date": fo_plan.first_date.isoformat() if fo_plan.first_date else None,
        "last_date": fo_plan.last_date.isoformat() if fo_plan.last_date else None,
        "empty_days": fo_plan.empty_day_count,
        "max_daily_load_seconds": fo_plan.max_daily_load_seconds,
        "daily_summary": fo_plan.daily_summary,
    }
    if unallocated_candidates:
        validation_errors.append(
            "Plano compartilhado FO + UN inviável: "
            f"FO={unallocated_lesson_count_by_track.get('FO', 0)} e "
            f"UN={unallocated_lesson_count_by_track.get('UN', 0)} aulas não alocadas; "
            f"total={len(unallocated_candidates)}."
        )
    total_capacity_units = sum(day.capacity_units for day in planned_days)
    # Kept at zero for legacy consumers: configured minutes are not capacity.
    capacity_deficit_units = 0
    overflow_days = find_overflow_days(planned_days)
    fo_plan_summary["shared_plan_unallocated_lesson_count"] = len(unallocated_candidates)
    fo_plan_summary["shared_plan_unallocated_by_track"] = unallocated_lesson_count_by_track
    lesson_order_diagnostics = build_lesson_order_diagnostics(
        rows=fo_rows,
        ordered_fo_lessons=ordered_fo_lessons,
        planned_days=planned_days,
        unallocated_candidates=unallocated_candidates,
        prefixes=diagnostic_lesson_prefixes,
    )
    simulation_token = build_simulation_token(
        settings=normalized,
        unavailability=unavailability,
        as_of_date=effective_as_of,
    )

    return ReprogramReport(
        settings=normalized,
        as_of_date=effective_as_of,
        target_finish_date=effective_target,
        exam_date=normalized.exam_date,
        available_days=planned_days,
        explicit_unavailability=unavailability,
        total_remaining_units=total_remaining_units,
        remaining_units_by_track=remaining_units_by_track,
        remaining_lesson_count_by_track=remaining_lesson_count_by_track,
        distributed_lesson_count_by_track=distributed_lesson_count_by_track,
        unallocated_lesson_count_by_track=unallocated_lesson_count_by_track,
        duration_diagnostics=duration_diagnostics,
        cut_summary=cut_summary,
        weekly_distribution=summarize_weekly_distribution(generated_days),
        track_distribution=summarize_track_distribution(generated_days),
        group_distribution=summarize_group_distribution(generated_days),
        first_days=summarize_edge_days(generated_days[:14]),
        last_days=summarize_edge_days(generated_days[-14:]) if len(generated_days) > 14 else summarize_edge_days(generated_days),
        required_average_units=required_average_units,
        total_capacity_units=total_capacity_units,
        capacity_deficit_units=capacity_deficit_units,
        overflow_days=overflow_days,
        assignment_count=sum(len(day.assignments) for day in planned_days),
        pending_lesson_count=len(candidates),
        available_day_count=available_day_count,
        unavailable_day_count=unavailable_day_count,
        simulation_token=simulation_token,
        fo_plan_summary=fo_plan_summary,
        lesson_order_diagnostics=lesson_order_diagnostics,
        distribution_diagnostics=distribution_diagnostics,
        validation_errors=validation_errors,
    )


def apply_reprogramming(
    conn: sqlite3.Connection,
    settings: ScheduleSettings,
    as_of_date: date | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    diagnostic_lesson_prefixes: tuple[str, ...] = (),
) -> ReprogramReport:
    report = build_reprogram_report(
        conn=conn,
        settings=settings,
        as_of_date=as_of_date,
        diagnostic_lesson_prefixes=diagnostic_lesson_prefixes,
    )
    if not report.feasible:
        raise ValueError(
            "Aplicação recusada porque a distribuição contém erros estruturais."
        )
    backup_dir = Path(db_path).expanduser().resolve().parent / "backups"
    backup_path = create_timestamped_backup(
        db_path=db_path,
        backup_dir=backup_dir,
        prefix="cronograma-pre-reprogram-",
    )

    persist_auto_cuts(conn)
    save_schedule_settings(conn, report.settings)
    write_schedule_dates(conn=conn, planned_days=report.available_days)
    conn.execute("DELETE FROM daily_assignments")
    conn.execute("DELETE FROM un_daily_assignments")
    persisted_schedule_errors = validate_persisted_schedule(
        conn=conn,
        planned_days=report.available_days,
    )
    sync_fo_exercise_tasks(conn)
    reschedule_unmoved_exercise_tasks(conn)
    report.distribution_diagnostics = diagnose_distribution(
        conn=conn,
        as_of_date=report.as_of_date,
        target_finish_date=report.target_finish_date,
    )
    report.validation_errors = validate_distribution_diagnostics(
        report.distribution_diagnostics,
        expected_track_counts={
            row["track_code"]: row["lesson_count"]
            for row in report.track_distribution
        },
    )
    report.validation_errors.extend(persisted_schedule_errors)
    if report.validation_errors:
        raise ValueError(
            "Validacao pos-aplicacao falhou: " + "; ".join(report.validation_errors)
        )
    report.backup_path = backup_path
    return report


def auto_adapt_if_enabled(
    db_path: str | Path = DEFAULT_DB_PATH,
    as_of_date: date | None = None,
) -> tuple[bool, str]:
    return False, "manual-only"


def validate_backup_against_source(
    conn: sqlite3.Connection,
    backup_path: str | Path,
) -> dict[str, Any]:
    with extract_backup_database(backup_path) as extracted_db_path:
        backup_conn = sqlite3.connect(extracted_db_path)
        backup_conn.row_factory = sqlite3.Row
        try:
            backup_tables = {
                row["name"]
                for row in backup_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            current_seen = scalar_int(conn, "SELECT COUNT(*) FROM lessons WHERE is_seen = 1")
            backup_seen = scalar_int(backup_conn, "SELECT COUNT(*) FROM lessons WHERE is_seen = 1")
            current_done = scalar_int(conn, "SELECT COUNT(*) FROM exercise_tasks WHERE status = 'done'")
            backup_done = (
                scalar_int(backup_conn, "SELECT COUNT(*) FROM exercise_tasks WHERE status = 'done'")
                if "exercise_tasks" in backup_tables
                else None
            )
            current_tasks = scalar_int(conn, "SELECT COUNT(*) FROM exercise_tasks")
            backup_tasks = (
                scalar_int(backup_conn, "SELECT COUNT(*) FROM exercise_tasks")
                if "exercise_tasks" in backup_tables
                else None
            )
        finally:
            backup_conn.close()

    return {
        "current_seen_lessons": current_seen,
        "backup_seen_lessons": backup_seen,
        "current_exercise_tasks": current_tasks,
        "backup_exercise_tasks": backup_tasks,
        "current_done_exercises": current_done,
        "backup_done_exercises": backup_done,
        "matches": current_seen == backup_seen and current_tasks == backup_tasks and current_done == backup_done,
    }


def fetch_schedulable_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM lessons
        WHERE (track_code = 'FO' AND lesson_type IN ('lesson', 'review'))
           OR (track_code = 'UN' AND lesson_type IN ('lesson', 'list'))
        ORDER BY recommended_date, slot_index, CASE track_code WHEN 'FO' THEN 0 ELSE 1 END, lesson_code
        """
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def build_lesson_candidates(
    rows: list[dict[str, Any]],
    as_of_date: date,
) -> tuple[list[LessonCandidate], dict[str, int]]:
    candidates: list[LessonCandidate] = []
    cut_summary = {
        "manual": 0,
        "review_free": 0,
        "english": 0,
        "already_cut": 0,
    }

    for original_order, row in enumerate(rows, start=1):
        cut_flags = derive_cut_flags(row)
        if cut_flags["manual"]:
            cut_summary["manual"] += 1
        elif cut_flags["review_free"]:
            cut_summary["review_free"] += 1
        elif cut_flags["english"]:
            cut_summary["english"] += 1
        elif cut_flags["already_cut"]:
            cut_summary["already_cut"] += 1

        if row.get("is_seen") == 1:
            continue
        if cut_flags["effective"]:
            continue

        candidates.append(
            LessonCandidate(
                lesson_code=row["lesson_code"],
                track_code=row["track_code"],
                subject_prefix=(row.get("subject_prefix") or row["track_code"]).strip() or row["track_code"],
                group_label=build_group_label(row),
                weight_units=lesson_weight_units(row),
                original_order=original_order,
                current_date=max(date.fromisoformat(row["recommended_date"]), as_of_date),
                row=row,
            )
        )

    return candidates, cut_summary


def build_planned_days(
    settings: ScheduleSettings,
    as_of_date: date,
    target_finish_date: date,
    unavailability: list[ScheduleUnavailability],
) -> list[PlannedDay]:
    days: list[PlannedDay] = []
    current_day = as_of_date
    while current_day <= target_finish_date:
        is_weekend = current_day.isoweekday() in {6, 7}
        capacity_percent = effective_capacity_percent(current_day, unavailability)
        if is_weekend and not settings.include_weekends:
            capacity_percent = 0
        base_capacity = configured_daily_capacity_minutes(current_day, settings)
        capacity_minutes = max(base_capacity * capacity_percent // 100, 0)
        days.append(
            PlannedDay(
                date_value=current_day,
                is_available=capacity_percent > 0 and (settings.include_weekends or not is_weekend),
                capacity_units=capacity_minutes,
                effective_capacity_percent=capacity_percent,
            )
        )
        current_day += timedelta(days=1)
    return days


def configured_daily_capacity_minutes(day: date, settings: ScheduleSettings) -> int:
    if day.isoweekday() == 6:
        return max(int(settings.max_daily_minutes_saturday), 0)
    if day.isoweekday() == 7:
        return max(int(settings.max_daily_minutes_sunday), 0)
    return max(int(settings.max_daily_minutes_weekday), 0)


def effective_capacity_percent(
    day: date,
    unavailability: list[ScheduleUnavailability],
) -> int:
    matching = [
        max(0, min(int(entry.capacity_percent), 100))
        for entry in unavailability
        if entry.start_date <= day <= entry.end_date
    ]
    return min(matching, default=100)


def distribute_tracks_without_limits(
    fo_candidates: list[LessonCandidate],
    un_candidates: list[LessonCandidate],
    planned_days: list[PlannedDay],
) -> list[LessonCandidate]:
    """Distribute every lesson while preserving each track's internal order."""
    queues = {
        "FO": deque(fo_candidates),
        "UN": deque(un_candidates),
    }
    available_days = [day for day in planned_days if day.is_available]
    if not available_days:
        return list(fo_candidates) + list(un_candidates)

    total_count_by_track = {code: len(queue) for code, queue in queues.items()}
    assigned_count_by_track = {"FO": 0, "UN": 0}
    merged: list[LessonCandidate] = []
    while any(queues.values()):
        available_tracks = [code for code in ("FO", "UN") if queues[code]]
        track_code = min(
            available_tracks,
            key=lambda code: (
                assigned_count_by_track[code] / total_count_by_track[code],
                TRACK_ORDER[code],
            ),
        )
        merged.append(queues[track_code].popleft())
        assigned_count_by_track[track_code] += 1

    quotas = weighted_day_quotas(len(merged), available_days)
    candidate_iter = iter(merged)
    for day, quota in zip(available_days, quotas):
        for _ in range(quota):
            candidate = next(candidate_iter)
            day.assignments.append(PlannedAssignment(candidate, day.date_value))
    return []


def planned_assignment_positions(
    planned_days: list[PlannedDay],
) -> dict[str, tuple[str, int]]:
    """Return the exact date/slot values that ``write_schedule_dates`` persists."""
    positions: dict[str, tuple[str, int]] = {}
    per_track_daily_slots: dict[tuple[str, date], int] = defaultdict(int)
    for day in planned_days:
        for assignment in day.assignments:
            track_code = assignment.lesson.track_code
            slot_key = (track_code, day.date_value)
            per_track_daily_slots[slot_key] += 1
            positions[assignment.lesson.lesson_code] = (
                day.date_value.isoformat(),
                per_track_daily_slots[slot_key],
            )
    return positions


def validate_planned_pedagogical_order(
    *,
    ordered_fo_lessons: list[Any],
    planned_days: list[PlannedDay],
) -> list[str]:
    """Reject any plan that does not consume the canonical FO sequence stably."""
    expected_codes = [lesson.lesson_code for lesson in ordered_fo_lessons]
    actual_codes = [
        assignment.lesson.lesson_code
        for day in planned_days
        for assignment in day.assignments
        if assignment.lesson.track_code == "FO"
    ]
    errors: list[str] = []
    if actual_codes != expected_codes:
        errors.append(
            "A distribuição FO não consumiu integralmente a ordem pedagógica canônica."
        )

    positions = planned_assignment_positions(planned_days)
    for previous_code, current_code in zip(expected_codes, expected_codes[1:]):
        previous_position = positions.get(previous_code)
        current_position = positions.get(current_code)
        if previous_position is None or current_position is None:
            continue
        if previous_position > current_position:
            errors.append(
                "Monotonicidade pedagógica violada: "
                f"{previous_code}={previous_position[0]}/slot-{previous_position[1]} > "
                f"{current_code}={current_position[0]}/slot-{current_position[1]}."
            )
            break
    return errors


def validate_persisted_schedule(
    *,
    conn: sqlite3.Connection,
    planned_days: list[PlannedDay],
) -> list[str]:
    """Read back the Base source of truth and require an exact plan match."""
    expected = planned_assignment_positions(planned_days)
    persisted = {
        row["lesson_code"]: (row["recommended_date"], int(row["slot_index"]))
        for row in conn.execute(
            "SELECT lesson_code, recommended_date, slot_index FROM lessons"
        ).fetchall()
        if row["lesson_code"] in expected
    }
    errors: list[str] = []
    for lesson_code, expected_position in expected.items():
        actual_position = persisted.get(lesson_code)
        if actual_position != expected_position:
            errors.append(
                "Persistência divergente do plano para "
                f"{lesson_code}: esperado={expected_position[0]}/slot-{expected_position[1]} "
                f"persistido={actual_position}."
            )
            break

    for table_name in ("daily_assignments", "un_daily_assignments"):
        remaining = scalar_int(conn, f"SELECT COUNT(*) FROM {table_name}")
        if remaining:
            errors.append(
                f"Cache antigo {table_name} não foi substituído: {remaining} linha(s) restante(s)."
            )
    return errors


def weighted_day_quotas(total_lessons: int, days: list[PlannedDay]) -> list[int]:
    """Allocate lesson counts proportionally; percentages are weights, not caps."""
    if total_lessons <= 0 or not days:
        return [0] * len(days)
    if total_lessons < len(days):
        return [1 if index < total_lessons else 0 for index in range(len(days))]
    quotas = [1] * len(days)
    remainder = total_lessons - len(days)
    weight_total = sum(day.effective_capacity_percent for day in days)
    exact = [remainder * day.effective_capacity_percent / weight_total for day in days]
    floors = [int(value) for value in exact]
    quotas = [base + extra for base, extra in zip(quotas, floors)]
    leftovers = remainder - sum(floors)
    order = sorted(
        range(len(days)),
        key=lambda index: (-(exact[index] - floors[index]), days[index].date_value),
    )
    for index in order[:leftovers]:
        quotas[index] += 1
    return quotas


def find_overflow_days(planned_days: list[PlannedDay]) -> list[dict[str, Any]]:
    return [
        {
            "date": day.date_value.isoformat(),
            "assigned_units": day.assigned_units,
            "capacity_units": day.capacity_units,
            "overflow_units": day.overflow_units,
        }
        for day in planned_days
        if day.overflow_units > 0
    ]


def persist_auto_cuts(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT lesson_code, lesson_type, title_raw, portal_title, subject_name, subject_prefix, is_cut, cut_source, cut_reason
        FROM lessons
        """
    ).fetchall()

    for raw_row in rows:
        row = row_to_dict(raw_row)
        flags = derive_cut_flags(row)
        if flags["manual"]:
            continue
        if flags["review_free"]:
            conn.execute(
                """
                UPDATE lessons
                SET is_cut = 1,
                    cut_source = 'auto',
                    cut_reason = 'review/free',
                    updated_at = CURRENT_TIMESTAMP
                WHERE lesson_code = ?
                """,
                (row["lesson_code"],),
            )
            continue
        if flags["english"]:
            conn.execute(
                """
                UPDATE lessons
                SET is_cut = 1,
                    cut_source = 'auto',
                    cut_reason = 'english',
                    updated_at = CURRENT_TIMESTAMP
                WHERE lesson_code = ?
                """,
                (row["lesson_code"],),
            )
            continue
        conn.execute(
            """
            UPDATE lessons
            SET is_cut = 0,
                cut_source = NULL,
                cut_reason = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE lesson_code = ?
              AND cut_source = 'auto'
              AND cut_reason IN ('review/free', 'english')
            """,
            (row["lesson_code"],),
        )


def write_schedule_dates(conn: sqlite3.Connection, planned_days: list[PlannedDay]) -> None:
    track_start_dates = fetch_track_start_dates(conn)
    positions = planned_assignment_positions(planned_days)

    for day in planned_days:
        if not day.assignments:
            continue
        for assignment in day.assignments:
            track_code = assignment.lesson.track_code
            _, slot_index = positions[assignment.lesson.lesson_code]
            start_date = track_start_dates.get(track_code, day.date_value)
            week_number = ((day.date_value - start_date).days // 7) + 1
            cursor = conn.execute(
                """
                UPDATE lessons
                SET recommended_date = ?,
                    week_number = ?,
                    day_index = ?,
                    day_name = ?,
                    slot_index = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE lesson_code = ?
                """,
                (
                    day.date_value.isoformat(),
                    max(week_number, 1),
                    day.date_value.isoweekday(),
                    PORTUGUESE_DAY_NAMES[day.date_value.isoweekday()],
                    slot_index,
                    assignment.lesson.lesson_code,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "Falha ao persistir a programação de "
                    f"{assignment.lesson.lesson_code}: linhas atualizadas={cursor.rowcount}."
                )


def fetch_track_start_dates(conn: sqlite3.Connection) -> dict[str, date]:
    rows = conn.execute(
        """
        SELECT track_code, MIN(recommended_date) AS min_date
        FROM lessons
        GROUP BY track_code
        """
    ).fetchall()
    return {
        row["track_code"]: date.fromisoformat(row["min_date"])
        for row in rows
        if row["min_date"]
    }


def summarize_weekly_distribution(planned_days: list[PlannedDay]) -> list[dict[str, Any]]:
    summary: dict[date, dict[str, Any]] = {}
    for day in planned_days:
        if not day.assignments:
            continue
        week_start = day.date_value - timedelta(days=day.date_value.weekday())
        bucket = summary.setdefault(
            week_start,
            {"week_start": week_start.isoformat(), "days": 0, "units": 0, "FO": 0, "UN": 0},
        )
        bucket["days"] += 1
        bucket["units"] += day.assigned_units
        for assignment in day.assignments:
            bucket[assignment.lesson.track_code] += assignment.lesson.weight_units
    return [summary[key] for key in sorted(summary)]


def summarize_track_distribution(planned_days: list[PlannedDay]) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for day in planned_days:
        for assignment in day.assignments:
            bucket = totals.setdefault(
                assignment.lesson.track_code,
                {"track_code": assignment.lesson.track_code, "lesson_count": 0, "units": 0},
            )
            bucket["lesson_count"] += 1
            bucket["units"] += assignment.lesson.weight_units
    return [totals[key] for key in sorted(totals, key=lambda value: TRACK_ORDER.get(value, 99))]


def summarize_group_distribution(planned_days: list[PlannedDay]) -> list[dict[str, Any]]:
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for day in planned_days:
        for assignment in day.assignments:
            key = (assignment.lesson.track_code, assignment.lesson.group_label)
            bucket = totals.setdefault(
                key,
                {
                    "track_code": assignment.lesson.track_code,
                    "group_label": assignment.lesson.group_label,
                    "subject_prefix": assignment.lesson.subject_prefix,
                    "lesson_count": 0,
                    "units": 0,
                },
            )
            bucket["lesson_count"] += 1
            bucket["units"] += assignment.lesson.weight_units
    return [
        totals[key]
        for key in sorted(
            totals,
            key=lambda value: (TRACK_ORDER.get(value[0], 99), value[1]),
        )
    ]


def summarize_edge_days(planned_days: list[PlannedDay]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for day in planned_days:
        if not day.assignments:
            continue
        fo_units = sum(item.lesson.weight_units for item in day.assignments if item.lesson.track_code == "FO")
        un_units = sum(item.lesson.weight_units for item in day.assignments if item.lesson.track_code == "UN")
        rows.append(
            {
                "date": day.date_value.isoformat(),
                "lesson_count": len(day.assignments),
                "units": day.assigned_units,
                "capacity_units": day.capacity_units,
                "FO": fo_units,
                "UN": un_units,
            }
        )
    return rows


def summarize_remaining_units_by_track(candidates: list[LessonCandidate]) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    for candidate in candidates:
        totals[candidate.track_code] += candidate.weight_units
    return dict(totals)


def summarize_candidate_counts_by_track(candidates: list[LessonCandidate]) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    for candidate in candidates:
        totals[candidate.track_code] += 1
    return dict(totals)


def summarize_duration_diagnostics(
    candidates: list[LessonCandidate],
) -> dict[str, dict[str, int]]:
    diagnostics = {
        track_code: {
            "real_duration_lesson_count": 0,
            "fallback_lesson_count": 0,
            "real_duration_minutes": 0,
            "fallback_minutes": 0,
            "fallback_minutes_per_lesson": fallback_minutes_for_track(track_code),
        }
        for track_code in TRACK_ORDER
    }
    for candidate in candidates:
        track = diagnostics.setdefault(
            candidate.track_code,
            {
                "real_duration_lesson_count": 0,
                "fallback_lesson_count": 0,
                "real_duration_minutes": 0,
                "fallback_minutes": 0,
                "fallback_minutes_per_lesson": fallback_minutes_for_track(candidate.track_code),
            },
        )
        if source_duration_seconds(candidate.row) > 0:
            track["real_duration_lesson_count"] += 1
            track["real_duration_minutes"] += candidate.weight_units
        else:
            track["fallback_lesson_count"] += 1
            track["fallback_minutes"] += candidate.weight_units
    return diagnostics


def build_lesson_order_diagnostics(
    *,
    rows: list[dict[str, Any]],
    ordered_fo_lessons: list[Any],
    planned_days: list[PlannedDay],
    unallocated_candidates: list[LessonCandidate],
    prefixes: tuple[str, ...],
) -> list[dict[str, Any]]:
    normalized_prefixes = tuple(
        dict.fromkeys(prefix.strip().upper() for prefix in prefixes if prefix.strip())
    )
    if not normalized_prefixes:
        return []

    projected_fo_codes = {
        assignment.lesson.lesson_code
        for day in planned_days
        for assignment in day.assignments
        if assignment.lesson.track_code == "FO"
    }
    projected_position_by_code = {
        code: position
        for code, position in planned_assignment_positions(planned_days).items()
        if code in projected_fo_codes
    }
    projected_date_by_code = {
        code: position[0] for code, position in projected_position_by_code.items()
    }
    actual_projected_codes = [
        assignment.lesson.lesson_code
        for day in planned_days
        for assignment in day.assignments
        if assignment.lesson.track_code == "FO"
    ]
    canonical_codes = [lesson.lesson_code for lesson in ordered_fo_lessons]
    canonical_code_set = set(canonical_codes)
    unallocated_codes = {
        candidate.lesson_code
        for candidate in unallocated_candidates
        if candidate.track_code == "FO"
    }
    result: list[dict[str, Any]] = []

    for prefix in normalized_prefixes:
        matching_rows = sorted(
            (row for row in rows if str(row.get("lesson_code") or "").upper().startswith(prefix)),
            key=lambda row: (
                int(row.get("module_number") or 9999),
                int(row.get("lesson_number") or 9999),
                int(row.get("slot_index") or 0),
                row["lesson_code"],
            ),
        )
        expected_projected_codes = [
            code for code in canonical_codes if code.upper().startswith(prefix) and code in projected_date_by_code
        ]
        actual_codes = [code for code in actual_projected_codes if code.upper().startswith(prefix)]
        errors: list[str] = []
        if actual_codes != expected_projected_codes:
            errors.append("A ordem projetada diverge da ordem pedagógica canônica.")
        first_violation: dict[str, Any] | None = None
        for previous_code, current_code in zip(
            expected_projected_codes,
            expected_projected_codes[1:],
        ):
            previous_position = projected_position_by_code[previous_code]
            current_position = projected_position_by_code[current_code]
            if previous_position > current_position:
                errors.append("Uma aula posterior foi projetada antes de sua predecessora.")
                first_violation = {
                    "previous_lesson_code": previous_code,
                    "previous_date": previous_position[0],
                    "previous_slot_index": previous_position[1],
                    "lesson_code": current_code,
                    "date": current_position[0],
                    "slot_index": current_position[1],
                }
                break
        unallocated_predecessor = False
        for code in (item for item in canonical_codes if item.upper().startswith(prefix)):
            if code in unallocated_codes:
                unallocated_predecessor = True
            elif unallocated_predecessor and code in projected_date_by_code:
                errors.append(
                    "Uma aula posterior foi projetada enquanto uma predecessora elegível ficou não alocada."
                )
                break

        entries: list[dict[str, Any]] = []
        for row in matching_rows:
            code = row["lesson_code"]
            cut_flags = derive_cut_flags(row)
            if int(row.get("is_seen") or 0) == 1:
                status = "seen"
                reason = "already_seen"
            elif cut_flags["effective"]:
                status = "cut"
                reason = row.get("cut_reason") or next(
                    (name for name in ("manual", "review_free", "english", "already_cut") if cut_flags[name]),
                    "cut",
                )
            elif code in projected_date_by_code:
                status = "projected"
                reason = None
            elif code in unallocated_codes or code in canonical_code_set:
                status = "unallocated"
                reason = "no_available_days"
            else:
                status = "excluded"
                reason = "not_an_eligible_fo_lesson"
            entries.append(
                {
                    "lesson_code": code,
                    "status": status,
                    "reason": reason,
                    "projected_date": projected_date_by_code.get(code),
                    "projected_slot_index": (
                        projected_position_by_code[code][1]
                        if code in projected_position_by_code
                        else None
                    ),
                    "current_date": row.get("recommended_date"),
                    "current_slot_index": row.get("slot_index"),
                }
            )

        result.append(
            {
                "prefix": prefix,
                "is_valid": not errors,
                "pedagogical_monotonicity": "ok" if not errors else "fail",
                "first_violation": first_violation,
                "projected_lesson_count": len(actual_codes),
                "unallocated_lesson_count": sum(
                    entry["status"] == "unallocated" for entry in entries
                ),
                "errors": errors,
                "entries": entries,
            }
        )
    return result


def build_planned_distribution_diagnostics(
    planned_days: list[PlannedDay],
    as_of_date: date,
    target_finish_date: date | None,
) -> dict[str, Any]:
    per_track: dict[str, dict[str, Any]] = {}
    for track_code in TRACK_ORDER:
        per_track[track_code] = empty_track_distribution(track_code)

    for day in planned_days:
        for assignment in day.assignments:
            bucket = per_track.setdefault(
                assignment.lesson.track_code,
                empty_track_distribution(assignment.lesson.track_code),
            )
            add_track_distribution_day(
                bucket=bucket,
                date_value=day.date_value,
                lesson_count=1,
                units=assignment.lesson.weight_units,
            )

    tracks = {
        track_code: finalize_track_distribution(
            bucket=per_track.get(track_code, empty_track_distribution(track_code)),
            as_of_date=as_of_date,
            target_finish_date=target_finish_date,
        )
        for track_code in TRACK_ORDER
    }
    diagnostics = {
        "mode": "dry_run_plan",
        "as_of_date": as_of_date.isoformat(),
        "target_finish_date": format_date(target_finish_date),
        "sources": TRACK_DATE_SOURCES,
        "tracks": tracks,
        "cache_tables": {
            "daily_assignments": {
                "role": "cache_fo_today_overrides",
                "source_of_truth": False,
            },
            "un_daily_assignments": {
                "role": "cache_un_daily_home_rows",
                "source_of_truth": False,
            },
        },
        "non_reprogrammable_pending_before_today": [],
    }
    diagnostics["warnings"] = build_distribution_warnings(diagnostics)
    return diagnostics


def diagnose_distribution(
    conn: sqlite3.Connection,
    as_of_date: date | None = None,
    target_finish_date: date | None = None,
) -> dict[str, Any]:
    effective_as_of = as_of_date or current_local_date()
    settings = get_schedule_settings(conn)
    effective_target = target_finish_date or settings.effective_target_finish_date()
    tracks = {
        track_code: diagnose_track_distribution(
            conn=conn,
            track_code=track_code,
            as_of_date=effective_as_of,
            target_finish_date=effective_target,
        )
        for track_code in TRACK_ORDER
    }
    diagnostics = {
        "mode": "database",
        "as_of_date": effective_as_of.isoformat(),
        "target_finish_date": format_date(effective_target),
        "settings": {
            "exam_date": format_date(settings.exam_date),
            "target_finish_date": format_date(settings.target_finish_date),
            "effective_target_finish_date": format_date(settings.effective_target_finish_date()),
            "include_weekends": settings.include_weekends,
        },
        "sources": TRACK_DATE_SOURCES,
        "tracks": tracks,
        "cache_tables": diagnose_cache_tables(conn),
        "non_reprogrammable_pending_before_today": diagnose_non_reprogrammable_pending(conn, effective_as_of),
    }
    diagnostics["warnings"] = build_distribution_warnings(diagnostics)
    return diagnostics


def diagnose_track_distribution(
    conn: sqlite3.Connection,
    track_code: str,
    as_of_date: date,
    target_finish_date: date | None,
) -> dict[str, Any]:
    lesson_filter = "lesson_type = 'lesson'" if track_code == "FO" else "lesson_type IN ('lesson', 'list')"
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS lesson_count,
            COALESCE(SUM(CASE
                WHEN duration_seconds IS NOT NULL AND duration_seconds > 0
                THEN CAST(ROUND(duration_seconds / 60.0) AS INTEGER)
                ELSE 1
            END), 0) AS units,
            MIN(recommended_date) AS first_date,
            MAX(recommended_date) AS last_date,
            COUNT(DISTINCT recommended_date) AS distinct_dates,
            SUM(CASE WHEN recommended_date < ? THEN 1 ELSE 0 END) AS before_today_count,
            SUM(CASE WHEN ? IS NOT NULL AND recommended_date > ? THEN 1 ELSE 0 END) AS after_target_count
        FROM lessons
        WHERE track_code = ?
          AND is_seen = 0
          AND COALESCE(is_cut, 0) = 0
          AND {lesson_filter}
        """,
        (
            as_of_date.isoformat(),
            format_date(target_finish_date) if target_finish_date else None,
            format_date(target_finish_date) if target_finish_date else None,
            track_code,
        ),
    ).fetchone()
    top_days = conn.execute(
        f"""
        SELECT
            recommended_date AS date,
            COUNT(*) AS lesson_count,
            COALESCE(SUM(CASE
                WHEN duration_seconds IS NOT NULL AND duration_seconds > 0
                THEN CAST(ROUND(duration_seconds / 60.0) AS INTEGER)
                ELSE 1
            END), 0) AS units
        FROM lessons
        WHERE track_code = ?
          AND is_seen = 0
          AND COALESCE(is_cut, 0) = 0
          AND {lesson_filter}
        GROUP BY recommended_date
        ORDER BY units DESC, lesson_count DESC, recommended_date
        LIMIT 10
        """,
        (track_code,),
    ).fetchall()
    return {
        "track_code": track_code,
        **TRACK_DATE_SOURCES[track_code],
        "lesson_count": int(row["lesson_count"] or 0),
        "units": int(row["units"] or 0),
        "first_date": row["first_date"],
        "last_date": row["last_date"],
        "distinct_dates": int(row["distinct_dates"] or 0),
        "before_today_count": int(row["before_today_count"] or 0),
        "after_target_count": int(row["after_target_count"] or 0),
        "top_days": [row_to_dict(day) for day in top_days],
    }


def empty_track_distribution(track_code: str) -> dict[str, Any]:
    return {
        "track_code": track_code,
        **TRACK_DATE_SOURCES.get(track_code, {}),
        "lesson_count": 0,
        "units": 0,
        "dates": {},
    }


def add_track_distribution_day(
    bucket: dict[str, Any],
    date_value: date,
    lesson_count: int,
    units: int,
) -> None:
    date_key = date_value.isoformat()
    day = bucket["dates"].setdefault(date_key, {"date": date_key, "lesson_count": 0, "units": 0})
    day["lesson_count"] += lesson_count
    day["units"] += units
    bucket["lesson_count"] += lesson_count
    bucket["units"] += units


def finalize_track_distribution(
    bucket: dict[str, Any],
    as_of_date: date,
    target_finish_date: date | None,
) -> dict[str, Any]:
    dates = sorted(bucket.pop("dates", {}).values(), key=lambda item: item["date"])
    before_today_count = sum(
        day["lesson_count"]
        for day in dates
        if day["date"] < as_of_date.isoformat()
    )
    after_target_count = (
        sum(day["lesson_count"] for day in dates if day["date"] > target_finish_date.isoformat())
        if target_finish_date
        else 0
    )
    top_days = sorted(
        dates,
        key=lambda item: (-int(item["units"]), -int(item["lesson_count"]), item["date"]),
    )[:10]
    bucket.update(
        {
            "first_date": dates[0]["date"] if dates else None,
            "last_date": dates[-1]["date"] if dates else None,
            "distinct_dates": len(dates),
            "before_today_count": before_today_count,
            "after_target_count": after_target_count,
            "top_days": top_days,
        }
    )
    return bucket


def diagnose_cache_tables(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "daily_assignments": diagnose_cache_table(conn, "daily_assignments"),
        "un_daily_assignments": diagnose_cache_table(conn, "un_daily_assignments"),
    }


def diagnose_cache_table(conn: sqlite3.Connection, table_name: str) -> dict[str, Any]:
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS row_count,
            MIN(dashboard_date) AS first_date,
            MAX(dashboard_date) AS last_date,
            COUNT(DISTINCT dashboard_date) AS distinct_dates
        FROM {table_name}
        """
    ).fetchone()
    role = "cache_un_daily_home_rows" if table_name == "un_daily_assignments" else "cache_fo_today_overrides"
    return {
        "role": role,
        "source_of_truth": False,
        "row_count": int(row["row_count"] or 0),
        "first_date": row["first_date"],
        "last_date": row["last_date"],
        "distinct_dates": int(row["distinct_dates"] or 0),
    }


def diagnose_non_reprogrammable_pending(conn: sqlite3.Connection, as_of_date: date) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            track_code,
            lesson_type,
            COUNT(*) AS lesson_count,
            MIN(recommended_date) AS first_date,
            MAX(recommended_date) AS last_date
        FROM lessons
        WHERE is_seen = 0
          AND COALESCE(is_cut, 0) = 0
          AND recommended_date < ?
          AND NOT (
              (track_code = 'FO' AND lesson_type = 'lesson')
              OR (track_code = 'UN' AND lesson_type IN ('lesson', 'list'))
          )
        GROUP BY track_code, lesson_type
        ORDER BY track_code, lesson_type
        """,
        (as_of_date.isoformat(),),
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def build_distribution_warnings(diagnostics: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for track_code in TRACK_ORDER:
        track = diagnostics["tracks"].get(track_code, {})
        lesson_count = int(track.get("lesson_count") or 0)
        distinct_dates = int(track.get("distinct_dates") or 0)
        if int(track.get("before_today_count") or 0) > 0:
            warnings.append(
                f"{track_code}: {track['before_today_count']} aula(s) ativa(s) antes da data de referencia."
            )
        if int(track.get("after_target_count") or 0) > 0:
            warnings.append(
                f"{track_code}: {track['after_target_count']} aula(s) ativa(s) depois da data-alvo."
            )
        if lesson_count > 0 and distinct_dates == 0:
            warnings.append(f"{track_code}: ha aulas pendentes sem datas distintas.")
        if lesson_count > 1 and distinct_dates <= 1:
            warnings.append(f"{track_code}: todas as aulas pendentes ficaram concentradas em um unico dia.")
    return warnings


def validate_distribution_diagnostics(
    diagnostics: dict[str, Any],
    expected_track_counts: dict[str, int] | None = None,
) -> list[str]:
    expected_track_counts = expected_track_counts or {}
    errors: list[str] = []
    for track_code in TRACK_ORDER:
        expected_count = int(expected_track_counts.get(track_code, 0) or 0)
        track = diagnostics["tracks"].get(track_code, {})
        lesson_count = int(track.get("lesson_count") or 0)
        if expected_count > 0 and lesson_count == 0:
            errors.append(f"{track_code}: nenhuma aula pendente distribuida.")
        if lesson_count != expected_count:
            errors.append(
                f"{track_code}: total distribuido {lesson_count} difere do esperado {expected_count}."
            )
        if int(track.get("before_today_count") or 0) > 0:
            errors.append(f"{track_code}: existem aulas ativas antes da data de referencia.")
        if int(track.get("after_target_count") or 0) > 0:
            errors.append(f"{track_code}: existem aulas ativas depois da data-alvo.")
    return errors


def build_simulation_token(
    settings: ScheduleSettings,
    unavailability: list[ScheduleUnavailability],
    as_of_date: date,
) -> str:
    payload = {
        "exam_date": format_date(settings.exam_date),
        "target_finish_date": format_date(settings.effective_target_finish_date()),
        "include_weekends": settings.include_weekends,
        "as_of_date": as_of_date.isoformat(),
        "unavailability": [
            {
                "start_date": entry.start_date.isoformat(),
                "end_date": entry.end_date.isoformat(),
                "capacity_percent": entry.capacity_percent,
            }
            for entry in unavailability
        ],
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(serialized.encode("utf-8")).hexdigest()


def current_simulation_token(
    conn: sqlite3.Connection,
    settings: ScheduleSettings | None = None,
    as_of_date: date | None = None,
) -> str:
    effective_settings = normalized_settings(settings or get_schedule_settings(conn))
    effective_date = as_of_date or current_local_date()
    return build_simulation_token(
        settings=effective_settings,
        unavailability=list_unavailability(conn),
        as_of_date=effective_date,
    )


def derive_cut_flags(row: dict[str, Any]) -> dict[str, bool]:
    manual_cut = bool(row.get("is_cut") == 1 and row.get("cut_source") == "manual")
    review_free = matches_review_free(row)
    english = should_preserve_english_cut(row)
    already_cut = bool(row.get("is_cut") == 1 and row.get("cut_source") != "manual")
    return {
        "manual": manual_cut,
        "review_free": review_free,
        "english": english,
        "already_cut": already_cut,
        "effective": manual_cut or review_free or english,
    }


def matches_review_free(row: dict[str, Any]) -> bool:
    if row.get("lesson_type") == "review":
        return True
    haystack = " ".join(
        [
            normalize_text(str(row.get("title_raw") or "")),
            normalize_text(str(row.get("portal_title") or "")),
        ]
    )
    return "revisao" in haystack or "livre" in haystack


def should_preserve_english_cut(row: dict[str, Any]) -> bool:
    return matches_english(row)


def matches_english(row: dict[str, Any]) -> bool:
    subject_prefix = str(row.get("subject_prefix") or "").strip().upper()
    if subject_prefix == "LIN":
        return True
    fields = [
        normalize_text(str(row.get("subject_name") or "")),
        normalize_text(str(row.get("subject_prefix") or "")),
        normalize_text(str(row.get("title_raw") or "")),
        normalize_text(str(row.get("portal_title") or "")),
    ]
    joined = " ".join(fields)
    return "ingles" in joined or "lingua estrangeira" in joined


def build_group_label(row: dict[str, Any]) -> str:
    subject = (row.get("subject_prefix") or row.get("track_code") or "SEM").strip()
    module = (row.get("module_label") or "").strip()
    if row.get("track_code") == "UN" and not module:
        module = (row.get("subject_name") or "").strip()
    return f"{subject} :: {module or 'Sem frente'}"


def lesson_weight_units(row: dict[str, Any]) -> int:
    """Return the lesson load in whole minutes; source durations are seconds."""
    duration_seconds = source_duration_seconds(row)
    if duration_seconds > 0:
        return max(int(round(duration_seconds / 60)), 1)
    return fallback_minutes_for_track(str(row.get("track_code") or ""))


def source_duration_seconds(row: dict[str, Any]) -> int:
    try:
        return max(int(row.get("duration_seconds") or 0), 0)
    except (TypeError, ValueError):
        return 0


def fallback_minutes_for_track(track_code: str) -> int:
    if track_code.upper() == "UN":
        return DEFAULT_UN_LESSON_MINUTES
    return DEFAULT_FO_LESSON_MINUTES


def scalar_int(conn: sqlite3.Connection, query: str) -> int:
    row = conn.execute(query).fetchone()
    return int(row[0] if row else 0)


def normalize_text(value: str) -> str:
    lowered = value.strip().lower()
    normalized = unicodedata.normalize("NFKD", lowered)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def parse_date(value: Any) -> date | None:
    if value in {None, ""}:
        return None
    return date.fromisoformat(str(value))


def parse_optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    return int(value)


def format_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


def merge_settings(base: ScheduleSettings, **overrides: Any) -> ScheduleSettings:
    data = {
        "exam_date": base.exam_date,
        "target_finish_date": base.target_finish_date,
        "finish_offset_days_before_exam": base.finish_offset_days_before_exam,
        "include_weekends": base.include_weekends,
        "include_vacations": base.include_vacations,
        "cut_review_free": base.cut_review_free,
        "preserve_english_cut": base.preserve_english_cut,
        "auto_adapt_enabled": base.auto_adapt_enabled,
        "max_daily_minutes_weekday": base.max_daily_minutes_weekday,
        "max_daily_minutes_saturday": base.max_daily_minutes_saturday,
        "max_daily_minutes_sunday": base.max_daily_minutes_sunday,
    }
    for key, value in overrides.items():
        if value is not None:
            data[key] = value
    return normalized_settings(ScheduleSettings(**data))
