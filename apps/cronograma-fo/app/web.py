from __future__ import annotations

import os
from calendar import monthrange
from datetime import date, datetime
from math import ceil
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, after_this_request, jsonify, redirect, render_template, request, send_file, url_for

from .dashboard import (
    build_fo_exercise_section,
    build_dashboard_progress,
    build_overdue_recommendations,
    build_schedule_summary,
    build_stats,
    build_today_rows,
    build_universe_narrado_section,
    count_database_rows,
    derive_un_module_path,
    fetch_database_filter_options,
    fetch_database_rows,
    lesson_label,
    parse_iso_date,
)
from .backup import create_backup_archive
from .db import DEFAULT_DB_PATH, connect_db, validate_runtime_database
from .exercises import (
    activate_exercise_for_lesson,
    deactivate_exercise_for_lesson,
    reschedule_unmoved_exercise_tasks,
    reschedule_exercise_task,
    set_exercise_offset_days,
    sync_fo_exercise_tasks,
    update_exercise_status,
)
from .reprogramming import (
    add_unavailability,
    apply_reprogramming,
    build_reprogram_report,
    current_local_date,
    current_simulation_token,
    format_date as format_iso_date,
    get_schedule_settings,
    list_unavailability,
    merge_settings,
    remove_unavailability,
    save_schedule_settings,
)
from .review_questions import (
    REVIEW_QUESTION_ALLOWED_IMAGE_EXTENSIONS,
    REVIEW_QUESTION_ALLOWED_IMAGE_MIME_TYPES,
    REVIEW_QUESTION_DIFFICULTIES,
    REVIEW_QUESTION_ERROR_REASONS,
    REVIEW_QUESTION_MAX_IMAGE_BYTES,
    REVIEW_QUESTION_OBJECTIVE_OPTIONS,
    REVIEW_QUESTION_SUBJECTS,
    ReviewQuestionCreateInput,
    ReviewQuestionAttemptInput,
    ReviewQuestionUploadInput,
    ReviewQuestionUpdateInput,
    count_review_question_dashboard_stats,
    count_review_questions_by_filter,
    count_pending_review_questions,
    create_review_question_with_uploads,
    delete_review_question,
    fetch_review_questions_by_filter,
    get_review_question_display_title,
    get_review_question,
    list_review_questions_due_for_review,
    list_existing_review_question_tags,
    postpone_review_question_to_tomorrow,
    reactivate_review_question_for_listing,
    register_review_question_attempt,
    render_review_question_explanation_html,
    review_question_status,
    resolve_review_question_upload_path,
    suspend_review_question_for_review,
    update_review_question_with_uploads,
)

DATABASE_PAGE_SIZE = 100
REVIEW_QUESTIONS_PAGE_SIZE = 50
VALID_FO_VIEWS = {"aulas", "exercicios", "tudo"}


def safe_return_to(path: str | None, fallback: str) -> str:
    if path and path.startswith("/") and not path.startswith("//"):
        return path
    return fallback


def normalize_fo_view(value: str | None, default: str = "aulas") -> str:
    return value if value in VALID_FO_VIEWS else default


def format_br_date(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return date.fromisoformat(value).strftime("%d/%m/%Y")
    except ValueError:
        return value


def format_br_datetime(value: str | None) -> str:
    if not value:
        return "-"
    normalized = value.replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return value


def format_duration(value: int | float | str | None) -> str:
    if value in {None, ""}:
        return "-"
    try:
        total_seconds = int(round(float(value)))
    except (TypeError, ValueError):
        return "-"
    if total_seconds <= 0:
        return "0m"

    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60

    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    if minutes > 0:
        return f"{minutes}m"
    return "< 1m"


def parse_positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value or "")
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def parse_non_negative_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value or "")
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def parse_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value in {None, ""}:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "sim", "yes", "on"}:
        return True
    if normalized in {"0", "false", "nao", "não", "no", "off"}:
        return False
    return default


def parse_required_iso_date(value: object, field_name: str) -> date:
    if value in {None, ""}:
        raise ValueError(f"Campo obrigatório ausente: {field_name}.")
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"Data inválida para {field_name}. Use YYYY-MM-DD.") from exc


def parse_optional_iso_date(value: object) -> date | None:
    if value in {None, ""}:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("Data inválida. Use YYYY-MM-DD.") from exc


def json_error(message: str, status_code: int = 400):
    response = jsonify({"ok": False, "error": message})
    response.status_code = status_code
    return response


def serialize_settings(settings) -> dict[str, object]:
    effective_target = settings.effective_target_finish_date()
    return {
        "exam_date": format_iso_date(settings.exam_date),
        "target_finish_date": format_iso_date(settings.target_finish_date),
        "effective_target_finish_date": format_iso_date(effective_target),
        "include_weekends": settings.include_weekends,
    }


def serialize_unavailability_entry(entry) -> dict[str, object]:
    return {
        "id": entry.id,
        "start_date": entry.start_date.isoformat(),
        "end_date": entry.end_date.isoformat(),
        "reason": entry.reason,
        "capacity_percent": entry.capacity_percent,
    }


def serialize_report(report) -> dict[str, object]:
    return {
        "simulation_token": report.simulation_token,
        "exam_date": format_iso_date(report.exam_date),
        "target_finish_date": report.target_finish_date.isoformat(),
        "as_of_date": report.as_of_date.isoformat(),
        "days_available": report.available_day_count,
        "days_unavailable": report.unavailable_day_count,
        "load_capacity_unit": "minutes",
        "remaining_total_units": report.total_remaining_units,
        "remaining_fo_units": report.remaining_units_by_track.get("FO", 0),
        "remaining_un_units": report.remaining_units_by_track.get("UN", 0),
        "unallocated_lessons_by_track": report.unallocated_lesson_count_by_track,
        "duration_diagnostics": report.duration_diagnostics,
        "total_capacity_units": report.total_capacity_units,
        "capacity_deficit_units": report.capacity_deficit_units,
        "overflow_days": report.overflow_days,
        "daily_goal_units": round(report.required_average_units, 2),
        "pending_lessons": report.pending_lesson_count,
        "cut_review_free": report.cut_summary["review_free"],
        "cut_english_preserved": report.cut_summary["english"],
        "cut_manual": report.cut_summary["manual"],
        "first_14_days": report.first_days,
        "last_14_days": report.last_days,
        "weekly_distribution": report.weekly_distribution,
        "reprogrammed_lessons": report.assignment_count,
        "backup_path": str(report.backup_path) if report.backup_path else None,
        "feasible": report.feasible,
        "fo_plan": report.fo_plan_summary,
        "lesson_order_diagnostics": report.lesson_order_diagnostics,
        "distribution_diagnostics": report.distribution_diagnostics,
        "validation_errors": report.validation_errors,
    }


def month_payload(reference_date: date) -> dict[str, int]:
    return {
        "year": reference_date.year,
        "month": reference_date.month,
        "days_in_month": monthrange(reference_date.year, reference_date.month)[1],
    }


def default_review_question_form_state() -> dict[str, object]:
    return {
        "statement": "",
        "subject": REVIEW_QUESTION_SUBJECTS[0],
        "error_reason": "",
        "tags": "",
        "difficulty": "Média",
        "is_objective": True,
        "correct_option": "",
        "explanation": "",
    }


def build_review_question_form_state(form_data: object | None = None) -> dict[str, object]:
    if form_data is None:
        return default_review_question_form_state()

    defaults = default_review_question_form_state()
    return {
        "statement": str(getattr(form_data, "get", lambda *_: "")("statement", defaults["statement"]) or "").strip(),
        "subject": str(getattr(form_data, "get", lambda *_: "")("subject", defaults["subject"]) or defaults["subject"]).strip(),
        "error_reason": str(getattr(form_data, "get", lambda *_: "")("error_reason", defaults["error_reason"]) or "").strip(),
        "tags": str(getattr(form_data, "get", lambda *_: "")("tags", defaults["tags"]) or "").strip(),
        "difficulty": str(getattr(form_data, "get", lambda *_: "")("difficulty", defaults["difficulty"]) or defaults["difficulty"]).strip(),
        "is_objective": parse_bool(getattr(form_data, "get", lambda *_: None)("is_objective"), False),
        "correct_option": str(getattr(form_data, "get", lambda *_: "")("correct_option", defaults["correct_option"]) or "").strip(),
        "explanation": str(getattr(form_data, "get", lambda *_: "")("explanation", defaults["explanation"]) or "").strip(),
    }


def review_question_form_state_from_question(question: dict[str, object]) -> dict[str, object]:
    return {
        "statement": str(question.get("statement") or ""),
        "subject": str(question.get("subject") or REVIEW_QUESTION_SUBJECTS[0]),
        "error_reason": str(question.get("error_reason") or ""),
        "tags": ", ".join(question.get("tags") or []),
        "difficulty": str(question.get("difficulty") or "Média"),
        "is_objective": bool(question.get("is_objective")),
        "correct_option": str(question.get("correct_option") or ""),
        "explanation": str(question.get("explanation") or ""),
    }


def review_question_form_options() -> dict[str, object]:
    return {
        "subjects": REVIEW_QUESTION_SUBJECTS,
        "error_reasons": REVIEW_QUESTION_ERROR_REASONS,
        "difficulties": REVIEW_QUESTION_DIFFICULTIES,
        "objective_options": REVIEW_QUESTION_OBJECTIVE_OPTIONS,
        "accepted_extensions": ", ".join(sorted(ext.lstrip(".") for ext in REVIEW_QUESTION_ALLOWED_IMAGE_EXTENSIONS)),
        "max_image_mb": REVIEW_QUESTION_MAX_IMAGE_BYTES // (1024 * 1024),
        "accepted_mime_types": ",".join(sorted(REVIEW_QUESTION_ALLOWED_IMAGE_MIME_TYPES)),
    }


def read_review_question_upload(file_storage) -> ReviewQuestionUploadInput | None:
    if file_storage is None:
        return None
    filename = str(getattr(file_storage, "filename", "") or "").strip()
    if not filename:
        return None
    data = file_storage.read()
    return ReviewQuestionUploadInput(
        filename=filename,
        content_type=str(getattr(file_storage, "mimetype", "") or getattr(file_storage, "content_type", "") or ""),
        data=data,
    )


def read_review_question_uploads(file_storages: object | None) -> list[ReviewQuestionUploadInput]:
    uploads: list[ReviewQuestionUploadInput] = []
    for file_storage in file_storages or []:
        upload = read_review_question_upload(file_storage)
        if upload is not None:
            uploads.append(upload)
    return uploads


def parse_review_question_deferred_ids(value: str | None) -> list[int]:
    if not value:
        return []
    parsed_ids: list[int] = []
    for chunk in str(value).split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            candidate = int(token)
        except ValueError:
            continue
        if candidate > 0 and candidate not in parsed_ids:
            parsed_ids.append(candidate)
    return parsed_ids


def serialize_review_question_deferred_ids(values: list[int]) -> str:
    return ",".join(str(value) for value in values if value > 0)


def review_question_image_url(relative_path: str | None) -> str | None:
    if not relative_path:
        return None
    return url_for("review_question_asset", relative_path=relative_path)


def build_review_feedback(question: dict[str, object], answer_action: str) -> dict[str, object]:
    if bool(question["is_objective"]):
        selected_option = answer_action
        if selected_option == "dont_know":
            return {
                "result": "dont_know",
                "selected_option": None,
                "message": f"Resposta correta: {question['correct_option']}.",
            }
        if selected_option == question["correct_option"]:
            return {
                "result": "correct",
                "selected_option": selected_option,
                "message": "Correto.",
            }
        return {
            "result": "wrong",
            "selected_option": selected_option,
            "message": f"Errado. Resposta correta: {question['correct_option']}.",
        }

    mapping = {
        "self_correct": ("self_correct", "Correto."),
        "self_wrong": ("self_wrong", "Errado."),
        "dont_know": ("dont_know", "Resposta em aberto. Reveja a resolução antes de avançar."),
    }
    result, message = mapping[answer_action]
    return {
        "result": result,
        "selected_option": None,
        "message": message,
    }


def enrich_review_question_for_template(question: dict[str, object]) -> dict[str, object]:
    question_image_paths = [str(path) for path in question.get("question_image_paths") or []]
    return {
        **question,
        "display_title": get_review_question_display_title(question),
        "question_image_url": review_question_image_url(str(question["question_image_path"]) if question.get("question_image_path") else None),
        "question_image_urls": [
            review_question_image_url(path)
            for path in question_image_paths
            if review_question_image_url(path)
        ],
        "question_images": [
            {
                "path": path,
                "url": review_question_image_url(path),
                "filename": Path(path).name,
            }
            for path in question_image_paths
        ],
        "answer_image_url": review_question_image_url(str(question["answer_image_path"]) if question.get("answer_image_path") else None),
        "explanation_html": render_review_question_explanation_html(str(question.get("explanation") or "")),
    }


def review_question_status_meta(question: dict[str, object], *, as_of_date: date | str | None = None) -> dict[str, str]:
    status = review_question_status(question, as_of_date=as_of_date)
    mapping = {
        "suspended": {"label": "Suspensa", "class_name": "status-muted"},
        "overdue": {"label": "Atrasada", "class_name": "warn"},
        "today": {"label": "Hoje", "class_name": "badge badge--pending"},
        "future": {"label": "Futura", "class_name": "ok"},
        "no_date": {"label": "Sem data", "class_name": "status-muted"},
    }
    return {"code": status, **mapping[status]}


def create_app(database_path: str | Path | None = None) -> Flask:
    app = Flask(__name__)

    configured_path = os.environ.get("CRONOGRAMA_DB_PATH")
    db_path = (
        Path(database_path)
        if database_path is not None
        else Path(configured_path) if configured_path else DEFAULT_DB_PATH
    )
    validate_runtime_database(db_path)

    app.jinja_env.filters["br_date"] = format_br_date
    app.jinja_env.filters["br_datetime"] = format_br_datetime
    app.jinja_env.filters["duration"] = format_duration

    @app.get("/")
    def dashboard():
        requested_date = request.args.get("date")
        selected_date = parse_iso_date(requested_date).isoformat()
        fo_view = normalize_fo_view(request.args.get("fo_view"))
        dashboard_extra_query_items = [
            (key, value)
            for key, value in request.args.items(multi=True)
            if key not in {"date", "fo_view"}
        ]

        def dashboard_url(*, date_value: str | None = None, fo_view_value: str | None = None) -> str:
            query_items = [
                ("date", date_value or selected_date),
                ("fo_view", normalize_fo_view(fo_view_value, fo_view)),
                *dashboard_extra_query_items,
            ]
            return f"{url_for('dashboard')}?{urlencode(query_items)}"

        with connect_db(db_path) as conn:
            sync_fo_exercise_tasks(conn)
            conn.commit()
            fo_rows, assigned_codes = build_today_rows(conn=conn, target_date=selected_date)
            fo_exercise_section = build_fo_exercise_section(conn=conn, target_date=selected_date)
            fo_total_duration_seconds = sum(
                int(row["target"].get("duration_seconds") or 0)
                for row in fo_rows
                if row["target"].get("lesson_type") == "lesson"
            )
            un_section = build_universe_narrado_section(conn=conn, target_date=selected_date)
            un_rows = un_section["rows"]
            displayed_codes = set(assigned_codes)
            displayed_codes.update(
                lesson["lesson_code"]
                for row in un_rows
                for lesson in row.get("pending_lessons", [])
            )
            overdue_recommendations = build_overdue_recommendations(
                conn=conn,
                target_date=selected_date,
                exclude_lesson_codes=displayed_codes,
                limit=3,
            )
            stats = build_stats(conn=conn, target_date=selected_date)
            dashboard_progress = build_dashboard_progress(conn=conn, target_date=selected_date)
            schedule_summary = build_schedule_summary(conn=conn, target_date=selected_date)
            review_question_stats = count_review_question_dashboard_stats(conn=conn, as_of_date=selected_date)
            has_data = conn.execute("SELECT 1 FROM lessons LIMIT 1").fetchone() is not None

        return render_template(
            "dashboard.html",
            selected_date=selected_date,
            fo_view=fo_view,
            fo_rows=fo_rows,
            fo_exercise_section=fo_exercise_section,
            fo_total_duration_seconds=fo_total_duration_seconds,
            un_rows=un_rows,
            un_section=un_section,
            overdue_recommendations=overdue_recommendations,
            stats=stats,
            dashboard_progress=dashboard_progress,
            schedule_summary=schedule_summary,
            review_question_stats=review_question_stats,
            has_data=has_data,
            lesson_label=lesson_label,
            dashboard_url=dashboard_url,
            dashboard_extra_query_items=dashboard_extra_query_items,
        )

    @app.get("/database")
    def database_view():
        status_filter = request.args.get("status", "all")
        track_filter = request.args.get("track", "all")
        subject_filter = request.args.get("subject", "all")
        front_filter = request.args.get("front", "all")
        search = request.args.get("q", "")
        page = parse_positive_int(request.args.get("page"), 1)

        with connect_db(db_path) as conn:
            filter_options = fetch_database_filter_options(conn=conn)

            if track_filter not in {"FO", "UN"}:
                track_filter = "all"
                subject_filter = "all"
                front_filter = "all"
            else:
                available_subjects = filter_options["subjects"].get(track_filter, [])
                if subject_filter not in available_subjects:
                    subject_filter = "all"

                available_fronts = filter_options["fronts"] if track_filter == "FO" else []
                if front_filter not in available_fronts:
                    front_filter = "all"

            total_rows = count_database_rows(
                conn=conn,
                status_filter=status_filter,
                track_filter=track_filter,
                subject_filter=subject_filter,
                front_filter=front_filter,
                search=search,
            )
            total_pages = max(ceil(total_rows / DATABASE_PAGE_SIZE), 1)
            page = min(page, total_pages)
            offset = (page - 1) * DATABASE_PAGE_SIZE

            rows = fetch_database_rows(
                conn=conn,
                status_filter=status_filter,
                track_filter=track_filter,
                subject_filter=subject_filter,
                front_filter=front_filter,
                search=search,
                limit=DATABASE_PAGE_SIZE,
                offset=offset,
            )
            stats = build_stats(conn=conn, target_date=parse_iso_date(None).isoformat())

        def database_page_url(target_page: int) -> str:
            return url_for(
                "database_view",
                status=status_filter,
                track=track_filter,
                subject=subject_filter,
                front=front_filter,
                q=search,
                page=target_page,
            )

        page_start = offset + 1 if total_rows else 0
        page_end = offset + len(rows) if total_rows else 0
        pagination = {
            "page": page,
            "page_size": DATABASE_PAGE_SIZE,
            "total_rows": total_rows,
            "total_pages": total_pages,
            "page_start": page_start,
            "page_end": page_end,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_url": database_page_url(page - 1) if page > 1 else None,
            "next_url": database_page_url(page + 1) if page < total_pages else None,
        }

        return render_template(
            "database.html",
            rows=rows,
            pagination=pagination,
            status_filter=status_filter,
            track_filter=track_filter,
            subject_filter=subject_filter,
            front_filter=front_filter,
            search=search,
            stats=stats,
            filter_options=filter_options,
            lesson_label=lesson_label,
        )

    @app.get("/backup/download")
    def backup_download():
        snapshot_path = create_backup_archive(db_path=db_path)
        download_name = f"cronograma-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"

        @after_this_request
        def cleanup_snapshot(response):
            try:
                snapshot_path.unlink(missing_ok=True)
            except OSError:
                pass
            return response

        return send_file(
            snapshot_path,
            as_attachment=True,
            download_name=download_name,
            mimetype="application/zip",
        )

    @app.get("/review-questions")
    def review_questions_home():
        review_date = current_local_date().isoformat()
        created_id = parse_non_negative_int(request.args.get("created"), 0)
        updated_id = parse_non_negative_int(request.args.get("updated"), 0)
        deleted_flag = request.args.get("deleted") == "1"
        status_filter = request.args.get("status", "all")
        subject_filter = request.args.get("subject", "all")
        difficulty_filter = request.args.get("difficulty", "all")
        error_reason_filter = request.args.get("reason", "all")
        tag_filter = str(request.args.get("tag", "") or "").strip()
        search = str(request.args.get("q", "") or "").strip()
        page = parse_positive_int(request.args.get("page"), 1)
        if status_filter not in {"all", "pending_today", "overdue", "future", "suspended"}:
            status_filter = "all"
        if subject_filter not in {"all", *REVIEW_QUESTION_SUBJECTS}:
            subject_filter = "all"
        if difficulty_filter not in {"all", *REVIEW_QUESTION_DIFFICULTIES}:
            difficulty_filter = "all"
        if error_reason_filter not in {"all", *REVIEW_QUESTION_ERROR_REASONS}:
            error_reason_filter = "all"

        created_question = None
        updated_question = None
        with connect_db(db_path) as conn:
            pending_count = count_pending_review_questions(conn=conn, as_of_date=review_date)
            total_rows = count_review_questions_by_filter(
                conn=conn,
                as_of_date=review_date,
                status_filter=status_filter,
                subject_filter=subject_filter,
                difficulty_filter=difficulty_filter,
                error_reason_filter=error_reason_filter,
                tag_filter=tag_filter,
                search=search,
            )
            total_pages = max(ceil(total_rows / REVIEW_QUESTIONS_PAGE_SIZE), 1)
            page = min(page, total_pages)
            offset = (page - 1) * REVIEW_QUESTIONS_PAGE_SIZE
            questions = fetch_review_questions_by_filter(
                conn=conn,
                as_of_date=review_date,
                status_filter=status_filter,
                subject_filter=subject_filter,
                difficulty_filter=difficulty_filter,
                error_reason_filter=error_reason_filter,
                tag_filter=tag_filter,
                search=search,
                limit=REVIEW_QUESTIONS_PAGE_SIZE,
                offset=offset,
            )
            if created_id > 0:
                try:
                    created_question = get_review_question(conn, created_id)
                except ValueError:
                    created_question = None
            if updated_id > 0:
                try:
                    updated_question = get_review_question(conn, updated_id)
                except ValueError:
                    updated_question = None

        question_rows = [
            {
                **enrich_review_question_for_template(question),
                "status_meta": review_question_status_meta(question, as_of_date=review_date),
            }
            for question in questions
        ]
        created_question = enrich_review_question_for_template(created_question) if created_question else None
        updated_question = enrich_review_question_for_template(updated_question) if updated_question else None

        def review_questions_page_url(target_page: int) -> str:
            return url_for(
                "review_questions_home",
                status=status_filter,
                subject=subject_filter,
                difficulty=difficulty_filter,
                reason=error_reason_filter,
                tag=tag_filter,
                q=search,
                page=target_page,
            )

        current_listing_url = review_questions_page_url(page)
        page_start = offset + 1 if total_rows else 0
        page_end = offset + len(question_rows) if total_rows else 0
        pagination = {
            "page": page,
            "page_size": REVIEW_QUESTIONS_PAGE_SIZE,
            "total_rows": total_rows,
            "total_pages": total_pages,
            "page_start": page_start,
            "page_end": page_end,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_url": review_questions_page_url(page - 1) if page > 1 else None,
            "next_url": review_questions_page_url(page + 1) if page < total_pages else None,
        }

        return render_template(
            "review_questions.html",
            pending_count=pending_count,
            created_question=created_question,
            updated_question=updated_question,
            deleted_flag=deleted_flag,
            questions=question_rows,
            pagination=pagination,
            current_listing_url=current_listing_url,
            status_filter=status_filter,
            subject_filter=subject_filter,
            difficulty_filter=difficulty_filter,
            error_reason_filter=error_reason_filter,
            tag_filter=tag_filter,
            search=search,
            filter_options=review_question_form_options(),
        )

    @app.route("/review-questions/new", methods=["GET", "POST"])
    def review_questions_new():
        form_state = build_review_question_form_state(request.form if request.method == "POST" else None)
        error_message: str | None = None
        tag_suggestions: list[str] = []

        if request.method == "POST":
            try:
                question_image_uploads = read_review_question_uploads(request.files.getlist("question_images"))
                if not question_image_uploads:
                    legacy_upload = read_review_question_upload(request.files.get("question_image"))
                    if legacy_upload is not None:
                        question_image_uploads = [legacy_upload]
                answer_image_upload = read_review_question_upload(request.files.get("answer_image"))
                payload = ReviewQuestionCreateInput(
                    statement=str(form_state["statement"]) or None,
                    subject=str(form_state["subject"]),
                    error_reason=str(form_state["error_reason"]) or None,
                    tags=str(form_state["tags"]) or None,
                    difficulty=str(form_state["difficulty"]),
                    is_objective=bool(form_state["is_objective"]),
                    correct_option=str(form_state["correct_option"]) or None,
                    explanation=str(form_state["explanation"]) or None,
                )
                with connect_db(db_path) as conn:
                    question = create_review_question_with_uploads(
                        conn=conn,
                        payload=payload,
                        question_image_uploads=question_image_uploads,
                        answer_image_upload=answer_image_upload,
                        reference_date=current_local_date(),
                    )
                    conn.commit()
                return redirect(url_for("review_questions_home", created=question["id"]))
            except ValueError as exc:
                error_message = str(exc)

        with connect_db(db_path) as conn:
            tag_suggestions = list_existing_review_question_tags(conn)

        return render_template(
            "review_question_form.html",
            page_title="Nova Questão - Cronograma FO",
            hero_kicker="Cadastro",
            hero_title="Nova questão",
            hero_subtitle="Texto da questão é opcional se você anexar um print. O agendamento inicial sai automaticamente da dificuldade escolhida.",
            form_title="Cadastrar questão",
            submit_label="Salvar questão",
            back_url=url_for("review_questions_home"),
            form_action=url_for("review_questions_new"),
            form_state=form_state,
            error_message=error_message,
            form_options=review_question_form_options(),
            tag_suggestions=tag_suggestions,
            removed_question_image_paths=[],
            existing_question=None,
        ), (400 if error_message else 200)

    @app.route("/review-questions/<int:question_id>/edit", methods=["GET", "POST"])
    def review_questions_edit(question_id: int):
        return_to = safe_return_to(request.values.get("return_to"), "")
        error_message: str | None = None
        tag_suggestions: list[str] = []
        removed_question_image_paths = request.form.getlist("remove_question_images") if request.method == "POST" else []
        with connect_db(db_path) as conn:
            try:
                existing_question = get_review_question(conn, question_id)
            except ValueError:
                return json_error("Questão não encontrada.", 404)
            tag_suggestions = list_existing_review_question_tags(conn)

        form_state = (
            build_review_question_form_state(request.form)
            if request.method == "POST"
            else review_question_form_state_from_question(existing_question)
        )

        if request.method == "POST":
            try:
                question_image_uploads = read_review_question_uploads(request.files.getlist("question_images"))
                if not question_image_uploads:
                    legacy_upload = read_review_question_upload(request.files.get("question_image"))
                    if legacy_upload is not None:
                        question_image_uploads = [legacy_upload]
                answer_image_upload = read_review_question_upload(request.files.get("answer_image"))
                payload = ReviewQuestionUpdateInput(
                    statement=str(form_state["statement"]) or None,
                    subject=str(form_state["subject"]),
                    error_reason=str(form_state["error_reason"]) or None,
                    tags=str(form_state["tags"]) or None,
                    difficulty=str(form_state["difficulty"]),
                    is_objective=bool(form_state["is_objective"]),
                    correct_option=str(form_state["correct_option"]) or None,
                    explanation=str(form_state["explanation"]) or None,
                )
                with connect_db(db_path) as conn:
                    updated_question = update_review_question_with_uploads(
                        conn=conn,
                        question_id=question_id,
                        payload=payload,
                        question_image_uploads=question_image_uploads,
                        removed_question_image_paths=removed_question_image_paths,
                        answer_image_upload=answer_image_upload,
                    )
                    conn.commit()
                if return_to:
                    return redirect(return_to)
                return redirect(url_for("review_questions_home", updated=updated_question["id"]))
            except ValueError as exc:
                error_message = str(exc)

        back_url = return_to or url_for("review_questions_home")
        form_action = (
            url_for("review_questions_edit", question_id=question_id, return_to=return_to)
            if return_to
            else url_for("review_questions_edit", question_id=question_id)
        )
        return render_template(
            "review_question_form.html",
            page_title="Editar Questão - Cronograma FO",
            hero_kicker="Edição",
            hero_title="Editar questão",
            hero_subtitle="Você pode substituir imagens, ajustar a classificação e manter o histórico da questão.",
            form_title=f"Editar questão #{question_id}",
            submit_label="Salvar alterações",
            back_url=back_url,
            form_action=form_action,
            form_state=form_state,
            error_message=error_message,
            form_options=review_question_form_options(),
            tag_suggestions=tag_suggestions,
            removed_question_image_paths=removed_question_image_paths,
            existing_question=enrich_review_question_for_template(existing_question),
            return_to=return_to,
        ), (400 if error_message else 200)

    @app.get("/review-questions/<int:question_id>/preview")
    def review_questions_preview(question_id: int):
        return_to = safe_return_to(request.args.get("return_to"), url_for("review_questions_home"))
        with connect_db(db_path) as conn:
            try:
                question = get_review_question(conn, question_id)
            except ValueError:
                return json_error("Questão não encontrada.", 404)
            pending_count = count_pending_review_questions(conn=conn, as_of_date=current_local_date())

        return render_template(
            "review_question_preview.html",
            pending_count=pending_count,
            question=enrich_review_question_for_template(question),
            back_url=return_to,
        )

    @app.post("/review-questions/<int:question_id>/suspend")
    def review_questions_suspend(question_id: int):
        return_to = safe_return_to(request.form.get("return_to"), url_for("review_questions_home"))
        with connect_db(db_path) as conn:
            try:
                suspend_review_question_for_review(conn=conn, question_id=question_id)
            except ValueError:
                return json_error("Questão não encontrada.", 404)
            conn.commit()
        return redirect(return_to)

    @app.post("/review-questions/<int:question_id>/reactivate")
    def review_questions_reactivate(question_id: int):
        return_to = safe_return_to(request.form.get("return_to"), url_for("review_questions_home"))
        with connect_db(db_path) as conn:
            try:
                reactivate_review_question_for_listing(
                    conn=conn,
                    question_id=question_id,
                    reference_date=current_local_date(),
                )
            except ValueError:
                return json_error("Questão não encontrada.", 404)
            conn.commit()
        return redirect(return_to)

    @app.post("/review-questions/<int:question_id>/delete")
    def review_questions_delete(question_id: int):
        return_to = safe_return_to(request.form.get("return_to"), url_for("review_questions_home"))
        with connect_db(db_path) as conn:
            try:
                delete_review_question(conn=conn, question_id=question_id)
            except ValueError:
                return json_error("Questão não encontrada.", 404)
            conn.commit()

        if return_to == url_for("review_questions_home"):
            return redirect(url_for("review_questions_home", deleted=1))
        separator = "&" if "?" in return_to else "?"
        return redirect(f"{return_to}{separator}deleted=1")

    @app.get("/review-questions/assets/<path:relative_path>")
    def review_question_asset(relative_path: str):
        try:
            asset_path = resolve_review_question_upload_path(relative_path)
        except ValueError:
            return json_error("Arquivo de imagem inválido.", 404)
        if not asset_path.exists() or not asset_path.is_file():
            return json_error("Imagem não encontrada.", 404)
        return send_file(asset_path)

    @app.route("/review-questions/review", methods=["GET", "POST"])
    def review_questions_review():
        forced_question_id = parse_non_negative_int(
            request.form.get("forced_question_id") if request.method == "POST" else request.args.get("question_id"),
            0,
        )
        deferred_ids = parse_review_question_deferred_ids(
            request.values.get("deferred_ids") if request.method == "POST" else request.args.get("deferred")
        )
        feedback: dict[str, object] | None = None
        current_question: dict[str, object] | None = None
        pending_count = 0
        error_message: str | None = None

        with connect_db(db_path) as conn:
            review_date = current_local_date()
            if request.method == "POST":
                action = str(request.form.get("action") or "").strip()
                question_id = parse_non_negative_int(request.form.get("question_id"), 0)
                if question_id <= 0:
                    return redirect(url_for("review_questions_review", deferred=serialize_review_question_deferred_ids(deferred_ids)))
                try:
                    current_question = get_review_question(conn, question_id)
                except ValueError:
                    return redirect(url_for("review_questions_review", deferred=serialize_review_question_deferred_ids(deferred_ids)))
                if bool(current_question["is_suspended"]):
                    error_message = "Questão suspensa. Reative no banco para revisar."
                    current_question = None
                elif action == "defer":
                    next_deferred = [*deferred_ids]
                    if question_id not in next_deferred:
                        next_deferred.append(question_id)
                    redirect_kwargs = {"deferred": serialize_review_question_deferred_ids(next_deferred)}
                    if forced_question_id and forced_question_id != question_id:
                        redirect_kwargs["question_id"] = forced_question_id
                    return redirect(url_for("review_questions_review", **redirect_kwargs))

                if current_question is not None and action == "postpone":
                    postpone_review_question_to_tomorrow(conn=conn, question_id=question_id)
                    conn.commit()
                    return redirect(url_for("review_questions_review", deferred=serialize_review_question_deferred_ids(deferred_ids)))

                if current_question is not None and action == "suspend":
                    suspend_review_question_for_review(conn=conn, question_id=question_id)
                    conn.commit()
                    return redirect(url_for("review_questions_review", deferred=serialize_review_question_deferred_ids(deferred_ids)))

                if current_question is not None and action == "answer":
                    answer_action = str(request.form.get("answer_action") or "").strip()
                    valid_actions = {"dont_know"}
                    if bool(current_question["is_objective"]):
                        valid_actions.update(REVIEW_QUESTION_OBJECTIVE_OPTIONS)
                    else:
                        valid_actions.update({"self_correct", "self_wrong"})
                    if answer_action not in valid_actions:
                        error_message = "Resposta inválida."
                    else:
                        feedback = build_review_feedback(current_question, answer_action)

                elif current_question is not None and action == "finalize":
                    result = str(request.form.get("result") or "").strip()
                    selected_option = str(request.form.get("selected_option") or "").strip() or None
                    difficulty_after = str(request.form.get("difficulty_after") or "").strip()
                    feedback = {
                        "result": result,
                        "selected_option": selected_option,
                        "message": str(request.form.get("feedback_message") or "").strip(),
                    }
                    try:
                        register_review_question_attempt(
                            conn=conn,
                            payload=ReviewQuestionAttemptInput(
                                question_id=question_id,
                                result=result,
                                selected_option=selected_option,
                                difficulty_after=difficulty_after,
                            ),
                        )
                        conn.commit()
                    except ValueError as exc:
                        error_message = str(exc)
                    else:
                        return redirect(url_for("review_questions_review", deferred=serialize_review_question_deferred_ids(deferred_ids)))

            queue = list_review_questions_due_for_review(conn=conn, as_of_date=review_date, excluded_ids=deferred_ids)
            if not queue and deferred_ids:
                deferred_ids = []
                queue = list_review_questions_due_for_review(conn=conn, as_of_date=review_date, excluded_ids=deferred_ids)
            pending_count = count_pending_review_questions(conn=conn, as_of_date=review_date)

            if current_question is None:
                if forced_question_id > 0:
                    try:
                        forced_question = get_review_question(conn, forced_question_id)
                    except ValueError:
                        error_message = error_message or "Questão não encontrada."
                    else:
                        if bool(forced_question["is_suspended"]):
                            error_message = error_message or "Questão suspensa. Reative no banco para revisar."
                        else:
                            current_question = forced_question
                if current_question is None and forced_question_id <= 0:
                    current_question = queue[0] if queue else None

        return render_template(
            "review_question_review.html",
            pending_count=pending_count,
            current_question=enrich_review_question_for_template(current_question) if current_question else None,
            feedback=feedback,
            error_message=error_message,
            deferred_ids=serialize_review_question_deferred_ids(deferred_ids),
            review_difficulties=REVIEW_QUESTION_DIFFICULTIES,
            objective_options=REVIEW_QUESTION_OBJECTIVE_OPTIONS,
            forced_question_id=forced_question_id,
        ), (400 if error_message else 200)

    @app.get("/reprogramming")
    def reprogramming_view():
        today = current_local_date()
        return render_template(
            "reprogramming.html",
            today_iso=today.isoformat(),
            today_br=today.strftime("%d/%m/%Y"),
            initial_month=month_payload(today),
        )

    @app.get("/api/reprogramming/settings")
    def reprogramming_settings_api():
        with connect_db(db_path) as conn:
            settings = get_schedule_settings(conn)
            payload = serialize_settings(settings)
            payload["config_token"] = current_simulation_token(conn, settings=settings)
        return jsonify({"ok": True, "settings": payload})

    @app.post("/api/reprogramming/settings")
    def reprogramming_settings_update_api():
        payload = request.get_json(silent=True) or {}
        try:
            with connect_db(db_path) as conn:
                current_settings = get_schedule_settings(conn)
                settings = merge_settings(
                    current_settings,
                    exam_date=parse_optional_iso_date(payload.get("exam_date")),
                    target_finish_date=parse_optional_iso_date(payload.get("target_finish_date")),
                    include_weekends=parse_bool(payload.get("include_weekends"), current_settings.include_weekends),
                )
                save_schedule_settings(conn, settings)
                conn.commit()
                response_payload = serialize_settings(settings)
                response_payload["config_token"] = current_simulation_token(conn, settings=settings)
        except ValueError as exc:
            return json_error(str(exc), 400)

        return jsonify({"ok": True, "settings": response_payload})

    @app.get("/api/reprogramming/unavailability")
    def reprogramming_unavailability_list_api():
        with connect_db(db_path) as conn:
            rows = list_unavailability(conn)
        return jsonify({"ok": True, "items": [serialize_unavailability_entry(item) for item in rows]})

    @app.post("/api/reprogramming/unavailability")
    def reprogramming_unavailability_create_api():
        payload = request.get_json(silent=True) or {}
        try:
            date_value = parse_required_iso_date(payload.get("date"), "date")
            reason = (payload.get("reason") or "").strip() or None
            with connect_db(db_path) as conn:
                entry_id = add_unavailability(
                    conn=conn,
                    start_date_value=date_value,
                    end_date_value=date_value,
                    capacity_percent=0,
                    reason=reason,
                )
                conn.commit()
                item = next((row for row in list_unavailability(conn) if row.id == entry_id), None)
        except ValueError as exc:
            return json_error(str(exc), 400)
        if item is None:
            return json_error("Não foi possível carregar a indisponibilidade criada.", 500)

        return jsonify({"ok": True, "item": serialize_unavailability_entry(item)})

    @app.delete("/api/reprogramming/unavailability/<int:entry_id>")
    def reprogramming_unavailability_delete_api(entry_id: int):
        with connect_db(db_path) as conn:
            removed = remove_unavailability(conn, entry_id)
            conn.commit()
        if removed == 0:
            return json_error("Indisponibilidade não encontrada.", 404)
        return jsonify({"ok": True, "removed_id": entry_id})

    @app.post("/api/reprogramming/dry-run")
    def reprogramming_dry_run_api():
        with connect_db(db_path) as conn:
            settings = get_schedule_settings(conn)
            try:
                report = build_reprogram_report(conn=conn, settings=settings)
            except ValueError as exc:
                return json_error(str(exc), 400)
        return jsonify({"ok": True, "report": serialize_report(report)})

    @app.post("/api/reprogramming/apply")
    def reprogramming_apply_api():
        payload = request.get_json(silent=True) or {}
        simulation_token = str(payload.get("simulation_token") or "").strip()
        if not simulation_token:
            return json_error("Simulação inválida ou ausente. Rode uma nova simulação antes de aplicar.", 409)

        with connect_db(db_path) as conn:
            settings = get_schedule_settings(conn)
            current_token = current_simulation_token(conn, settings=settings)
            if current_token != simulation_token:
                return json_error("A configuração mudou após a simulação. Rode uma nova simulação antes de aplicar.", 409)

            try:
                report = apply_reprogramming(conn=conn, settings=settings, db_path=db_path)
                conn.commit()
            except ValueError as exc:
                return json_error(str(exc), 400)

        return jsonify({"ok": True, "report": serialize_report(report)})

    @app.post("/lessons/<lesson_code>/toggle-seen")
    def toggle_seen(lesson_code: str):
        next_date = request.form.get("next_date")
        selected_date = parse_iso_date(next_date).isoformat()
        return_to = request.form.get("return_to")
        fallback = url_for(
            "dashboard",
            date=selected_date,
            fo_view=normalize_fo_view(request.form.get("fo_view")),
        )

        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        mark_date = parse_iso_date(None)
        with connect_db(db_path) as conn:
            lesson = conn.execute(
                "SELECT lesson_type, track_code, is_seen FROM lessons WHERE lesson_code = ?",
                (lesson_code,),
            ).fetchone()

            if lesson:
                if lesson["lesson_type"] == "pending":
                    return redirect(safe_return_to(return_to, fallback))

                if lesson["is_seen"] == 1:
                    conn.execute(
                        "UPDATE lessons SET is_seen = 0, seen_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE lesson_code = ?",
                        (lesson_code,),
                    )
                    if lesson["track_code"] == "FO" and lesson["lesson_type"] == "lesson":
                        deactivate_exercise_for_lesson(conn=conn, lesson_code=lesson_code)
                else:
                    conn.execute(
                        "UPDATE lessons SET is_seen = 1, seen_at = ?, updated_at = CURRENT_TIMESTAMP WHERE lesson_code = ?",
                        (now, lesson_code),
                    )
                    if lesson["track_code"] == "FO" and lesson["lesson_type"] == "lesson":
                        activate_exercise_for_lesson(
                            conn=conn,
                            lesson_code=lesson_code,
                            reference_date=mark_date,
                        )
                conn.commit()

        return redirect(safe_return_to(return_to, fallback))

    @app.post("/un/modules/mark-seen")
    def mark_un_module_seen():
        next_date = request.form.get("next_date")
        selected_date = parse_iso_date(next_date).isoformat()
        return_to = request.form.get("return_to")
        fallback = url_for(
            "dashboard",
            date=selected_date,
            fo_view=normalize_fo_view(request.form.get("fo_view")),
        )
        module_path = (request.form.get("module_path") or "").strip()
        if not module_path:
            return redirect(safe_return_to(return_to, fallback))

        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        with connect_db(db_path) as conn:
            rows = conn.execute(
                """
                SELECT lesson_code, relative_path
                FROM lessons
                WHERE track_code = 'UN'
                  AND lesson_type IN ('lesson', 'list')
                  AND is_seen = 0
                  AND is_cut = 0
                """
            ).fetchall()
            lesson_codes = [
                row["lesson_code"]
                for row in rows
                if derive_un_module_path(row["relative_path"]) == module_path
            ]
            if lesson_codes:
                conn.executemany(
                    """
                    UPDATE lessons
                    SET is_seen = 1,
                        seen_at = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE lesson_code = ?
                    """,
                    [(now, lesson_code) for lesson_code in lesson_codes],
                )
                conn.commit()

        return redirect(safe_return_to(return_to, fallback))

    @app.post("/fo/exercises/settings")
    def update_fo_exercise_settings():
        selected_date = parse_iso_date(request.form.get("next_date")).isoformat()
        return_to = request.form.get("return_to")
        fallback = url_for(
            "dashboard",
            date=selected_date,
            fo_view=normalize_fo_view(request.form.get("fo_view"), "exercicios"),
        )
        offset_days = parse_non_negative_int(request.form.get("offset_days"), 3)

        with connect_db(db_path) as conn:
            set_exercise_offset_days(conn=conn, offset_days=offset_days)
            reschedule_unmoved_exercise_tasks(conn=conn)
            conn.commit()

        return redirect(safe_return_to(return_to, fallback))

    @app.post("/exercise-tasks/<int:task_id>/status")
    def set_exercise_status(task_id: int):
        selected_date = parse_iso_date(request.form.get("next_date")).isoformat()
        return_to = request.form.get("return_to")
        fallback = url_for(
            "dashboard",
            date=selected_date,
            fo_view=normalize_fo_view(request.form.get("fo_view"), "exercicios"),
        )
        status = request.form.get("status", "pending")

        with connect_db(db_path) as conn:
            update_exercise_status(conn=conn, task_id=task_id, status=status)
            conn.commit()

        return redirect(safe_return_to(return_to, fallback))

    @app.post("/exercise-tasks/<int:task_id>/reschedule")
    def reschedule_exercise(task_id: int):
        selected_date = parse_iso_date(request.form.get("next_date")).isoformat()
        return_to = request.form.get("return_to")
        fallback = url_for(
            "dashboard",
            date=selected_date,
            fo_view=normalize_fo_view(request.form.get("fo_view"), "exercicios"),
        )
        scheduled_date = parse_iso_date(request.form.get("scheduled_date")).isoformat()

        with connect_db(db_path) as conn:
            reschedule_exercise_task(
                conn=conn,
                task_id=task_id,
                scheduled_date=scheduled_date,
            )
            conn.commit()

        return redirect(safe_return_to(return_to, fallback))

    return app
