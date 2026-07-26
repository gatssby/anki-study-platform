from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .exercises import (
    count_fo_exercise_tasks_for_date,
    fetch_fo_exercise_tasks_for_date,
    get_exercise_offset_days,
)


def current_local_date() -> date:
    try:
        tz = ZoneInfo("America/Sao_Paulo")
    except Exception:
        tz = timezone(timedelta(hours=-3))
    return datetime.now(tz).date()


def parse_iso_date(value: str | None) -> date:
    if value:
        return date.fromisoformat(value)
    return current_local_date()


def to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def strip_fo_lesson_prefix(lesson: dict[str, Any]) -> str:
    title = (lesson.get("portal_title") or "").strip() or lesson["title_raw"]
    subject_name = lesson.get("subject_name")
    subject_prefix = lesson.get("subject_prefix")
    module_label = lesson.get("module_label")

    candidates = []
    if subject_name and module_label:
        candidates.append(f"{subject_name} {module_label}")
    if subject_prefix and module_label:
        candidates.append(f"{subject_prefix} {module_label}")
    if subject_name:
        candidates.append(subject_name)
    if subject_prefix:
        candidates.append(subject_prefix)

    normalized_title = title.lower()
    for candidate in candidates:
        prefix = f"{candidate} - "
        if normalized_title.startswith(prefix.lower()):
            stripped = title[len(prefix) :].strip()
            if stripped:
                return stripped

    return title


def lesson_label(lesson: dict[str, Any]) -> str:
    if lesson.get("track_code") == "UN":
        return lesson["title_raw"]
    if lesson["lesson_type"] == "lesson":
        return strip_fo_lesson_prefix(lesson)
    return lesson["title_raw"]


def track_lesson_filter(track_code: str) -> str:
    if track_code == "UN":
        return "lesson_type IN ('lesson', 'list') AND is_cut = 0"
    return "lesson_type = 'lesson' AND is_cut = 0"


def fetch_lessons_for_date(
    conn: sqlite3.Connection,
    target_date: str,
    track_code: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM lessons
        WHERE recommended_date = ?
          AND track_code = ?
          AND is_cut = 0
        ORDER BY day_index, slot_index
        """,
        (target_date, track_code),
    ).fetchall()
    return [to_dict(row) for row in rows]


def fetch_lessons_for_track(conn: sqlite3.Connection, track_code: str) -> list[dict[str, Any]]:
    lesson_filter = track_lesson_filter(track_code)
    rows = conn.execute(
        f"""
        SELECT *
        FROM lessons
        WHERE track_code = ?
          AND {lesson_filter}
        ORDER BY recommended_date, day_index, slot_index
        """,
        (track_code,),
    ).fetchall()
    return [to_dict(row) for row in rows]


def fetch_lesson_by_code(conn: sqlite3.Connection, lesson_code: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM lessons WHERE lesson_code = ?",
        (lesson_code,),
    ).fetchone()
    return to_dict(row) if row else None


def fetch_assignments_for_date(conn: sqlite3.Connection, target_date: str) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT planned_slot_key, assigned_lesson_code
        FROM daily_assignments
        WHERE dashboard_date = ?
        """,
        (target_date,),
    ).fetchall()
    return {row["planned_slot_key"]: row["assigned_lesson_code"] for row in rows}


def upsert_assignment(
    conn: sqlite3.Connection,
    target_date: str,
    planned_slot_key: str,
    assigned_lesson_code: str,
) -> None:
    conn.execute(
        """
        INSERT INTO daily_assignments (
            dashboard_date,
            planned_slot_key,
            assigned_lesson_code,
            updated_at
        ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(dashboard_date, planned_slot_key) DO UPDATE SET
            assigned_lesson_code = excluded.assigned_lesson_code,
            updated_at = CURRENT_TIMESTAMP
        """,
        (target_date, planned_slot_key, assigned_lesson_code),
    )


def delete_assignment(conn: sqlite3.Connection, target_date: str, planned_slot_key: str) -> None:
    conn.execute(
        "DELETE FROM daily_assignments WHERE dashboard_date = ? AND planned_slot_key = ?",
        (target_date, planned_slot_key),
    )


def fetch_un_assignments_for_date(conn: sqlite3.Connection, target_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT lessons.*, un_daily_assignments.row_index
        FROM un_daily_assignments
        JOIN lessons ON lessons.lesson_code = un_daily_assignments.assigned_lesson_code
        WHERE un_daily_assignments.dashboard_date = ?
        ORDER BY un_daily_assignments.row_index
        """,
        (target_date,),
    ).fetchall()
    return [to_dict(row) for row in rows]


def replace_un_assignments_for_date(
    conn: sqlite3.Connection,
    target_date: str,
    lessons: list[dict[str, Any]],
) -> None:
    conn.execute(
        "DELETE FROM un_daily_assignments WHERE dashboard_date = ?",
        (target_date,),
    )
    conn.executemany(
        """
        INSERT INTO un_daily_assignments (
            dashboard_date,
            row_index,
            assigned_lesson_code,
            updated_at
        ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """,
        [
            (target_date, index, lesson["lesson_code"])
            for index, lesson in enumerate(lessons, start=1)
        ],
    )


def fetch_track_due_lessons(conn: sqlite3.Connection, lesson: dict[str, Any], target_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM lessons
        WHERE lesson_type = 'lesson'
          AND track_code = 'FO'
          AND is_cut = 0
          AND subject_prefix = ?
          AND (
            (module_number IS NULL AND ? IS NULL)
            OR module_number = ?
          )
          AND recommended_date <= ?
        ORDER BY recommended_date, day_index, slot_index
        """,
        (lesson["subject_prefix"], lesson["module_number"], lesson["module_number"], target_date),
    ).fetchall()
    return [to_dict(row) for row in rows]


def first_unseen(lessons: list[dict[str, Any]]) -> dict[str, Any] | None:
    for lesson in lessons:
        if lesson["is_seen"] == 0:
            return lesson
    return None


def unseen_before_planned_count(lessons: list[dict[str, Any]], planned_slot_key: str) -> int:
    count = 0
    for lesson in lessons:
        if lesson["slot_key"] == planned_slot_key:
            break
        if lesson["is_seen"] == 0:
            count += 1
    return count


def build_today_rows(conn: sqlite3.Connection, target_date: str) -> tuple[list[dict[str, Any]], set[str]]:
    planned_lessons = fetch_lessons_for_date(conn=conn, target_date=target_date, track_code="FO")
    assignments = fetch_assignments_for_date(conn=conn, target_date=target_date)

    rows: list[dict[str, Any]] = []
    assigned_codes: set[str] = set()
    has_writes = False

    for planned in planned_lessons:
        row: dict[str, Any] = {
            "planned": planned,
            "target": planned,
            "delay_count": 0,
            "warning": None,
            "is_fixed": False,
        }

        if planned["lesson_type"] != "lesson":
            rows.append(row)
            continue

        track_due = fetch_track_due_lessons(conn=conn, lesson=planned, target_date=target_date)
        delay_count = unseen_before_planned_count(track_due, planned["slot_key"])

        assigned_code = assignments.get(planned["slot_key"])
        target_lesson: dict[str, Any] | None = None

        if assigned_code:
            assigned = fetch_lesson_by_code(conn=conn, lesson_code=assigned_code)
            if assigned and assigned["lesson_type"] == "lesson":
                target_lesson = assigned
                row["is_fixed"] = True
            else:
                delete_assignment(conn=conn, target_date=target_date, planned_slot_key=planned["slot_key"])
                has_writes = True

        if target_lesson is None:
            due_unseen = first_unseen(track_due)
            target_lesson = due_unseen or planned
            upsert_assignment(
                conn=conn,
                target_date=target_date,
                planned_slot_key=planned["slot_key"],
                assigned_lesson_code=target_lesson["lesson_code"],
            )
            row["is_fixed"] = True
            has_writes = True

        row["target"] = target_lesson
        row["delay_count"] = delay_count
        assigned_codes.add(target_lesson["lesson_code"])

        if target_lesson["slot_key"] != planned["slot_key"] and delay_count > 0:
            module_display = planned["module_label"] or ""
            delay_word = "aula" if delay_count == 1 else "aulas"
            row["warning"] = (
                f"Atraso em {planned['subject_prefix']} {module_display}: "
                f"{delay_count} {delay_word} antes da aula planejada."
            ).strip()

        rows.append(row)

    if has_writes:
        conn.commit()

    return rows, assigned_codes


def build_universe_narrado_rows(conn: sqlite3.Connection, target_date: str) -> list[dict[str, Any]]:
    lessons = fetch_lessons_for_date(conn=conn, target_date=target_date, track_code="UN")
    return [
        {
            "lesson": lesson,
        }
        for lesson in lessons
        if lesson["lesson_type"] == "lesson"
    ]


def next_business_day(current_date_value: date) -> date:
    current = current_date_value
    while current.isoweekday() > 5:
        current += timedelta(days=1)
    return current


def previous_business_day(current_date_value: date) -> date:
    current = current_date_value - timedelta(days=1)
    while current.isoweekday() > 5:
        current -= timedelta(days=1)
    return current


def business_days_between(start_date: date, end_date: date) -> list[date]:
    if start_date > end_date:
        return []

    days: list[date] = []
    current = start_date
    while current <= end_date:
        if current.isoweekday() <= 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def fetch_track_end_date(conn: sqlite3.Connection, track_code: str) -> date | None:
    lesson_filter = track_lesson_filter(track_code)
    row = conn.execute(
        f"""
        SELECT MAX(recommended_date) AS end_date
        FROM lessons
        WHERE track_code = ?
          AND {lesson_filter}
        """,
        (track_code,),
    ).fetchone()
    if not row or not row["end_date"]:
        return None
    return date.fromisoformat(row["end_date"])


def fetch_track_start_date(conn: sqlite3.Connection, track_code: str) -> date | None:
    lesson_filter = track_lesson_filter(track_code)
    row = conn.execute(
        f"""
        SELECT MIN(recommended_date) AS start_date
        FROM lessons
        WHERE track_code = ?
          AND {lesson_filter}
        """,
        (track_code,),
    ).fetchone()
    if not row or not row["start_date"]:
        return None
    return date.fromisoformat(row["start_date"])


def lesson_duration_seconds(lesson: dict[str, Any]) -> int:
    if lesson.get("lesson_type") != "lesson":
        return 0
    try:
        return int(lesson.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        return 0


def derive_un_module_path(relative_path: str | None) -> str:
    path = (relative_path or "").strip().replace("\\", "/")
    for marker in ("/Aulas/", "/Material de Apoio/"):
        if marker in path:
            return path.split(marker, 1)[0].rstrip("/")
    if path.endswith("/Lista.pdf"):
        return path[: -len("/Lista.pdf")].rstrip("/")
    if "/" in path:
        return path.rsplit("/", 1)[0].rstrip("/")
    return path


def un_module_display_name(module_path: str, lessons: list[dict[str, Any]]) -> str:
    labels = [lesson.get("module_label") for lesson in lessons if lesson.get("module_label")]
    if labels:
        return max(set(labels), key=labels.count)

    parts = [part for part in module_path.split("/") if part]
    if parts and parts[-1] in {"Sem Tópico", "Sem Subtópico"} and len(parts) >= 2:
        return parts[-2]
    return parts[-1] if parts else "Módulo UN"


def un_lesson_sort_key(lesson: dict[str, Any]) -> tuple[Any, ...]:
    lesson_number = lesson.get("lesson_number")
    item_order = {"lesson": 0, "list": 1, "review": 2, "pending": 3}.get(lesson.get("lesson_type"), 9)
    return (
        lesson_number is None,
        lesson_number if lesson_number is not None else 999999,
        item_order,
        lesson.get("relative_path") or "",
    )


def build_un_module_rows(
    lessons: list[dict[str, Any]],
    target_date: str,
    target_duration_seconds: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for lesson in lessons:
        module_path = derive_un_module_path(lesson.get("relative_path"))
        if not module_path:
            module_path = lesson.get("module_label") or lesson.get("lesson_code") or "Módulo UN"
        grouped.setdefault(module_path, []).append(lesson)

    target_day = date.fromisoformat(target_date)
    module_rows: list[dict[str, Any]] = []
    for module_path, module_lessons in grouped.items():
        active_lessons = [lesson for lesson in module_lessons if int(lesson.get("is_cut") or 0) == 0]
        pending_lessons = [lesson for lesson in active_lessons if int(lesson.get("is_seen") or 0) == 0]
        if not pending_lessons:
            continue

        video_lessons = [lesson for lesson in active_lessons if lesson.get("lesson_type") == "lesson"]
        seen_video_count = sum(1 for lesson in video_lessons if int(lesson.get("is_seen") or 0) == 1)
        pending_duration_seconds = sum(lesson_duration_seconds(lesson) for lesson in pending_lessons)
        first_pending_date = min(lesson["recommended_date"] for lesson in pending_lessons if lesson.get("recommended_date"))
        overdue_days = max((target_day - date.fromisoformat(first_pending_date)).days, 0)
        first_pending_lesson = min(
            pending_lessons,
            key=lambda lesson: (
                lesson.get("recommended_date") or "",
                lesson.get("day_index") or 0,
                lesson.get("slot_index") or 0,
                un_lesson_sort_key(lesson),
            ),
        )

        module_rows.append(
            {
                "module_path": module_path,
                "subject_prefix": first_pending_lesson.get("subject_prefix") or "",
                "module_name": un_module_display_name(module_path, active_lessons),
                "pending_duration_seconds": pending_duration_seconds,
                "seen_lesson_count": seen_video_count,
                "total_lesson_count": len(video_lessons),
                "pending_item_count": len(pending_lessons),
                "first_pending_date": first_pending_date,
                "overdue_days": overdue_days,
                "is_overdue": overdue_days > 0,
                "sort_key": (
                    first_pending_lesson.get("recommended_date") or "",
                    first_pending_lesson.get("day_index") or 0,
                    first_pending_lesson.get("slot_index") or 0,
                    first_pending_lesson.get("relative_path") or "",
                ),
                "lessons": sorted(active_lessons, key=un_lesson_sort_key),
                "pending_lessons": sorted(pending_lessons, key=un_lesson_sort_key),
            }
        )

    module_rows.sort(key=lambda row: row["sort_key"])

    if target_duration_seconds <= 0:
        return module_rows[:1]

    selected_rows: list[dict[str, Any]] = []
    selected_duration = 0
    for row in module_rows:
        selected_rows.append(row)
        selected_duration += int(row["pending_duration_seconds"] or 0)
        if selected_duration >= target_duration_seconds:
            break
    return selected_rows


def format_duration_compact(total_seconds: int) -> str:
    if total_seconds <= 0:
        return "0m"
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    if minutes > 0:
        return f"{minutes}m"
    return "< 1m"


def build_schedule_summary(conn: sqlite3.Connection, target_date: str) -> dict[str, Any]:
    fo_end_date = fetch_track_end_date(conn=conn, track_code="FO")
    if not fo_end_date:
        return {
            "end_date": None,
            "business_days_remaining": 0,
            "calendar_days_remaining": 0,
        }

    target_day = date.fromisoformat(target_date)
    reference_day = next_business_day(target_day)
    remaining_business_days = business_days_between(start_date=reference_day, end_date=fo_end_date)

    return {
        "end_date": fo_end_date.isoformat(),
        "business_days_remaining": len(remaining_business_days),
        "calendar_days_remaining": max((fo_end_date - target_day).days, 0),
    }


def calculate_un_expected_duration(
    conn: sqlite3.Connection,
    target_date: str,
    total_duration_seconds: int,
) -> int:
    if total_duration_seconds <= 0:
        return 0

    fo_end_date = fetch_track_end_date(conn=conn, track_code="FO")
    un_start_date = fetch_track_start_date(conn=conn, track_code="UN")
    if not fo_end_date or not un_start_date:
        return 0

    target_day = date.fromisoformat(target_date)
    if target_day < un_start_date:
        return 0

    total_days = business_days_between(start_date=un_start_date, end_date=fo_end_date)
    if not total_days:
        return total_duration_seconds

    reference_day = min(next_business_day(target_day), fo_end_date)
    elapsed_days = business_days_between(start_date=un_start_date, end_date=reference_day)
    expected_duration = round(total_duration_seconds * len(elapsed_days) / len(total_days))
    return min(expected_duration, total_duration_seconds)


def build_un_track_progress(
    conn: sqlite3.Connection,
    target_date: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total_lessons,
          SUM(CASE WHEN is_seen = 1 THEN 1 ELSE 0 END) AS seen_lessons,
          SUM(CASE WHEN recommended_date <= ? THEN 1 ELSE 0 END) AS expected_lessons,
          SUM(CASE WHEN lesson_type = 'lesson' THEN COALESCE(duration_seconds, 0) ELSE 0 END) AS total_duration_seconds,
          SUM(CASE WHEN lesson_type = 'lesson' AND is_seen = 1 THEN COALESCE(duration_seconds, 0) ELSE 0 END) AS seen_duration_seconds
        FROM lessons
        WHERE track_code = 'UN'
          AND lesson_type IN ('lesson', 'list')
          AND is_cut = 0
        """,
        (target_date,),
    ).fetchone()

    total_lessons = int(row["total_lessons"] or 0)
    seen_lessons = int(row["seen_lessons"] or 0)
    expected_lessons = int(row["expected_lessons"] or 0)
    total_duration_seconds = int(row["total_duration_seconds"] or 0)
    seen_duration_seconds = int(row["seen_duration_seconds"] or 0)
    remaining_duration_seconds = max(total_duration_seconds - seen_duration_seconds, 0)
    progress_percent = (
        round((seen_duration_seconds / total_duration_seconds) * 100, 1)
        if total_duration_seconds
        else 0.0
    )
    expected_duration_seconds = calculate_un_expected_duration(
        conn=conn,
        target_date=target_date,
        total_duration_seconds=total_duration_seconds,
    )
    pace_delta_seconds = seen_duration_seconds - expected_duration_seconds

    if pace_delta_seconds < 0:
        pace_status = "negative"
        pace_label = f"{format_duration_compact(abs(pace_delta_seconds))} de atraso"
        pace_summary = f"{format_duration_compact(abs(pace_delta_seconds))} atraso"
    elif pace_delta_seconds > 0:
        pace_status = "positive"
        pace_label = f"{format_duration_compact(pace_delta_seconds)} adiantadas"
        pace_summary = f"{format_duration_compact(pace_delta_seconds)} adiant."
    else:
        pace_status = "neutral"
        pace_label = "Em dia"
        pace_summary = "Em dia"

    return {
        "track_code": "UN",
        "total_lessons": total_lessons,
        "seen_lessons": seen_lessons,
        "remaining_lessons": max(total_lessons - seen_lessons, 0),
        "expected_lessons": expected_lessons,
        "progress_percent": progress_percent,
        "pace_delta": pace_delta_seconds,
        "pace_status": pace_status,
        "pace_label": pace_label,
        "pace_summary": pace_summary,
        "total_duration_seconds": total_duration_seconds,
        "seen_duration_seconds": seen_duration_seconds,
        "remaining_duration_seconds": remaining_duration_seconds,
        "expected_duration_seconds": expected_duration_seconds,
    }


def build_track_progress(
    conn: sqlite3.Connection,
    track_code: str,
    target_date: str,
) -> dict[str, Any]:
    if track_code == "UN":
        return build_un_track_progress(conn=conn, target_date=target_date)

    lesson_filter = track_lesson_filter(track_code)
    row = conn.execute(
        f"""
        SELECT
          COUNT(*) AS total_lessons,
          SUM(CASE WHEN is_seen = 1 THEN 1 ELSE 0 END) AS seen_lessons,
          SUM(CASE WHEN recommended_date <= ? THEN 1 ELSE 0 END) AS expected_lessons
        FROM lessons
        WHERE track_code = ?
          AND {lesson_filter}
        """,
        (target_date, track_code),
    ).fetchone()

    total_lessons = int(row["total_lessons"] or 0)
    seen_lessons = int(row["seen_lessons"] or 0)
    expected_lessons = int(row["expected_lessons"] or 0)
    progress_percent = round((seen_lessons / total_lessons) * 100, 1) if total_lessons else 0.0
    pace_delta = seen_lessons - expected_lessons

    if pace_delta < 0:
        pace_status = "negative"
        pace_label = f"{abs(pace_delta)} aula(s) atrasadas"
        pace_summary = f"{abs(pace_delta)} atrasadas"
    elif pace_delta > 0:
        pace_status = "positive"
        pace_label = f"{pace_delta} aula(s) adiantadas"
        pace_summary = f"{pace_delta} adiantadas"
    else:
        pace_status = "neutral"
        pace_label = "Em dia"
        pace_summary = "Em dia"

    return {
        "track_code": track_code,
        "total_lessons": total_lessons,
        "seen_lessons": seen_lessons,
        "remaining_lessons": max(total_lessons - seen_lessons, 0),
        "expected_lessons": expected_lessons,
        "progress_percent": progress_percent,
        "pace_delta": pace_delta,
        "pace_status": pace_status,
        "pace_label": pace_label,
        "pace_summary": pace_summary,
    }


def un_topic_group_key(lesson: dict[str, Any]) -> tuple[str, str]:
    module_label = (lesson.get("module_label") or "").strip()
    primary_topic = module_label.split(" | ", 1)[0].strip() if module_label else ""
    if not primary_topic:
        primary_topic = lesson.get("title_raw") or lesson.get("lesson_code") or ""
    return (lesson.get("subject_prefix") or "", primary_topic)


def calculate_un_daily_time_plan(conn: sqlite3.Connection, target_date: str) -> dict[str, Any]:
    fo_end_date = fetch_track_end_date(conn=conn, track_code="FO")
    un_start_date = fetch_track_start_date(conn=conn, track_code="UN")
    if not fo_end_date or not un_start_date:
        return {
            "target_duration_seconds": 0,
            "remaining_duration_seconds": 0,
            "business_days_remaining": 0,
            "end_date": fo_end_date.isoformat() if fo_end_date else None,
        }

    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN lesson_type = 'lesson' THEN COALESCE(duration_seconds, 0) ELSE 0 END) AS remaining_duration_seconds
        FROM lessons
        WHERE track_code = 'UN'
          AND lesson_type IN ('lesson', 'list')
          AND is_seen = 0
          AND is_cut = 0
        """
    ).fetchone()
    remaining_duration_seconds = int(row["remaining_duration_seconds"] or 0)

    target_day = date.fromisoformat(target_date)
    reference_day = max(next_business_day(target_day), un_start_date)
    remaining_business_days = business_days_between(start_date=reference_day, end_date=fo_end_date)

    if remaining_duration_seconds <= 0:
        target_duration_seconds = 0
    elif remaining_business_days:
        target_duration_seconds = (
            remaining_duration_seconds + len(remaining_business_days) - 1
        ) // len(remaining_business_days)
    else:
        target_duration_seconds = remaining_duration_seconds

    return {
        "target_duration_seconds": target_duration_seconds,
        "remaining_duration_seconds": remaining_duration_seconds,
        "business_days_remaining": len(remaining_business_days),
        "end_date": fo_end_date.isoformat(),
    }


def choose_topic_prefix_by_duration(
    topic_lessons: list[dict[str, Any]],
    target_duration_seconds: int,
    current_duration_seconds: int,
    force_one: bool,
) -> int:
    if not topic_lessons:
        return 0

    best_count = 0
    best_delta = abs(target_duration_seconds - current_duration_seconds)
    running_duration = current_duration_seconds

    for index, lesson in enumerate(topic_lessons, start=1):
        running_duration += lesson_duration_seconds(lesson)
        delta = abs(target_duration_seconds - running_duration)
        if (
            delta < best_delta
            or (delta == best_delta and index > best_count)
            or (force_one and best_count == 0)
        ):
            best_count = index
            best_delta = delta

    return best_count


def build_un_queue_by_duration(
    unseen_lessons: list[dict[str, Any]],
    target_duration_seconds: int,
    initial_duration_seconds: int = 0,
) -> list[dict[str, Any]]:
    if not unseen_lessons:
        return []

    selected: list[dict[str, Any]] = []
    selected_codes: set[str] = set()
    remaining = unseen_lessons[:]
    current_duration = initial_duration_seconds

    while remaining:
        if selected and current_duration >= target_duration_seconds:
            break

        current_key = un_topic_group_key(remaining[0])
        topic_lessons = [lesson for lesson in remaining if un_topic_group_key(lesson) == current_key]
        force_one = not selected and current_duration == 0 and target_duration_seconds > 0
        prefix_count = choose_topic_prefix_by_duration(
            topic_lessons=topic_lessons,
            target_duration_seconds=target_duration_seconds,
            current_duration_seconds=current_duration,
            force_one=force_one,
        )

        if prefix_count <= 0:
            break

        selected_topic_lessons = topic_lessons[:prefix_count]
        selected.extend(selected_topic_lessons)
        selected_codes.update(lesson["lesson_code"] for lesson in selected_topic_lessons)
        current_duration += sum(lesson_duration_seconds(lesson) for lesson in selected_topic_lessons)

        while prefix_count < len(topic_lessons) and lesson_duration_seconds(topic_lessons[prefix_count]) == 0:
            selected.append(topic_lessons[prefix_count])
            selected_codes.add(topic_lessons[prefix_count]["lesson_code"])
            prefix_count += 1

        remaining = [lesson for lesson in remaining if lesson["lesson_code"] not in selected_codes]

    return selected


def build_universe_narrado_section(conn: sqlite3.Connection, target_date: str) -> dict[str, Any]:
    progress = build_track_progress(conn=conn, track_code="UN", target_date=target_date)
    fo_end_date = fetch_track_end_date(conn=conn, track_code="FO")
    daily_time_plan = calculate_un_daily_time_plan(conn=conn, target_date=target_date)

    rows = conn.execute(
        """
        SELECT *
        FROM lessons
        WHERE track_code = 'UN'
          AND lesson_type IN ('lesson', 'list')
          AND is_cut = 0
        ORDER BY recommended_date, day_index, slot_index
        """
    ).fetchall()
    lessons = [to_dict(row) for row in rows]
    module_rows = build_un_module_rows(
        lessons=lessons,
        target_date=target_date,
        target_duration_seconds=daily_time_plan["target_duration_seconds"],
    )

    total_duration_seconds = sum(
        int(row.get("pending_duration_seconds") or 0)
        for row in module_rows
    )

    return {
        "rows": module_rows,
        "queue_size": len(module_rows),
        "pending_item_count": sum(int(row.get("pending_item_count") or 0) for row in module_rows),
        "total_duration_seconds": total_duration_seconds,
        "target_duration_seconds": daily_time_plan["target_duration_seconds"],
        "remaining_duration_seconds": daily_time_plan["remaining_duration_seconds"],
        "business_days_remaining": daily_time_plan["business_days_remaining"],
        "progress": progress,
        "end_date": fo_end_date.isoformat() if fo_end_date else None,
    }


def build_fo_exercise_section(conn: sqlite3.Connection, target_date: str) -> dict[str, Any]:
    return {
        "rows": fetch_fo_exercise_tasks_for_date(conn=conn, target_date=target_date),
        "counts": count_fo_exercise_tasks_for_date(conn=conn, target_date=target_date),
        "offset_days": get_exercise_offset_days(conn),
    }


def build_dashboard_progress(conn: sqlite3.Connection, target_date: str) -> dict[str, dict[str, Any]]:
    return {
        "FO": build_track_progress(conn=conn, track_code="FO", target_date=target_date),
        "UN": build_track_progress(conn=conn, track_code="UN", target_date=target_date),
    }


def build_overdue_recommendations(
    conn: sqlite3.Connection,
    target_date: str,
    exclude_lesson_codes: set[str],
    limit: int = 3,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM lessons
        WHERE lesson_type = 'lesson'
          AND track_code = 'FO'
          AND is_seen = 0
          AND is_cut = 0
          AND recommended_date < ?
        ORDER BY recommended_date, day_index, slot_index
        """,
        (target_date,),
    ).fetchall()

    recommendations: list[dict[str, Any]] = []
    for row in rows:
        lesson = to_dict(row)
        if lesson["lesson_code"] in exclude_lesson_codes:
            continue
        recommendations.append(lesson)
        if len(recommendations) >= limit:
            break

    return recommendations


def build_stats(conn: sqlite3.Connection, target_date: str) -> dict[str, int | float]:
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN track_code = 'FO' AND lesson_type = 'lesson' THEN 1
                   WHEN track_code = 'UN' AND lesson_type IN ('lesson', 'list') THEN 1
                   ELSE 0 END) AS total_lessons,
          SUM(CASE WHEN track_code = 'FO' AND lesson_type = 'lesson' AND is_seen = 1 THEN 1
                   WHEN track_code = 'UN' AND lesson_type IN ('lesson', 'list') AND is_seen = 1 THEN 1
                   ELSE 0 END) AS seen_lessons,
          SUM(CASE WHEN track_code = 'FO' AND lesson_type = 'lesson' AND recommended_date <= ? THEN 1
                   WHEN track_code = 'UN' AND lesson_type IN ('lesson', 'list') AND recommended_date <= ? THEN 1
                   ELSE 0 END) AS due_total,
          SUM(CASE WHEN track_code = 'FO' AND lesson_type = 'lesson' AND recommended_date <= ? AND is_seen = 1 THEN 1
                   WHEN track_code = 'UN' AND lesson_type IN ('lesson', 'list') AND recommended_date <= ? AND is_seen = 1 THEN 1
                   ELSE 0 END) AS due_seen
        FROM lessons
        WHERE is_cut = 0
        """,
        (target_date, target_date, target_date, target_date),
    ).fetchone()

    total_lessons = int(row["total_lessons"] or 0)
    seen_lessons = int(row["seen_lessons"] or 0)
    due_total = int(row["due_total"] or 0)
    due_seen = int(row["due_seen"] or 0)
    overdue = max(due_total - due_seen, 0)

    overall_percent = round((seen_lessons / total_lessons) * 100, 1) if total_lessons else 0.0

    return {
        "total_lessons": total_lessons,
        "seen_lessons": seen_lessons,
        "overall_percent": overall_percent,
        "due_total": due_total,
        "due_seen": due_seen,
        "overdue": overdue,
    }


def build_database_filter_clause(
    status_filter: str,
    track_filter: str,
    subject_filter: str,
    front_filter: str,
    search: str,
) -> tuple[str, list[Any]]:
    query = """
        FROM lessons
        WHERE 1 = 1
          AND is_cut = 0
    """
    params: list[Any] = []

    if status_filter == "seen":
        query += " AND is_seen = 1"
    elif status_filter == "unseen":
        query += " AND is_seen = 0"

    if track_filter in {"FO", "UN"}:
        query += " AND track_code = ?"
        params.append(track_filter)

    if subject_filter != "all":
        query += " AND subject_prefix = ?"
        params.append(subject_filter)

    if track_filter == "FO" and front_filter != "all":
        query += " AND module_label = ?"
        params.append(front_filter)

    trimmed_search = search.strip()
    if trimmed_search:
        like_value = f"%{trimmed_search}%"
        query += """
            AND (
                lesson_code LIKE ?
                OR title_raw LIKE ?
                OR COALESCE(portal_title, '') LIKE ?
                OR COALESCE(subject_prefix, '') LIKE ?
                OR COALESCE(subject_name, '') LIKE ?
            )
        """
        params.extend([like_value, like_value, like_value, like_value, like_value])

    return query, params


def count_database_rows(
    conn: sqlite3.Connection,
    status_filter: str,
    track_filter: str,
    subject_filter: str,
    front_filter: str,
    search: str,
) -> int:
    filter_clause, params = build_database_filter_clause(
        status_filter=status_filter,
        track_filter=track_filter,
        subject_filter=subject_filter,
        front_filter=front_filter,
        search=search,
    )
    row = conn.execute(f"SELECT COUNT(*) AS total {filter_clause}", params).fetchone()
    return int(row["total"] or 0)


def fetch_database_rows(
    conn: sqlite3.Connection,
    status_filter: str,
    track_filter: str,
    subject_filter: str,
    front_filter: str,
    search: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    filter_clause, params = build_database_filter_clause(
        status_filter=status_filter,
        track_filter=track_filter,
        subject_filter=subject_filter,
        front_filter=front_filter,
        search=search,
    )
    query = f"""
        SELECT *
        {filter_clause}
    """
    query += " ORDER BY recommended_date, day_index, slot_index"
    query += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(query, params).fetchall()
    return [to_dict(row) for row in rows]


def fetch_database_filter_options(conn: sqlite3.Connection) -> dict[str, Any]:
    subject_rows = conn.execute(
        """
        SELECT track_code, subject_prefix
        FROM lessons
        WHERE subject_prefix IS NOT NULL
          AND subject_prefix != ''
          AND is_cut = 0
        GROUP BY track_code, subject_prefix
        ORDER BY track_code, subject_prefix
        """
    ).fetchall()

    front_rows = conn.execute(
        """
        SELECT module_label
        FROM lessons
        WHERE track_code = 'FO'
          AND module_label IS NOT NULL
          AND module_label != ''
          AND is_cut = 0
        GROUP BY module_label
        ORDER BY module_number, module_label
        """
    ).fetchall()

    options = {
        "subjects": {
            "FO": [],
            "UN": [],
        },
        "fronts": [row["module_label"] for row in front_rows],
    }

    for row in subject_rows:
        track_code = row["track_code"]
        if track_code in options["subjects"]:
            options["subjects"][track_code].append(row["subject_prefix"])

    return options
