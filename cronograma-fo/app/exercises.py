from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

DEFAULT_EXERCISE_OFFSET_DAYS = 3
EXERCISE_OFFSET_SETTING_KEY = "fo_exercise_offset_days"
VALID_EXERCISE_STATUSES = {"pending", "done", "skipped"}


def to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def get_exercise_offset_days(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT setting_value FROM app_settings WHERE setting_key = ?",
        (EXERCISE_OFFSET_SETTING_KEY,),
    ).fetchone()
    if not row:
        return DEFAULT_EXERCISE_OFFSET_DAYS

    try:
        offset_days = int(row["setting_value"])
    except (TypeError, ValueError):
        return DEFAULT_EXERCISE_OFFSET_DAYS
    return offset_days if offset_days >= 0 else DEFAULT_EXERCISE_OFFSET_DAYS


def set_exercise_offset_days(conn: sqlite3.Connection, offset_days: int) -> None:
    normalized_offset = max(offset_days, 0)
    conn.execute(
        """
        INSERT INTO app_settings (setting_key, setting_value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(setting_key) DO UPDATE SET
            setting_value = excluded.setting_value,
            updated_at = CURRENT_TIMESTAMP
        """,
        (EXERCISE_OFFSET_SETTING_KEY, str(normalized_offset)),
    )


def reschedule_unmoved_exercise_tasks(conn: sqlite3.Connection) -> int:
    offset_days = get_exercise_offset_days(conn)
    rows = conn.execute(
        """
        SELECT
            exercise_tasks.id,
            lessons.recommended_date,
            lessons.is_seen,
            lessons.seen_at
        FROM exercise_tasks
        JOIN lessons ON lessons.lesson_code = exercise_tasks.source_lesson_code
        WHERE lessons.track_code = 'FO'
          AND lessons.lesson_type = 'lesson'
          AND exercise_tasks.manually_moved = 0
        """
    ).fetchall()

    for row in rows:
        reference_date = date.fromisoformat(row["recommended_date"])
        if row["is_seen"] == 1:
            normalized_seen_at = str(row["seen_at"] or "").replace(" ", "T")
            try:
                reference_date = datetime.fromisoformat(normalized_seen_at).date()
            except ValueError:
                pass
        conn.execute(
            """
            UPDATE exercise_tasks
            SET scheduled_date = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (scheduled_date_from(reference_date, offset_days), row["id"]),
        )
    return len(rows)


def scheduled_date_from(reference_date: date, offset_days: int) -> str:
    return (reference_date + timedelta(days=offset_days)).isoformat()


def lesson_seen_date(lesson: sqlite3.Row | dict[str, Any]) -> date | None:
    seen_at = lesson["seen_at"] if isinstance(lesson, sqlite3.Row) else lesson.get("seen_at")
    if not seen_at:
        return None

    normalized = str(seen_at).replace(" ", "T")
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        return None


def default_task_date_for_lesson(lesson: sqlite3.Row, offset_days: int) -> str:
    if lesson["is_seen"] == 1:
        seen_date = lesson_seen_date(lesson)
        if seen_date:
            return scheduled_date_from(seen_date, offset_days)
    return scheduled_date_from(date.fromisoformat(lesson["recommended_date"]), offset_days)


def sync_fo_exercise_tasks(conn: sqlite3.Connection) -> int:
    offset_days = get_exercise_offset_days(conn)
    lesson_rows = conn.execute(
        """
        SELECT lesson_code, recommended_date, is_seen, seen_at
        FROM lessons
        WHERE track_code = 'FO'
          AND lesson_type = 'lesson'
        ORDER BY recommended_date, day_index, slot_index
        """
    ).fetchall()
    existing_rows = conn.execute(
        """
        SELECT id, source_lesson_code, is_active, manually_moved
        FROM exercise_tasks
        """
    ).fetchall()
    existing_by_code = {row["source_lesson_code"]: row for row in existing_rows}
    lesson_codes = {lesson["lesson_code"] for lesson in lesson_rows}

    new_tasks: list[tuple[str, str, int]] = []
    deactivate_task_ids: list[tuple[int]] = []
    activate_tasks: list[tuple[str, int]] = []

    for lesson in lesson_rows:
        existing = existing_by_code.get(lesson["lesson_code"])
        active = 1 if lesson["is_seen"] == 1 else 0

        if existing is None:
            new_tasks.append(
                (
                    lesson["lesson_code"],
                    default_task_date_for_lesson(lesson, offset_days),
                    active,
                )
            )
            continue

        if lesson["is_seen"] == 0 and existing["is_active"] == 1:
            deactivate_task_ids.append((existing["id"],))
        elif lesson["is_seen"] == 1 and existing["is_active"] == 0:
            activate_tasks.append(
                (default_task_date_for_lesson(lesson, offset_days), existing["id"])
            )

    stale_task_ids = [
        (row["id"],)
        for row in existing_rows
        if row["source_lesson_code"] not in lesson_codes
    ]

    if new_tasks:
        conn.executemany(
            """
            INSERT INTO exercise_tasks (
                source_lesson_code,
                scheduled_date,
                status,
                is_active,
                manually_moved,
                updated_at
            ) VALUES (?, ?, 'pending', ?, 0, CURRENT_TIMESTAMP)
            """,
            new_tasks,
        )
    if deactivate_task_ids:
        conn.executemany(
            """
            UPDATE exercise_tasks
            SET is_active = 0,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            deactivate_task_ids,
        )
    if activate_tasks:
        conn.executemany(
            """
            UPDATE exercise_tasks
            SET is_active = 1,
                scheduled_date = ?,
                manually_moved = 0,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            activate_tasks,
        )
    if stale_task_ids:
        conn.executemany(
            "DELETE FROM exercise_tasks WHERE id = ?",
            stale_task_ids,
        )

    return len(new_tasks) + len(deactivate_task_ids) + len(activate_tasks)


def activate_exercise_for_lesson(
    conn: sqlite3.Connection,
    lesson_code: str,
    reference_date: date,
) -> None:
    offset_days = get_exercise_offset_days(conn)
    scheduled_date = scheduled_date_from(reference_date, offset_days)
    conn.execute(
        """
        INSERT INTO exercise_tasks (
            source_lesson_code,
            scheduled_date,
            status,
            is_active,
            manually_moved,
            updated_at
        ) VALUES (?, ?, 'pending', 1, 0, CURRENT_TIMESTAMP)
        ON CONFLICT(source_lesson_code) DO UPDATE SET
            scheduled_date = excluded.scheduled_date,
            is_active = 1,
            manually_moved = 0,
            updated_at = CURRENT_TIMESTAMP
        """,
        (lesson_code, scheduled_date),
    )


def deactivate_exercise_for_lesson(conn: sqlite3.Connection, lesson_code: str) -> None:
    conn.execute(
        """
        UPDATE exercise_tasks
        SET is_active = 0,
            updated_at = CURRENT_TIMESTAMP
        WHERE source_lesson_code = ?
        """,
        (lesson_code,),
    )


def fetch_fo_exercise_tasks_for_date(
    conn: sqlite3.Connection,
    target_date: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            exercise_tasks.*,
            lessons.lesson_code,
            lessons.track_code,
            lessons.lesson_type,
            lessons.title_raw,
            lessons.portal_title,
            lessons.subject_name,
            lessons.subject_prefix,
            lessons.module_label,
            lessons.module_number,
            lessons.lesson_number,
            lessons.week_number,
            lessons.day_index,
            lessons.slot_index,
            lessons.recommended_date,
            lessons.is_seen,
            lessons.seen_at,
            CASE
              WHEN exercise_tasks.scheduled_date < ?
                   AND exercise_tasks.status != 'done'
                   AND lessons.is_seen = 1
              THEN 1
              ELSE 0
            END AS is_overdue
        FROM exercise_tasks
        JOIN lessons ON lessons.lesson_code = exercise_tasks.source_lesson_code
        WHERE lessons.track_code = 'FO'
          AND lessons.lesson_type = 'lesson'
          AND lessons.is_seen = 1
          AND exercise_tasks.is_active = 1
          AND (
            exercise_tasks.scheduled_date = ?
            OR (
              exercise_tasks.scheduled_date < ?
              AND exercise_tasks.status != 'done'
            )
          )
        ORDER BY is_overdue DESC, exercise_tasks.scheduled_date, lessons.subject_prefix, lessons.module_number, lessons.lesson_number
        """,
        (target_date, target_date, target_date),
    ).fetchall()
    return [to_dict(row) for row in rows]


def count_fo_exercise_tasks_for_date(conn: sqlite3.Connection, target_date: str) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS visible_total,
          SUM(CASE WHEN exercise_tasks.scheduled_date < ?
                    AND exercise_tasks.status != 'done'
                    AND lessons.is_seen = 1
                   THEN 1 ELSE 0 END) AS overdue_total,
          SUM(CASE WHEN exercise_tasks.status = 'done' THEN 1 ELSE 0 END) AS done_total
        FROM exercise_tasks
        JOIN lessons ON lessons.lesson_code = exercise_tasks.source_lesson_code
        WHERE lessons.track_code = 'FO'
          AND lessons.lesson_type = 'lesson'
          AND lessons.is_seen = 1
          AND exercise_tasks.is_active = 1
          AND (
            exercise_tasks.scheduled_date = ?
            OR (
              exercise_tasks.scheduled_date < ?
              AND exercise_tasks.status != 'done'
            )
          )
        """,
        (target_date, target_date, target_date),
    ).fetchone()
    return {
        "visible_total": int(row["visible_total"] or 0),
        "overdue_total": int(row["overdue_total"] or 0),
        "done_total": int(row["done_total"] or 0),
    }


def update_exercise_status(conn: sqlite3.Connection, task_id: int, status: str) -> bool:
    if status not in VALID_EXERCISE_STATUSES:
        return False
    cursor = conn.execute(
        """
        UPDATE exercise_tasks
        SET status = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
          AND source_lesson_code IN (
            SELECT lesson_code
            FROM lessons
            WHERE track_code = 'FO'
              AND lesson_type = 'lesson'
          )
        """,
        (status, task_id),
    )
    return cursor.rowcount > 0


def reschedule_exercise_task(conn: sqlite3.Connection, task_id: int, scheduled_date: str) -> bool:
    cursor = conn.execute(
        """
        UPDATE exercise_tasks
        SET scheduled_date = ?,
            manually_moved = 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
          AND source_lesson_code IN (
            SELECT lesson_code
            FROM lessons
            WHERE track_code = 'FO'
              AND lesson_type = 'lesson'
          )
        """,
        (scheduled_date, task_id),
    )
    return cursor.rowcount > 0
