from __future__ import annotations

import json
import logging
import re
import sqlite3
from shutil import copy2
from dataclasses import asdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from secrets import token_hex
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import bleach
from markdown_it import MarkdownIt

from .paths import APP_DATA_ROOT, LEGACY_REVIEW_QUESTION_UPLOADS_DIR

logger = logging.getLogger(__name__)

REVIEW_QUESTION_STATE_DIR = APP_DATA_ROOT / "review_questions"
REVIEW_QUESTION_UPLOADS_DIR = REVIEW_QUESTION_STATE_DIR / "uploads"
REVIEW_QUESTION_UPLOADS_RELATIVE_DIR = Path("review_questions") / "uploads"
REVIEW_QUESTION_ALLOWED_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}
REVIEW_QUESTION_ALLOWED_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
}
REVIEW_QUESTION_MAX_IMAGE_BYTES = 10 * 1024 * 1024

REVIEW_QUESTION_SUBJECTS = (
    "Biologia",
    "Filosofia",
    "Física",
    "Geografia",
    "História",
    "Literatura",
    "Matemática",
    "Português",
    "Química",
    "Sociologia",
)
REVIEW_QUESTION_ERROR_REASONS = (
    "Desatenção",
    "Lacuna Teórica",
    "Erro de Cálculo",
    "Interpretação",
    "Outro",
)
REVIEW_QUESTION_DIFFICULTIES = (
    "Muito difícil",
    "Difícil",
    "Média",
    "Fácil",
    "Muito fácil",
)
REVIEW_QUESTION_RESULTS = (
    "correct",
    "wrong",
    "dont_know",
    "skipped_session",
    "postponed_tomorrow",
    "self_correct",
    "self_wrong",
)
REVIEW_QUESTION_OBJECTIVE_OPTIONS = ("A", "B", "C", "D", "E")

# Novas questões começam com o ritmo conservador de "ainda não sei".
REVIEW_QUESTION_INITIAL_RESULT = "dont_know"

REVIEW_QUESTION_SPACING_RULES = {
    "correct": {
        "Muito difícil": 1,
        "Difícil": 3,
        "Média": 7,
        "Fácil": 14,
        "Muito fácil": 30,
    },
    "wrong": {
        "Muito difícil": 1,
        "Difícil": 2,
        "Média": 3,
        "Fácil": 5,
        "Muito fácil": 7,
    },
}

REVIEW_QUESTION_SPECIAL_RESULT_RULES = {
    "skipped_session": 1,
    "postponed_tomorrow": 1,
}

REVIEW_QUESTION_ALLOWED_EXPLANATION_TAGS = (
    "p",
    "br",
    "strong",
    "em",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "blockquote",
    "code",
    "pre",
)
REVIEW_QUESTION_ALLOWED_EXPLANATION_ATTRIBUTES: dict[str, list[str]] = {}
REVIEW_QUESTION_DISPLAY_STATEMENT_LIMIT = 64

_REVIEW_QUESTION_MARKDOWN = MarkdownIt("commonmark", {"breaks": True, "html": False})

_MISSING = object()


@dataclass(frozen=True)
class ReviewQuestionCreateInput:
    subject: str
    difficulty: str
    title: str | None = None
    statement: str | None = None
    question_image_path: str | None = None
    question_image_paths: Sequence[str] | str | None = None
    answer_image_path: str | None = None
    error_reason: str | None = None
    tags: Sequence[str] | str | None = None
    is_objective: bool = True
    correct_option: str | None = None
    explanation: str | None = None
    is_suspended: bool = False


@dataclass(frozen=True)
class ReviewQuestionUpdateInput:
    title: Any = _MISSING
    statement: Any = _MISSING
    question_image_path: Any = _MISSING
    question_image_paths: Any = _MISSING
    answer_image_path: Any = _MISSING
    subject: Any = _MISSING
    error_reason: Any = _MISSING
    tags: Any = _MISSING
    difficulty: Any = _MISSING
    is_objective: Any = _MISSING
    correct_option: Any = _MISSING
    explanation: Any = _MISSING
    is_suspended: Any = _MISSING
    next_review_date: Any = _MISSING
    last_reviewed_at: Any = _MISSING


@dataclass(frozen=True)
class ReviewQuestionAttemptInput:
    question_id: int
    result: str
    selected_option: str | None = None
    reviewed_at: str | None = None
    difficulty_after: str | None = None
    next_review_date_after: str | None = None


@dataclass(frozen=True)
class ReviewQuestionUploadInput:
    filename: str
    content_type: str
    data: bytes


def ensure_review_question_storage_dirs() -> Path:
    REVIEW_QUESTION_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    copied_count = recover_legacy_review_question_uploads()
    if copied_count:
        logger.info(
            "review question uploads recovered from legacy state dir",
            extra={
                "copied_files": copied_count,
                "legacy_dir": str(LEGACY_REVIEW_QUESTION_UPLOADS_DIR),
                "target_dir": str(REVIEW_QUESTION_UPLOADS_DIR),
            },
        )
    return REVIEW_QUESTION_UPLOADS_DIR


def recover_legacy_review_question_uploads() -> int:
    legacy_dir = LEGACY_REVIEW_QUESTION_UPLOADS_DIR
    target_dir = REVIEW_QUESTION_UPLOADS_DIR
    if not legacy_dir.exists() or not legacy_dir.is_dir():
        return 0

    copied_count = 0
    for legacy_file in legacy_dir.rglob("*"):
        if not legacy_file.is_file():
            continue
        relative_path = legacy_file.relative_to(legacy_dir)
        target_path = target_dir / relative_path
        if target_path.exists():
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        copy2(legacy_file, target_path)
        copied_count += 1
    return copied_count


def current_local_date() -> date:
    try:
        tz = ZoneInfo("America/Sao_Paulo")
    except Exception:
        tz = timezone(timedelta(hours=-3))
    return datetime.now(tz).date()


def current_timestamp() -> str:
    try:
        tz = ZoneInfo("America/Sao_Paulo")
    except Exception:
        tz = timezone(timedelta(hours=-3))
    return datetime.now(tz).replace(tzinfo=None, microsecond=0).isoformat(sep=" ")


def calculate_review_question_next_review(
    difficulty: str,
    result: str,
    reference_date: date | str | None = None,
) -> str:
    normalized_difficulty = normalize_difficulty(difficulty)
    normalized_result = normalize_result(result)
    base_date = normalize_reference_date(reference_date)

    if normalized_result in {"correct", "self_correct"}:
        days = REVIEW_QUESTION_SPACING_RULES["correct"][normalized_difficulty]
    elif normalized_result in {"wrong", "dont_know", "self_wrong"}:
        days = REVIEW_QUESTION_SPACING_RULES["wrong"][normalized_difficulty]
    else:
        days = REVIEW_QUESTION_SPECIAL_RESULT_RULES[normalized_result]
    return (base_date + timedelta(days=days)).isoformat()


def create_review_question(
    conn: sqlite3.Connection,
    payload: ReviewQuestionCreateInput,
    *,
    reference_date: date | str | None = None,
) -> dict[str, Any]:
    ensure_review_question_storage_dirs()
    normalized = normalize_question_payload(
        {
            "title": payload.title,
            "statement": payload.statement,
            "question_image_path": payload.question_image_path,
            "question_image_paths": payload.question_image_paths,
            "answer_image_path": payload.answer_image_path,
            "subject": payload.subject,
            "error_reason": payload.error_reason,
            "tags": payload.tags,
            "difficulty": payload.difficulty,
            "is_objective": payload.is_objective,
            "correct_option": payload.correct_option,
            "explanation": payload.explanation,
            "is_suspended": payload.is_suspended,
        }
    )
    now = current_timestamp()
    next_review_date = calculate_review_question_next_review(
        normalized["difficulty"],
        REVIEW_QUESTION_INITIAL_RESULT,
        reference_date=reference_date,
    )
    cursor = conn.execute(
        """
        INSERT INTO review_questions (
            title,
            statement,
            question_image_path,
            question_image_paths,
            answer_image_path,
            subject,
            error_reason,
            tags,
            difficulty,
            is_objective,
            correct_option,
            explanation,
            is_suspended,
            next_review_date,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            normalized["title"],
            normalized["statement"],
            normalized["question_image_path"],
            normalized["question_image_paths"],
            normalized["answer_image_path"],
            normalized["subject"],
            normalized["error_reason"],
            normalized["tags"],
            normalized["difficulty"],
            normalized["is_objective"],
            normalized["correct_option"],
            normalized["explanation"],
            normalized["is_suspended"],
            next_review_date,
            now,
            now,
        ),
    )
    return get_review_question(conn, int(cursor.lastrowid))


def create_review_question_with_uploads(
    conn: sqlite3.Connection,
    payload: ReviewQuestionCreateInput,
    *,
    question_image_upload: ReviewQuestionUploadInput | None = None,
    question_image_uploads: Sequence[ReviewQuestionUploadInput] | None = None,
    answer_image_upload: ReviewQuestionUploadInput | None = None,
    reference_date: date | str | None = None,
) -> dict[str, Any]:
    saved_paths: list[str] = []
    try:
        question_image_paths = parse_review_question_image_paths(payload.question_image_paths or payload.question_image_path)
        answer_image_path = payload.answer_image_path

        uploads = list(question_image_uploads or [])
        if question_image_upload is not None:
            uploads.insert(0, question_image_upload)
        for upload in uploads:
            saved_path = save_review_question_upload(upload, upload_kind="question")
            question_image_paths.append(saved_path)
            saved_paths.append(saved_path)
        if answer_image_upload is not None:
            answer_image_path = save_review_question_upload(answer_image_upload, upload_kind="answer")
            saved_paths.append(answer_image_path)

        normalized_payload = ReviewQuestionCreateInput(
            **{
                **asdict(payload),
                "question_image_path": question_image_paths[0] if question_image_paths else None,
                "question_image_paths": question_image_paths,
                "answer_image_path": answer_image_path,
            }
        )
        return create_review_question(conn, normalized_payload, reference_date=reference_date)
    except Exception:
        for relative_path in saved_paths:
            delete_review_question_upload(relative_path)
        raise


def update_review_question(
    conn: sqlite3.Connection,
    question_id: int,
    payload: ReviewQuestionUpdateInput,
) -> dict[str, Any]:
    existing = get_review_question_row(conn, question_id)
    changes = {
        key: value
        for key, value in payload.__dict__.items()
        if value is not _MISSING
    }
    if not changes:
        return row_to_review_question(existing)

    merged = {
        "title": changes.get("title", existing["title"]),
        "statement": changes.get("statement", existing["statement"]),
        "question_image_path": changes.get("question_image_path", existing["question_image_path"]),
        "question_image_paths": changes.get("question_image_paths", existing["question_image_paths"]),
        "answer_image_path": changes.get("answer_image_path", existing["answer_image_path"]),
        "subject": changes.get("subject", existing["subject"]),
        "error_reason": changes.get("error_reason", existing["error_reason"]),
        "tags": changes.get("tags", parse_review_question_tags(existing["tags"])),
        "difficulty": changes.get("difficulty", existing["difficulty"]),
        "is_objective": changes.get("is_objective", bool(existing["is_objective"])),
        "correct_option": changes.get("correct_option", existing["correct_option"]),
        "explanation": changes.get("explanation", existing["explanation"]),
        "is_suspended": changes.get("is_suspended", bool(existing["is_suspended"])),
    }
    normalized = normalize_question_payload(merged)

    next_review_date = changes.get("next_review_date", existing["next_review_date"])
    if next_review_date not in {None, ""}:
        next_review_date = normalize_iso_date(next_review_date, "next_review_date")
    else:
        next_review_date = None

    last_reviewed_at = changes.get("last_reviewed_at", existing["last_reviewed_at"])
    if last_reviewed_at not in {None, ""}:
        last_reviewed_at = normalize_datetime_string(last_reviewed_at, "last_reviewed_at")
    else:
        last_reviewed_at = None

    conn.execute(
        """
        UPDATE review_questions
        SET title = ?,
            statement = ?,
            question_image_path = ?,
            question_image_paths = ?,
            answer_image_path = ?,
            subject = ?,
            error_reason = ?,
            tags = ?,
            difficulty = ?,
            is_objective = ?,
            correct_option = ?,
            explanation = ?,
            is_suspended = ?,
            next_review_date = ?,
            last_reviewed_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            normalized["title"],
            normalized["statement"],
            normalized["question_image_path"],
            normalized["question_image_paths"],
            normalized["answer_image_path"],
            normalized["subject"],
            normalized["error_reason"],
            normalized["tags"],
            normalized["difficulty"],
            normalized["is_objective"],
            normalized["correct_option"],
            normalized["explanation"],
            normalized["is_suspended"],
            next_review_date,
            last_reviewed_at,
            current_timestamp(),
            question_id,
        ),
    )
    return get_review_question(conn, question_id)


def update_review_question_with_uploads(
    conn: sqlite3.Connection,
    question_id: int,
    payload: ReviewQuestionUpdateInput,
    *,
    question_image_upload: ReviewQuestionUploadInput | None = None,
    question_image_uploads: Sequence[ReviewQuestionUploadInput] | None = None,
    removed_question_image_paths: Sequence[str] | None = None,
    answer_image_upload: ReviewQuestionUploadInput | None = None,
) -> dict[str, Any]:
    saved_paths: list[str] = []
    try:
        existing = get_review_question(conn, question_id)
        changes = dict(payload.__dict__)
        removed_paths = normalize_review_question_upload_relative_paths(removed_question_image_paths)
        uploads = list(question_image_uploads or [])
        if question_image_upload is not None:
            uploads.insert(0, question_image_upload)
        if uploads or removed_paths:
            raw_paths = changes.get("question_image_paths", existing.get("question_image_paths"))
            if raw_paths is _MISSING:
                raw_paths = existing.get("question_image_paths")
            merged_paths = parse_review_question_image_paths(raw_paths)
            if removed_paths:
                merged_paths = [path for path in merged_paths if path not in removed_paths]
            for upload in uploads:
                saved_path = save_review_question_upload(upload, upload_kind="question")
                merged_paths.append(saved_path)
                saved_paths.append(saved_path)
            changes["question_image_paths"] = merged_paths
            changes["question_image_path"] = merged_paths[0] if merged_paths else None
        if answer_image_upload is not None:
            changes["answer_image_path"] = save_review_question_upload(answer_image_upload, upload_kind="answer")
            saved_paths.append(str(changes["answer_image_path"]))
        return update_review_question(conn, question_id, ReviewQuestionUpdateInput(**changes))
    except Exception:
        for relative_path in saved_paths:
            delete_review_question_upload(relative_path)
        raise


def suspend_review_question(conn: sqlite3.Connection, question_id: int) -> dict[str, Any]:
    return set_review_question_suspended(conn, question_id, is_suspended=True)


def reactivate_review_question(conn: sqlite3.Connection, question_id: int) -> dict[str, Any]:
    return set_review_question_suspended(conn, question_id, is_suspended=False)


def set_review_question_suspended(
    conn: sqlite3.Connection,
    question_id: int,
    *,
    is_suspended: bool,
) -> dict[str, Any]:
    get_review_question_row(conn, question_id)
    conn.execute(
        """
        UPDATE review_questions
        SET is_suspended = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (1 if is_suspended else 0, current_timestamp(), question_id),
    )
    return get_review_question(conn, question_id)


def delete_review_question(conn: sqlite3.Connection, question_id: int) -> dict[str, Any]:
    row = get_review_question_row(conn, question_id)
    deleted_question = row_to_review_question(row)
    referenced_paths = collect_review_question_upload_paths(row)
    removable_paths = [
        path
        for path in referenced_paths
        if not is_review_question_upload_referenced_elsewhere(conn, path, excluding_question_id=question_id)
    ]

    conn.execute("DELETE FROM review_questions WHERE id = ?", (question_id,))
    for relative_path in removable_paths:
        delete_review_question_upload(relative_path)
    return deleted_question


def list_pending_review_questions(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    review_date = normalize_reference_date(as_of_date).isoformat()
    params: list[Any] = [review_date]
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(max(limit, 0))

    rows = conn.execute(
        f"""
        SELECT *
        FROM review_questions
        WHERE is_suspended = 0
          AND (
            next_review_date IS NULL
            OR next_review_date = ''
            OR next_review_date <= ?
          )
        ORDER BY COALESCE(NULLIF(next_review_date, ''), '0001-01-01'), created_at, id
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [row_to_review_question(row) for row in rows]


def list_review_questions_due_for_review(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str | None = None,
    excluded_ids: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    review_date = normalize_reference_date(as_of_date).isoformat()
    excluded_values = [int(value) for value in (excluded_ids or []) if int(value) > 0]
    query = """
        SELECT *
        FROM review_questions
        WHERE is_suspended = 0
          AND next_review_date <= ?
    """
    params: list[Any] = [review_date]
    if excluded_values:
        placeholders = ", ".join("?" for _ in excluded_values)
        query += f" AND id NOT IN ({placeholders})"
        params.extend(excluded_values)
    query += " ORDER BY next_review_date ASC, created_at ASC, id ASC"
    rows = conn.execute(query, tuple(params)).fetchall()
    return [row_to_review_question(row) for row in rows]


def count_pending_review_questions(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str | None = None,
) -> int:
    review_date = normalize_reference_date(as_of_date).isoformat()
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM review_questions
        WHERE is_suspended = 0
          AND next_review_date <= ?
        """,
        (review_date,),
    ).fetchone()
    return int(row[0] if row else 0)


def list_recent_review_questions(
    conn: sqlite3.Connection,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM review_questions
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (max(limit, 0),),
    ).fetchall()
    return [row_to_review_question(row) for row in rows]


def count_review_questions_by_filter(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str | None = None,
    status_filter: str = "all",
    subject_filter: str = "all",
    difficulty_filter: str = "all",
    error_reason_filter: str = "all",
    tag_filter: str = "",
    search: str = "",
) -> int:
    review_date = normalize_reference_date(as_of_date).isoformat()
    where_sql, params = build_review_question_filter_where(
        review_date=review_date,
        status_filter=status_filter,
        subject_filter=subject_filter,
        difficulty_filter=difficulty_filter,
        error_reason_filter=error_reason_filter,
        tag_filter=tag_filter,
        search=search,
    )
    row = conn.execute(f"SELECT COUNT(*) FROM review_questions {where_sql}", params).fetchone()
    return int(row[0] if row else 0)


def fetch_review_questions_by_filter(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str | None = None,
    status_filter: str = "all",
    subject_filter: str = "all",
    difficulty_filter: str = "all",
    error_reason_filter: str = "all",
    tag_filter: str = "",
    search: str = "",
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    review_date = normalize_reference_date(as_of_date).isoformat()
    where_sql, params = build_review_question_filter_where(
        review_date=review_date,
        status_filter=status_filter,
        subject_filter=subject_filter,
        difficulty_filter=difficulty_filter,
        error_reason_filter=error_reason_filter,
        tag_filter=tag_filter,
        search=search,
    )
    query = f"""
        SELECT *
        FROM review_questions
        {where_sql}
        ORDER BY
            is_suspended DESC,
            CASE
              WHEN next_review_date IS NULL OR next_review_date = '' THEN 1
              ELSE 0
            END ASC,
            next_review_date ASC,
            created_at DESC,
            id DESC
    """
    final_params = [*params]
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        final_params.extend([max(limit, 0), max(offset, 0)])
    rows = conn.execute(query, tuple(final_params)).fetchall()
    return [row_to_review_question(row) for row in rows]


def build_review_question_filter_where(
    *,
    review_date: str,
    status_filter: str,
    subject_filter: str,
    difficulty_filter: str,
    error_reason_filter: str,
    tag_filter: str,
    search: str,
) -> tuple[str, tuple[Any, ...]]:
    clauses: list[str] = []
    params: list[Any] = []

    if status_filter == "pending_today":
        clauses.append("is_suspended = 0 AND next_review_date = ?")
        params.append(review_date)
    elif status_filter == "overdue":
        clauses.append("is_suspended = 0 AND next_review_date < ?")
        params.append(review_date)
    elif status_filter == "future":
        clauses.append("is_suspended = 0 AND next_review_date > ?")
        params.append(review_date)
    elif status_filter == "suspended":
        clauses.append("is_suspended = 1")
    elif status_filter == "no_date":
        clauses.append("is_suspended = 0 AND (next_review_date IS NULL OR next_review_date = '')")

    if subject_filter in REVIEW_QUESTION_SUBJECTS:
        clauses.append("subject = ?")
        params.append(subject_filter)

    if difficulty_filter in REVIEW_QUESTION_DIFFICULTIES:
        clauses.append("difficulty = ?")
        params.append(difficulty_filter)

    if error_reason_filter in REVIEW_QUESTION_ERROR_REASONS:
        clauses.append("error_reason = ?")
        params.append(error_reason_filter)

    normalized_tag = str(tag_filter or "").strip().lower()
    if normalized_tag:
        clauses.append("LOWER(COALESCE(tags, '')) LIKE ?")
        params.append(f"%{normalized_tag}%")

    normalized_search = str(search or "").strip().lower()
    if normalized_search:
        clauses.append(
            "("
            "LOWER(COALESCE(title, '')) LIKE ? OR "
            "LOWER(COALESCE(statement, '')) LIKE ? OR "
            "LOWER(COALESCE(explanation, '')) LIKE ?"
            ")"
        )
        search_value = f"%{normalized_search}%"
        params.extend([search_value, search_value, search_value])

    if not clauses:
        return "", ()
    return "WHERE " + " AND ".join(clauses), tuple(params)


def review_question_status(question: sqlite3.Row | dict[str, Any], *, as_of_date: date | str | None = None) -> str:
    target_date = normalize_reference_date(as_of_date).isoformat()
    is_suspended = bool(question["is_suspended"] if isinstance(question, sqlite3.Row) else question.get("is_suspended"))
    next_review_date = question["next_review_date"] if isinstance(question, sqlite3.Row) else question.get("next_review_date")

    if is_suspended:
        return "suspended"
    if not next_review_date:
        return "no_date"
    if str(next_review_date) < target_date:
        return "overdue"
    if str(next_review_date) == target_date:
        return "today"
    return "future"


def count_review_question_dashboard_stats(
    conn: sqlite3.Connection,
    *,
    as_of_date: date | str | None = None,
) -> dict[str, int]:
    review_date = normalize_reference_date(as_of_date).isoformat()
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN is_suspended = 0 AND next_review_date = ? THEN 1 ELSE 0 END) AS due_today,
            SUM(CASE WHEN is_suspended = 0 AND next_review_date < ? THEN 1 ELSE 0 END) AS overdue,
            SUM(CASE WHEN is_suspended = 0 AND next_review_date <= ? THEN 1 ELSE 0 END) AS pending_total
        FROM review_questions
        """,
        (review_date, review_date, review_date),
    ).fetchone()
    return {
        # Itens sem data ficam fora do card da home para evitar contagem ambígua.
        "due_today": int(row["due_today"] or 0),
        "overdue": int(row["overdue"] or 0),
        "pending_total": int(row["pending_total"] or 0),
    }


def register_review_question_attempt(
    conn: sqlite3.Connection,
    payload: ReviewQuestionAttemptInput,
    *,
    increment_review_count: bool = True,
) -> dict[str, Any]:
    question = get_review_question_row(conn, payload.question_id)
    result = normalize_result(payload.result)
    selected_option = normalize_option(payload.selected_option, allow_empty=True)
    reviewed_at = normalize_datetime_string(payload.reviewed_at or current_timestamp(), "reviewed_at")
    reviewed_on = datetime.fromisoformat(reviewed_at.replace(" ", "T")).date()
    difficulty_after = normalize_difficulty(payload.difficulty_after or question["difficulty"])
    next_review_date_after = payload.next_review_date_after
    if next_review_date_after in {None, ""}:
        next_review_date_after = calculate_review_question_next_review(
            difficulty_after,
            result,
            reference_date=reviewed_on,
        )
    else:
        next_review_date_after = normalize_iso_date(next_review_date_after, "next_review_date_after")

    conn.execute(
        """
        INSERT INTO review_question_attempts (
            question_id,
            reviewed_at,
            selected_option,
            result,
            difficulty_after,
            next_review_date_after
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            payload.question_id,
            reviewed_at,
            selected_option,
            result,
            difficulty_after,
            next_review_date_after,
        ),
    )

    correct_increment = 1 if result in {"correct", "self_correct"} else 0
    wrong_increment = 1 if result in {"wrong", "self_wrong"} else 0
    dont_know_increment = 1 if result == "dont_know" else 0

    conn.execute(
        """
        UPDATE review_questions
        SET difficulty = ?,
            next_review_date = ?,
            last_reviewed_at = ?,
            review_count = review_count + ?,
            correct_count = correct_count + ?,
            wrong_count = wrong_count + ?,
            dont_know_count = dont_know_count + ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            difficulty_after,
            next_review_date_after,
            reviewed_at,
            1 if increment_review_count else 0,
            correct_increment,
            wrong_increment,
            dont_know_increment,
            current_timestamp(),
            payload.question_id,
        ),
    )
    return get_review_question(conn, payload.question_id)


def postpone_review_question_to_tomorrow(
    conn: sqlite3.Connection,
    question_id: int,
    *,
    reviewed_at: str | None = None,
) -> dict[str, Any]:
    question = get_review_question_row(conn, question_id)
    timestamp = normalize_datetime_string(reviewed_at or current_timestamp(), "reviewed_at")
    next_review_date = (datetime.fromisoformat(timestamp.replace(" ", "T")).date() + timedelta(days=1)).isoformat()
    conn.execute(
        """
        INSERT INTO review_question_attempts (
            question_id,
            reviewed_at,
            selected_option,
            result,
            difficulty_after,
            next_review_date_after
        ) VALUES (?, ?, NULL, 'postponed_tomorrow', ?, ?)
        """,
        (
            question_id,
            timestamp,
            question["difficulty"],
            next_review_date,
        ),
    )
    conn.execute(
        """
        UPDATE review_questions
        SET next_review_date = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            next_review_date,
            current_timestamp(),
            question_id,
        ),
    )
    return get_review_question(conn, question_id)


def suspend_review_question_for_review(
    conn: sqlite3.Connection,
    question_id: int,
) -> dict[str, Any]:
    return set_review_question_suspended(conn, question_id, is_suspended=True)


def reactivate_review_question_for_listing(
    conn: sqlite3.Connection,
    question_id: int,
    *,
    reference_date: date | str | None = None,
) -> dict[str, Any]:
    question = get_review_question_row(conn, question_id)
    next_review_date = question["next_review_date"]
    if not next_review_date:
        # Ao reativar sem data, a opção mais segura é recolocar a questão em hoje.
        next_review_date = normalize_reference_date(reference_date).isoformat()
    conn.execute(
        """
        UPDATE review_questions
        SET is_suspended = 0,
            next_review_date = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            next_review_date,
            current_timestamp(),
            question_id,
        ),
    )
    return get_review_question(conn, question_id)


def get_review_question(conn: sqlite3.Connection, question_id: int) -> dict[str, Any]:
    return row_to_review_question(get_review_question_row(conn, question_id))


def get_review_question_row(conn: sqlite3.Connection, question_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM review_questions WHERE id = ?",
        (question_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Questao de revisao nao encontrada: {question_id}")
    return row


def collect_review_question_upload_paths(question: sqlite3.Row | dict[str, Any]) -> list[str]:
    if isinstance(question, sqlite3.Row):
        raw_question_image_paths = question["question_image_paths"]
        raw_question_image_path = question["question_image_path"]
        raw_answer_image_path = question["answer_image_path"]
    else:
        raw_question_image_paths = question.get("question_image_paths")
        raw_question_image_path = question.get("question_image_path")
        raw_answer_image_path = question.get("answer_image_path")

    collected_paths: list[str] = []
    for candidate in _iter_raw_review_question_upload_path_candidates(
        raw_question_image_paths=raw_question_image_paths,
        raw_question_image_path=raw_question_image_path,
        raw_answer_image_path=raw_answer_image_path,
    ):
        try:
            normalized = normalize_review_question_upload_relative_path(candidate).as_posix()
        except ValueError:
            continue
        if normalized not in collected_paths:
            collected_paths.append(normalized)
    return collected_paths


def is_review_question_upload_referenced_elsewhere(
    conn: sqlite3.Connection,
    relative_path: str,
    *,
    excluding_question_id: int,
) -> bool:
    normalized = normalize_review_question_upload_relative_path(relative_path).as_posix()
    rows = conn.execute(
        """
        SELECT id, question_image_path, question_image_paths, answer_image_path
        FROM review_questions
        WHERE id <> ?
        """,
        (excluding_question_id,),
    ).fetchall()
    for row in rows:
        other_paths = collect_review_question_upload_paths(row)
        if normalized in other_paths:
            return True
    return False


def row_to_review_question(row: sqlite3.Row) -> dict[str, Any]:
    data = {key: row[key] for key in row.keys()}
    data["tags"] = parse_review_question_tags(data.get("tags"))
    data["question_image_paths"] = collect_review_question_upload_paths(row)
    data["question_image_path"] = data["question_image_paths"][0] if data["question_image_paths"] else None
    data["is_objective"] = bool(data["is_objective"])
    data["is_suspended"] = bool(data["is_suspended"])
    data["display_title"] = get_review_question_display_title(data)
    return data


def parse_review_question_tags(raw_value: str | Sequence[str] | None) -> list[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        if not raw_value.strip():
            return []
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            parsed = [part.strip() for part in raw_value.split(",")]
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [str(parsed).strip()] if str(parsed).strip() else []
    return [str(item).strip() for item in raw_value if str(item).strip()]


def serialize_review_question_tags(tags: Sequence[str] | str | None) -> str | None:
    parsed_tags = parse_review_question_tags(tags)
    return json.dumps(parsed_tags, ensure_ascii=False) if parsed_tags else None


def normalize_review_question_upload_relative_paths(values: Sequence[str] | None) -> list[str]:
    normalized_paths: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        normalized = normalize_review_question_upload_relative_path(text).as_posix()
        if normalized not in normalized_paths:
            normalized_paths.append(normalized)
    return normalized_paths


def list_existing_review_question_tags(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT tags
        FROM review_questions
        WHERE tags IS NOT NULL
          AND tags <> ''
        """
    ).fetchall()
    unique_tags: dict[str, str] = {}
    for row in rows:
        for raw_tag in parse_review_question_tags(row["tags"]):
            normalized_tag = " ".join(str(raw_tag).split()).strip()
            if not normalized_tag:
                continue
            unique_tags.setdefault(normalized_tag.casefold(), normalized_tag)
    return sorted(unique_tags.values(), key=lambda value: value.casefold())


def parse_review_question_image_paths(
    raw_value: Sequence[str] | str | None,
    *,
    fallback_path: str | None = None,
) -> list[str]:
    paths: list[str] = []
    if raw_value is None:
        paths = []
    elif isinstance(raw_value, str):
        if raw_value.strip():
            try:
                parsed = json.loads(raw_value)
            except json.JSONDecodeError:
                parsed = [raw_value]
            if isinstance(parsed, list):
                paths = [normalize_review_question_upload_relative_path(item).as_posix() for item in parsed if str(item).strip()]
            elif str(parsed).strip():
                paths = [normalize_review_question_upload_relative_path(parsed).as_posix()]
    else:
        paths = [normalize_review_question_upload_relative_path(item).as_posix() for item in raw_value if str(item).strip()]

    if not paths and fallback_path:
        paths = [normalize_review_question_upload_relative_path(fallback_path).as_posix()]

    deduped: list[str] = []
    for path in paths:
        if path not in deduped:
            deduped.append(path)
    return deduped


def _iter_raw_review_question_upload_path_candidates(
    *,
    raw_question_image_paths: Sequence[str] | str | None,
    raw_question_image_path: str | None,
    raw_answer_image_path: str | None,
) -> list[str]:
    candidates: list[str] = []
    raw_candidates: list[str] = []

    if raw_question_image_paths is not None:
        if isinstance(raw_question_image_paths, str):
            if raw_question_image_paths.strip():
                try:
                    parsed = json.loads(raw_question_image_paths)
                except json.JSONDecodeError:
                    parsed = [raw_question_image_paths]
                if isinstance(parsed, list):
                    raw_candidates.extend(str(item).strip() for item in parsed if str(item).strip())
                elif str(parsed).strip():
                    raw_candidates.append(str(parsed).strip())
        else:
            raw_candidates.extend(str(item).strip() for item in raw_question_image_paths if str(item).strip())

    for maybe_path in (raw_question_image_path, raw_answer_image_path):
        text = str(maybe_path or "").strip()
        if text:
            raw_candidates.append(text)

    for candidate in raw_candidates:
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def serialize_review_question_image_paths(paths: Sequence[str] | str | None) -> str | None:
    parsed_paths = parse_review_question_image_paths(paths)
    return json.dumps(parsed_paths, ensure_ascii=False) if parsed_paths else None


def get_review_question_display_title(question: sqlite3.Row | dict[str, Any]) -> str:
    subject = str(question["subject"] if isinstance(question, sqlite3.Row) else question.get("subject") or "Questão").strip()
    statement = question["statement"] if isinstance(question, sqlite3.Row) else question.get("statement")
    normalized_statement = normalize_optional_text(statement)
    if normalized_statement:
        compact = " ".join(normalized_statement.split())
        snippet = compact[:REVIEW_QUESTION_DISPLAY_STATEMENT_LIMIT].rstrip()
        if len(compact) > REVIEW_QUESTION_DISPLAY_STATEMENT_LIMIT:
            snippet += "..."
        return f"{subject} · {snippet}"
    return f"{subject} · questão com imagem"


def render_review_question_explanation_html(raw_text: str | None) -> str | None:
    text = normalize_optional_text(raw_text)
    if text is None:
        return None
    text = re.sub(r"<script\b[^>]*>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL)
    rendered = _REVIEW_QUESTION_MARKDOWN.render(text)
    sanitized = bleach.clean(
        rendered,
        tags=REVIEW_QUESTION_ALLOWED_EXPLANATION_TAGS,
        attributes=REVIEW_QUESTION_ALLOWED_EXPLANATION_ATTRIBUTES,
        strip=True,
    )
    return sanitized or None


def save_review_question_upload(
    upload: ReviewQuestionUploadInput,
    *,
    upload_kind: str,
) -> str:
    ensure_review_question_storage_dirs()
    if upload_kind not in {"question", "answer"}:
        raise ValueError(f"Tipo de upload inválido: {upload_kind}")

    normalized_name = Path(upload.filename or "").name.strip()
    if not normalized_name:
        raise ValueError("Arquivo de imagem inválido: nome ausente.")
    extension = Path(normalized_name).suffix.lower()
    content_type = str(upload.content_type or "").strip().lower()

    if extension not in REVIEW_QUESTION_ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("Formato de imagem inválido. Use png, jpg, jpeg ou webp.")
    if content_type not in REVIEW_QUESTION_ALLOWED_IMAGE_MIME_TYPES:
        raise ValueError("Tipo de arquivo inválido. Use png, jpg, jpeg ou webp.")
    if len(upload.data) > REVIEW_QUESTION_MAX_IMAGE_BYTES:
        raise ValueError("Imagem excede o limite de 10 MB.")
    if len(upload.data) == 0:
        raise ValueError("Imagem vazia. Selecione ou cole um arquivo válido.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = f"{upload_kind}-{stamp}-{token_hex(8)}{extension}"
    relative_path = REVIEW_QUESTION_UPLOADS_RELATIVE_DIR / safe_name
    target_path = resolve_review_question_upload_path(str(relative_path))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(upload.data)
    return relative_path.as_posix()


def resolve_review_question_upload_path(relative_path: str | Path) -> Path:
    normalized = normalize_review_question_upload_relative_path(relative_path)
    return APP_DATA_ROOT / normalized


def normalize_review_question_upload_relative_path(relative_path: str | Path) -> Path:
    normalized = Path(str(relative_path).strip())
    if normalized.is_absolute():
        raise ValueError("O caminho da imagem deve ser relativo ao diretório persistente de dados da aplicação.")
    if ".." in normalized.parts:
        raise ValueError("Caminho de imagem inválido.")
    if len(normalized.parts) < 3:
        raise ValueError("Caminho de imagem inválido.")
    if tuple(normalized.parts[:2]) != tuple(REVIEW_QUESTION_UPLOADS_RELATIVE_DIR.parts):
        raise ValueError("O caminho da imagem deve ficar em review_questions/uploads.")
    if normalized.suffix.lower() not in REVIEW_QUESTION_ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("Formato de imagem inválido. Use png, jpg, jpeg ou webp.")
    return normalized


def delete_review_question_upload(relative_path: str | Path | None) -> None:
    if relative_path in {None, ""}:
        return
    try:
        target_path = resolve_review_question_upload_path(str(relative_path))
    except ValueError:
        return
    try:
        target_path.unlink(missing_ok=True)
    except OSError:
        pass


def normalize_question_payload(payload: dict[str, Any]) -> dict[str, Any]:
    subject = normalize_subject(payload.get("subject"))
    difficulty = normalize_difficulty(payload.get("difficulty"))
    error_reason = normalize_error_reason(payload.get("error_reason"))
    is_objective = 1 if bool(payload.get("is_objective", True)) else 0
    correct_option = normalize_option(payload.get("correct_option"), allow_empty=True)

    statement = normalize_optional_text(payload.get("statement"))
    question_image_paths = parse_review_question_image_paths(
        payload.get("question_image_paths"),
        fallback_path=normalize_optional_text(payload.get("question_image_path")),
    )
    question_image_path = question_image_paths[0] if question_image_paths else None
    if statement is None and question_image_path is None:
        raise ValueError("Informe statement ou ao menos uma imagem da questão.")

    if is_objective:
        if correct_option is None:
            raise ValueError("Questões objetivas exigem correct_option entre A e E.")
    else:
        correct_option = None

    return {
        "title": normalize_optional_text(payload.get("title")),
        "statement": statement,
        "question_image_path": question_image_path,
        "question_image_paths": serialize_review_question_image_paths(question_image_paths),
        "answer_image_path": normalize_optional_path(payload.get("answer_image_path")),
        "subject": subject,
        "error_reason": error_reason,
        "tags": serialize_review_question_tags(payload.get("tags")),
        "difficulty": difficulty,
        "is_objective": is_objective,
        "correct_option": correct_option,
        "explanation": normalize_optional_text(payload.get("explanation")),
        "is_suspended": 1 if bool(payload.get("is_suspended", False)) else 0,
    }


def normalize_subject(value: Any) -> str:
    subject = normalize_required_text(value, "subject")
    if subject not in REVIEW_QUESTION_SUBJECTS:
        raise ValueError(f"Matéria inválida: {subject}")
    return subject


def normalize_error_reason(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    reason = normalize_required_text(value, "error_reason")
    if reason not in REVIEW_QUESTION_ERROR_REASONS:
        raise ValueError(f"Motivo do erro inválido: {reason}")
    return reason


def normalize_difficulty(value: Any) -> str:
    difficulty = normalize_required_text(value, "difficulty")
    if difficulty not in REVIEW_QUESTION_DIFFICULTIES:
        raise ValueError(f"Dificuldade inválida: {difficulty}")
    return difficulty


def normalize_result(value: Any) -> str:
    result = normalize_required_text(value, "result")
    if result not in REVIEW_QUESTION_RESULTS:
        raise ValueError(f"Resultado inválido: {result}")
    return result


def normalize_option(value: Any, *, allow_empty: bool) -> str | None:
    if value in {None, ""}:
        if allow_empty:
            return None
        raise ValueError("Alternativa obrigatória ausente.")
    option = normalize_required_text(value, "selected_option").upper()
    if option not in REVIEW_QUESTION_OBJECTIVE_OPTIONS:
        raise ValueError(f"Alternativa inválida: {option}")
    return option


def normalize_reference_date(value: date | str | None) -> date:
    if value is None:
        return current_local_date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(normalize_iso_date(value, "reference_date"))


def normalize_iso_date(value: Any, field_name: str) -> str:
    normalized = normalize_required_text(value, field_name)
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError(f"Data inválida para {field_name}. Use YYYY-MM-DD.") from exc


def normalize_datetime_string(value: Any, field_name: str) -> str:
    normalized = normalize_required_text(value, field_name).replace("T", " ")
    try:
        return datetime.fromisoformat(normalized.replace(" ", "T")).replace(microsecond=0).isoformat(sep=" ")
    except ValueError as exc:
        raise ValueError(f"Data/hora inválida para {field_name}.") from exc


def normalize_required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Campo obrigatório ausente: {field_name}")
    return text


def normalize_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def normalize_optional_path(value: Any) -> str | None:
    text = normalize_optional_text(value)
    if text is None:
        return None
    return normalize_review_question_upload_relative_path(text).as_posix()
