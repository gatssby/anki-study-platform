#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sqlite_safe_backup import create_sqlite_snapshot, timestamped_snapshot_path
from app.fo_planner import (
    AREA_ORDER,
    DEFAULT_CUT_SUBJECTS,
    SUBJECT_AREAS,
    Assignment,
    Lesson,
    StudyDay,
    build_fo_plan,
    select_eligible_fo_lessons,
)

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "cronograma.db"


DAY_NAMES = {
    1: "Segunda",
    2: "Terca",
    3: "Quarta",
    4: "Quinta",
    5: "Sexta",
    6: "Sabado",
    7: "Domingo",
}


@dataclass(frozen=True)
class ScheduleSettings:
    target_finish_date: date | None
    exam_date: date | None
    finish_offset_days_before_exam: int | None
    include_weekends: bool
    auto_adapt_enabled: bool
    max_daily_minutes_weekday: int
    max_daily_minutes_saturday: int
    max_daily_minutes_sunday: int

    def effective_target_finish_date(self) -> date | None:
        if self.target_finish_date:
            return self.target_finish_date
        if self.exam_date and self.finish_offset_days_before_exam is not None:
            return self.exam_date - timedelta(days=self.finish_offset_days_before_exam)
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gera cronograma FO adaptativo baseado nas aulas do portal/lessons."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Caminho do banco SQLite.")
    parser.add_argument("--as-of-date", help="Data inicial em YYYY-MM-DD. Padrao: hoje em America/Sao_Paulo.")
    parser.add_argument("--target-end-date", "--target-finish-date", dest="target_end_date", help="Data limite das aulas.")
    parser.add_argument("--study-weekends", dest="study_weekends", action="store_true", default=None)
    parser.add_argument("--no-study-weekends", dest="study_weekends", action="store_false")
    parser.add_argument("--cut-subject", action="append", default=[], help="Materia/prefixo a cortar. Pode repetir.")
    parser.add_argument("--daily-capacity-minutes", type=int, help="Compatibilidade: valor informativo, ignorado no modo sem teto.")
    parser.add_argument("--preserve-past", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--adaptive-mode", action=argparse.BooleanOptionalAction, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Simula sem alterar o banco. Padrao.")
    mode.add_argument("--apply", action="store_true", help="Aplica a distribuicao futura com backup automatico.")
    return parser.parse_args()


def current_local_date() -> date:
    try:
        tz = ZoneInfo("America/Sao_Paulo")
    except Exception:
        tz = timezone(timedelta(hours=-3))
    return datetime.now(tz).date()


def normalize(value: str | None) -> str:
    text = value or ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.upper().split())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def parse_date(value: Any) -> date | None:
    if value in {None, ""}:
        return None
    return date.fromisoformat(str(value))


def parse_optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    return int(value)


def get_schedule_settings(conn: sqlite3.Connection) -> ScheduleSettings:
    row = conn.execute(
        """
        SELECT
            exam_date,
            target_finish_date,
            finish_offset_days_before_exam,
            include_weekends,
            auto_adapt_enabled,
            max_daily_minutes_weekday,
            max_daily_minutes_saturday,
            max_daily_minutes_sunday
        FROM schedule_settings
        WHERE id = 1
        """
    ).fetchone()
    if not row:
        return ScheduleSettings(
            target_finish_date=None,
            exam_date=None,
            finish_offset_days_before_exam=None,
            include_weekends=False,
            auto_adapt_enabled=True,
            max_daily_minutes_weekday=240,
            max_daily_minutes_saturday=180,
            max_daily_minutes_sunday=180,
        )
    return ScheduleSettings(
        target_finish_date=parse_date(row["target_finish_date"]),
        exam_date=parse_date(row["exam_date"]),
        finish_offset_days_before_exam=parse_optional_int(row["finish_offset_days_before_exam"]),
        include_weekends=bool(row["include_weekends"]),
        auto_adapt_enabled=bool(row["auto_adapt_enabled"]),
        max_daily_minutes_weekday=int(row["max_daily_minutes_weekday"] or 240),
        max_daily_minutes_saturday=int(row["max_daily_minutes_saturday"] or 180),
        max_daily_minutes_sunday=int(row["max_daily_minutes_sunday"] or 180),
    )


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def app_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key = ?", (key,)).fetchone()
    return str(row["setting_value"]) if row else None


def resolve_config(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    settings = get_schedule_settings(conn)
    target = date.fromisoformat(args.target_end_date) if args.target_end_date else settings.effective_target_finish_date()
    if target is None:
        raise ValueError("Defina target_end_date/target_finish_date antes do dry-run.")

    cut_setting = app_setting(conn, "adaptive_cut_subjects")
    configured_cuts = [item.strip() for item in (cut_setting or "").split(",") if item.strip()]
    cut_subjects = tuple(args.cut_subject or configured_cuts or DEFAULT_CUT_SUBJECTS)
    study_weekends = settings.include_weekends if args.study_weekends is None else bool(args.study_weekends)
    preserve_setting = app_setting(conn, "adaptive_preserve_past")
    preserve_past = (
        str(preserve_setting).strip().lower() not in {"0", "false", "no", "nao", "não"}
        if preserve_setting is not None
        else True
    )
    if args.preserve_past is not None:
        preserve_past = bool(args.preserve_past)
    adaptive_mode = settings.auto_adapt_enabled if args.adaptive_mode is None else bool(args.adaptive_mode)
    daily_capacity = args.daily_capacity_minutes
    if daily_capacity is None:
        daily_capacity = None

    return {
        "target_end_date": target,
        "study_weekends": study_weekends,
        "cut_subjects": cut_subjects,
        "daily_capacity_minutes": daily_capacity,
        "preserve_past": preserve_past,
        "adaptive_mode": adaptive_mode,
        "settings": settings,
    }


def fetch_pending_lessons(
    conn: sqlite3.Connection,
    as_of_date: date,
    cut_subjects: tuple[str, ...],
) -> tuple[list[Lesson], dict[str, int], list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT *
        FROM lessons
        WHERE track_code = 'FO'
          AND lesson_type = 'lesson'
          AND is_seen = 0
        ORDER BY
          COALESCE(subject_prefix, ''),
          COALESCE(module_number, 9999),
          COALESCE(lesson_number, 9999),
          recommended_date,
          slot_index,
          lesson_code
        """
    ).fetchall()

    return select_eligible_fo_lessons(rows, cut_subjects=cut_subjects)


def unavailable_capacity_by_date(conn: sqlite3.Connection) -> dict[date, int]:
    result: dict[date, int] = {}
    rows = conn.execute(
        """
        SELECT start_date, end_date, capacity_percent
        FROM schedule_unavailability
        ORDER BY start_date, end_date, id
        """
    ).fetchall()
    for row in rows:
        current = date.fromisoformat(row["start_date"])
        end_date = date.fromisoformat(row["end_date"])
        capacity_percent = int(row["capacity_percent"])
        while current <= end_date:
            result[current] = min(result.get(current, 100), capacity_percent)
            current += timedelta(days=1)
    return result


def area_for_subject(subject: str) -> str:
    return SUBJECT_AREAS.get(normalize(subject), "outras")


def area_sort_key(area: str) -> tuple[int, str]:
    try:
        return AREA_ORDER.index(area), area
    except ValueError:
        return len(AREA_ORDER), area


def summarize(assignments: list[Assignment], days: list[StudyDay], lessons: list[Lesson], as_of_date: date) -> dict[str, Any]:
    by_day: dict[date, list[Assignment]] = defaultdict(list)
    by_subject: Counter[str] = Counter()
    for assignment in assignments:
        by_day[assignment.day.date_value].append(assignment)
        by_subject[assignment.lesson.subject_prefix] += 1

    total_minutes = sum(lesson.minutes for lesson in lessons)
    available_count = len(days)
    max_minutes = max((sum(item.lesson.minutes for item in items) for items in by_day.values()), default=0)
    max_lessons = max((len(items) for items in by_day.values()), default=0)
    return {
        "total_minutes": total_minutes,
        "average_lessons_per_day": (len(lessons) / available_count) if available_count else 0,
        "average_minutes_per_day": (total_minutes / available_count) if available_count else 0,
        "max_minutes_per_day": max_minutes,
        "max_lessons_per_day": max_lessons,
        "overdue_lessons": sum(1 for lesson in lessons if lesson.recommended_date < as_of_date),
        "included_subjects": dict(sorted(by_subject.items())),
        "first_30_days": [
            {
                "date": day.date_value.isoformat(),
                "lessons": len(by_day.get(day.date_value, [])),
                "minutes": sum(item.lesson.minutes for item in by_day.get(day.date_value, [])),
                "subjects": ",".join(item.lesson.subject_prefix for item in by_day.get(day.date_value, [])),
            }
            for day in days[:30]
        ],
    }


def assignments_for_first_days(assignments: list[Assignment], days: list[StudyDay], count: int) -> list[Assignment]:
    selected_dates = {day.date_value for day in days[:count]}
    return [assignment for assignment in assignments if assignment.day.date_value in selected_dates]


def first_days_distribution(assignments: list[Assignment], days: list[StudyDay], count: int = 14) -> dict[str, dict[str, int]]:
    selected = assignments_for_first_days(assignments, days, count)
    by_subject: Counter[str] = Counter()
    by_area: Counter[str] = Counter()
    for assignment in selected:
        by_subject[assignment.lesson.subject_prefix] += 1
        by_area[assignment.lesson.area] += 1
    return {
        "subjects": dict(sorted(by_subject.items())),
        "areas": dict(sorted(by_area.items(), key=lambda item: area_sort_key(item[0]))),
    }


def detect_repetitive_sequences(assignments: list[Assignment], days: list[StudyDay], count: int = 14) -> list[dict[str, Any]]:
    selected = assignments_for_first_days(assignments, days, count)
    selected.sort(key=lambda item: (item.day.date_value, item.slot_index))
    rows: list[dict[str, Any]] = []
    for label, values in (
        ("materia", [item.lesson.subject_prefix for item in selected]),
        ("area", [item.lesson.area for item in selected]),
    ):
        start = 0
        while start < len(values):
            end = start + 1
            while end < len(values) and values[end] == values[start]:
                end += 1
            length = end - start
            if length >= 2:
                rows.append(
                    {
                        "tipo": f"{label}_consecutiva",
                        "sequencia": values[start],
                        "repeticoes": length,
                    }
                )
            start = end

        for size in (2, 3):
            counter: Counter[tuple[str, ...]] = Counter(
                tuple(values[index : index + size])
                for index in range(0, max(len(values) - size + 1, 0))
            )
            for sequence, repetitions in counter.items():
                if repetitions >= 3:
                    rows.append(
                        {
                            "tipo": f"{label}_padrao_{size}",
                            "sequencia": " > ".join(sequence),
                            "repeticoes": repetitions,
                        }
                    )
    rows.sort(key=lambda item: (-int(item["repeticoes"]), item["tipo"], item["sequencia"]))
    return rows[:10]


def parse_seen_at_as_utc_local_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        local_tz = ZoneInfo("America/Sao_Paulo")
    except Exception:
        local_tz = timezone(timedelta(hours=-3))
    return parsed.astimezone(local_tz).date()


def validate_exercise_schedule(conn: sqlite3.Connection) -> dict[str, Any]:
    offset_raw = app_setting(conn, "fo_exercise_offset_days")
    try:
        offset_days = int(offset_raw or 3)
    except ValueError:
        offset_days = 3
    rows = conn.execute(
        """
        SELECT
            exercise_tasks.id,
            exercise_tasks.source_lesson_code,
            exercise_tasks.scheduled_date,
            exercise_tasks.status,
            exercise_tasks.is_active,
            exercise_tasks.manually_moved,
            lessons.is_seen,
            lessons.seen_at
        FROM exercise_tasks
        JOIN lessons ON lessons.lesson_code = exercise_tasks.source_lesson_code
        WHERE lessons.track_code = 'FO'
          AND lessons.lesson_type = 'lesson'
        ORDER BY exercise_tasks.id
        """
    ).fetchall()

    active_seen_bad = 0
    active_unseen_bad = 0
    naive_mismatches: list[dict[str, Any]] = []
    local_mismatches: list[dict[str, Any]] = []
    for row in rows:
        is_seen = int(row["is_seen"] or 0) == 1
        is_active = int(row["is_active"] or 0) == 1
        if is_seen and not is_active:
            active_seen_bad += 1
        if not is_seen and is_active:
            active_unseen_bad += 1
        if not is_seen or int(row["manually_moved"] or 0) == 1:
            continue
        scheduled = date.fromisoformat(row["scheduled_date"])
        try:
            naive_date = datetime.fromisoformat(str(row["seen_at"]).replace(" ", "T")).date()
        except ValueError:
            continue
        expected_naive = naive_date + timedelta(days=offset_days)
        local_seen_date = parse_seen_at_as_utc_local_date(row["seen_at"])
        expected_local = local_seen_date + timedelta(days=offset_days) if local_seen_date else None
        if scheduled != expected_naive:
            naive_mismatches.append(
                {
                    "source_lesson_code": row["source_lesson_code"],
                    "scheduled_date": scheduled.isoformat(),
                    "expected_date": expected_naive.isoformat(),
                    "seen_at": row["seen_at"],
                }
            )
        if expected_local and scheduled != expected_local:
            local_mismatches.append(
                {
                    "source_lesson_code": row["source_lesson_code"],
                    "scheduled_date": scheduled.isoformat(),
                    "expected_date": expected_local.isoformat(),
                    "seen_at": row["seen_at"],
                }
            )
    return {
        "relation": "exercise_tasks.source_lesson_code -> lessons.lesson_code",
        "offset_days": offset_days,
        "total_tasks": len(rows),
        "active_seen_bad": active_seen_bad,
        "active_unseen_bad": active_unseen_bad,
        "seen_at_naive_mismatch_count": len(naive_mismatches),
        "seen_at_naive_mismatch_examples": naive_mismatches[:10],
        "seen_at_utc_to_sp_mismatch_count": len(local_mismatches),
        "seen_at_utc_to_sp_mismatch_examples": local_mismatches[:10],
    }


def seen_lesson_expected_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            lesson_code,
            recommended_date,
            seen_at,
            subject_prefix,
            module_label,
            lesson_number
        FROM lessons
        WHERE track_code = 'FO'
          AND lesson_type = 'lesson'
          AND is_seen = 1
          AND seen_at IS NOT NULL
          AND seen_at != ''
        ORDER BY seen_at, lesson_code
        """
    ).fetchall()

    expected_rows: list[dict[str, Any]] = []
    for row in rows:
        expected_date = parse_seen_at_as_utc_local_date(row["seen_at"])
        if not expected_date:
            continue
        expected_rows.append(
            {
                "lesson_code": row["lesson_code"],
                "recommended_date": row["recommended_date"],
                "seen_at": row["seen_at"],
                "expected_recommended_date": expected_date.isoformat(),
                "subject_prefix": row["subject_prefix"],
                "module_label": row["module_label"],
                "lesson_number": row["lesson_number"],
            }
        )
    return expected_rows


def seen_lesson_normalization_changes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        row
        for row in seen_lesson_expected_rows(conn)
        if row["recommended_date"] != row["expected_recommended_date"]
    ]


def print_report(report: dict[str, Any]) -> None:
    print(f"modo: {report['mode']}")
    print(f"data_inicial_usada: {report['as_of_date']}")
    print(f"target_end_date: {report['target_end_date']}")
    print(f"finais_de_semana_incluidos: {str(report['study_weekends']).lower()}")
    print(f"adaptive_mode: {str(report['adaptive_mode']).lower()}")
    print(f"preserve_past: {str(report['preserve_past']).lower()}")
    print(f"total_aulas_pendentes_incluidas: {report['pending_lessons']}")
    print(f"plano_viavel: {str(report['is_feasible']).lower()}")
    print(f"aulas_alocadas: {report['allocated_lessons']}")
    print(f"aulas_nao_alocadas: {report['unallocated_lesson_count']}")
    print(f"carga_total_segundos: {report['total_load_seconds']}")
    print(f"aulas_com_duracao_estimada_45min: {report['estimated_duration_lesson_count']}")
    print("modo_capacidade: unlimited")
    print(f"capacidade_configurada_ignorada_segundos: {report['total_capacity_seconds']}")
    print(f"deficit_segundos: {report['deficit_seconds']}")
    print(f"total_dias_disponiveis: {report['available_days']}")
    print(f"total_dias_indisponiveis_pulados: {report['skipped_days']}")
    print(f"aulas_por_dia_necessarias: {report['average_lessons_per_day']:.2f}")
    print(f"minutos_por_dia_estimados: {report['average_minutes_per_day']:.1f}")
    print(f"max_aulas_em_um_dia: {report['max_lessons_per_day']}")
    print(f"max_minutos_em_um_dia: {report['max_minutes_per_day']}")
    print(f"aulas_atrasadas_priorizadas: {report['overdue_lessons']}")
    print(f"aulas_vistas_preservadas: {report['seen_lessons_preserved']}")
    print(f"aulas_vistas_a_normalizar: {report['seen_lesson_normalization_count']}")
    print(f"dias_passados_preservados: {report['past_days_preserved']}")
    print(f"daily_assignments_futuros_a_limpar_no_apply: {report['future_daily_assignments_to_replace']}")
    print(f"listas_existentes_preservadas: {report['exercise_tasks_preserved']}")
    print(f"listas_concluidas_preservadas: {report['done_exercise_tasks_preserved']}")
    print(f"materias_cortadas_config: {', '.join(report['cut_subjects']) or '-'}")
    print("aulas_cortadas:")
    for reason, count in sorted(report["cut_counts"].items()):
        print(f"- {reason}: {count}")
    print("materias_incluidas:")
    for subject, count in report["included_subjects"].items():
        print(f"- {subject}: {count}")
    print("distribuicao_primeiros_14_dias_por_materia:")
    for subject, count in report["first_14_distribution"]["subjects"].items():
        print(f"- {subject}: {count}")
    print("distribuicao_primeiros_14_dias_por_area:")
    for area, count in report["first_14_distribution"]["areas"].items():
        print(f"- {area}: {count}")
    print("top_10_sequencias_repetitivas:")
    if report["repetitive_sequences"]:
        for row in report["repetitive_sequences"]:
            print(f"- tipo={row['tipo']} sequencia={row['sequencia']} repeticoes={row['repeticoes']}")
    else:
        print("- 0")
    validation = report["exercise_validation"]
    print("validacao_listas_3_dias_apos_seen_at:")
    print(f"- relacao: {validation['relation']}")
    print(f"- offset_days: {validation['offset_days']}")
    print(f"- total_tasks: {validation['total_tasks']}")
    print(f"- active_seen_bad: {validation['active_seen_bad']}")
    print(f"- active_unseen_bad: {validation['active_unseen_bad']}")
    print(f"- mismatches_seen_at_data_servidor: {validation['seen_at_naive_mismatch_count']}")
    print(f"- mismatches_seen_at_utc_para_sao_paulo: {validation['seen_at_utc_to_sp_mismatch_count']}")
    if validation["seen_at_naive_mismatch_examples"]:
        print("  exemplos_data_servidor:")
        for item in validation["seen_at_naive_mismatch_examples"]:
            print(
                f"  - {item['source_lesson_code']} scheduled={item['scheduled_date']} "
                f"expected={item['expected_date']} seen_at={item['seen_at']}"
            )
    if validation["seen_at_utc_to_sp_mismatch_examples"]:
        print("  exemplos_utc_para_sao_paulo:")
        for item in validation["seen_at_utc_to_sp_mismatch_examples"]:
            print(
                f"  - {item['source_lesson_code']} scheduled={item['scheduled_date']} "
                f"expected={item['expected_date']} seen_at={item['seen_at']}"
            )
    print("  proposta_correcao_dry_run:")
    print("  - padronizar regra futura em data local America/Sao_Paulo ao marcar aula vista")
    print("  - antes de corrigir historico, gerar dry-run dedicado listando source_lesson_code, scheduled atual e scheduled proposto")
    print("  - nao sobrescrever listas status=done nem manually_moved=1")
    print("normalizacao_aulas_vistas:")
    if report["seen_lesson_normalization_examples"]:
        for item in report["seen_lesson_normalization_examples"]:
            print(
                f"- {item['lesson_code']} recommended_atual={item['recommended_date']} "
                f"seen_at={item['seen_at']} recommended_esperado={item['expected_recommended_date']}"
            )
    else:
        print("- 0")
    print("primeiras_30_datas_geradas:")
    for row in report["first_30_days"]:
        print(f"- {row['date']} aulas={row['lessons']} minutos={row['minutes']} materias={row['subjects'] or '-'}")
    print("alertas:")
    if report["warnings"]:
        for warning in report["warnings"]:
            print(f"- {warning}")
    else:
        print("- 0")
    if report.get("backup_path"):
        print(f"backup: {report['backup_path']}")


def build_report(conn: sqlite3.Connection, args: argparse.Namespace, db_path: Path) -> tuple[dict[str, Any], list[Assignment]]:
    config = resolve_config(conn, args)
    as_of_date = date.fromisoformat(args.as_of_date) if args.as_of_date else current_local_date()
    target = config["target_end_date"]
    if target < as_of_date:
        raise ValueError(f"target_end_date {target.isoformat()} anterior a data inicial {as_of_date.isoformat()}.")
    if not config["adaptive_mode"]:
        raise ValueError("adaptive_mode=false; nada a gerar.")

    lessons, cut_counts, cut_examples = fetch_pending_lessons(conn, as_of_date=as_of_date, cut_subjects=config["cut_subjects"])
    settings = config["settings"]
    override = config["daily_capacity_minutes"]
    plan = build_fo_plan(
        lessons,
        start_date=as_of_date,
        end_date=target,
        include_weekends=config["study_weekends"],
        capacity_percent_by_date=unavailable_capacity_by_date(conn),
        max_daily_minutes_weekday=override or settings.max_daily_minutes_weekday,
        max_daily_minutes_saturday=override or settings.max_daily_minutes_saturday,
        max_daily_minutes_sunday=override or settings.max_daily_minutes_sunday,
    )
    days, skipped, assignments = list(plan.days), list(plan.skipped_dates), list(plan.assignments)
    summary = summarize(assignments=assignments, days=days, lessons=lessons, as_of_date=as_of_date)
    seen_preserved = scalar(conn, "SELECT COUNT(*) FROM lessons WHERE track_code = 'FO' AND is_seen = 1")
    future_da = scalar(conn, "SELECT COUNT(*) FROM daily_assignments WHERE dashboard_date >= ?", (as_of_date.isoformat(),))
    exercise_total = scalar(conn, "SELECT COUNT(*) FROM exercise_tasks")
    exercise_done = scalar(conn, "SELECT COUNT(*) FROM exercise_tasks WHERE status = 'done'")
    past_days = scalar(conn, "SELECT COUNT(DISTINCT recommended_date) FROM lessons WHERE recommended_date < ?", (as_of_date.isoformat(),))
    exercise_validation = validate_exercise_schedule(conn)
    seen_normalization_changes = seen_lesson_normalization_changes(conn)

    warnings: list[str] = []
    if not plan.is_feasible:
        warnings.append(
            f"Plano FO inviavel: {len(plan.unallocated_lessons)} aulas e "
            f"{plan.unallocated_load_seconds} segundos nao foram alocados."
        )
    if assignments and assignments[-1].day.date_value > target:
        warnings.append("Plano ultrapassou target_end_date.")
    if summary["average_lessons_per_day"] > 12:
        warnings.append("Volume alto: media acima de 12 aulas por dia.")
    if cut_examples:
        warnings.append(f"{len(cut_examples)} exemplos de cortes foram omitidos do plano; veja contagem por motivo.")
    if exercise_validation["seen_at_naive_mismatch_count"]:
        warnings.append(
            "Listas vistas divergem de date(seen_at)+offset quando seen_at e interpretado como data do servidor; "
            "isso indica mistura de UTC com data local."
        )
    if exercise_validation["seen_at_utc_to_sp_mismatch_count"]:
        warnings.append(
            "Ha listas existentes que tambem divergiriam de seen_at UTC convertido para America/Sao_Paulo; "
            "qualquer correcao deve ser aplicada em dry-run especifico antes."
        )
    if seen_normalization_changes:
        warnings.append(
            f"{len(seen_normalization_changes)} aulas vistas tem recommended_date diferente da data local de seen_at; "
            "elas serao normalizadas somente em --apply."
        )

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "as_of_date": as_of_date.isoformat(),
        "target_end_date": target.isoformat(),
        "study_weekends": config["study_weekends"],
        "adaptive_mode": config["adaptive_mode"],
        "preserve_past": config["preserve_past"],
        "pending_lessons": len(lessons),
        "allocated_lessons": len(plan.assignments),
        "is_feasible": plan.is_feasible,
        "capacity_mode": plan.capacity_mode,
        "configured_daily_minutes_ignored": True,
        "total_load_seconds": plan.total_load_seconds,
        "estimated_duration_lesson_count": len(plan.estimated_duration_lesson_codes),
        "estimated_duration_lesson_codes": list(plan.estimated_duration_lesson_codes),
        "total_capacity_seconds": plan.total_capacity_seconds,
        "raw_capacity_deficit_seconds": plan.raw_capacity_deficit_seconds,
        "deficit_seconds": plan.deficit_seconds,
        "deficit_minutes": plan.deficit_seconds / 60,
        "unallocated_lesson_count": len(plan.unallocated_lessons),
        "unallocated_load_seconds": plan.unallocated_load_seconds,
        "unallocated_lessons": [
            {
                "lesson_code": item.lesson.lesson_code,
                "duration_seconds": item.duration_seconds,
                "max_capacity_seconds": item.max_capacity_seconds,
                "reason": item.reason,
            }
            for item in plan.unallocated_lessons
        ],
        "first_date": plan.first_date.isoformat() if plan.first_date else None,
        "last_date": plan.last_date.isoformat() if plan.last_date else None,
        "empty_days": plan.empty_day_count,
        "max_daily_load_seconds": plan.max_daily_load_seconds,
        "daily_summary": plan.daily_summary,
        "available_days": len(days),
        "skipped_days": len(skipped),
        "average_lessons_per_day": summary["average_lessons_per_day"],
        "average_minutes_per_day": summary["average_minutes_per_day"],
        "max_lessons_per_day": max((row["lesson_count"] for row in plan.daily_summary), default=0),
        "max_minutes_per_day": plan.max_daily_load_seconds / 60,
        "overdue_lessons": summary["overdue_lessons"],
        "seen_lessons_preserved": seen_preserved,
        "seen_lesson_normalization_count": len(seen_normalization_changes),
        "seen_lesson_normalization_examples": seen_normalization_changes[:30],
        "past_days_preserved": past_days,
        "future_daily_assignments_to_replace": future_da,
        "exercise_tasks_preserved": exercise_total,
        "done_exercise_tasks_preserved": exercise_done,
        "cut_subjects": tuple(config["cut_subjects"]),
        "cut_counts": cut_counts,
        "included_subjects": summary["included_subjects"],
        "first_14_distribution": first_days_distribution(assignments=assignments, days=days, count=14),
        "repetitive_sequences": detect_repetitive_sequences(assignments=assignments, days=days, count=14),
        "exercise_validation": exercise_validation,
        "first_30_days": summary["first_30_days"],
        "warnings": warnings,
        "db_sha256_before": sha256_file(db_path),
    }
    return report, assignments


def summary_day(assignments: list[Assignment], day_value: date) -> list[Assignment]:
    return [assignment for assignment in assignments if assignment.day.date_value == day_value]


def create_backup(db_path: Path) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_path = timestamped_snapshot_path(
        backup_dir,
        prefix="cronograma-pre-adaptive-",
    )
    return create_sqlite_snapshot(db_path, backup_path).path


def apply_assignments(conn: sqlite3.Connection, assignments: list[Assignment], as_of_date: date) -> None:
    conn.execute("DELETE FROM daily_assignments WHERE dashboard_date >= ?", (as_of_date.isoformat(),))
    per_day_slot: Counter[date] = Counter()
    start_date = as_of_date
    for assignment in assignments:
        day = assignment.day.date_value
        per_day_slot[day] += 1
        week_number = ((day - start_date).days // 7) + 1
        conn.execute(
            """
            UPDATE lessons
            SET recommended_date = ?,
                week_number = ?,
                day_index = ?,
                day_name = ?,
                slot_index = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE lesson_code = ?
              AND track_code = 'FO'
              AND lesson_type = 'lesson'
              AND is_seen = 0
            """,
            (
                day.isoformat(),
                max(1, week_number),
                day.isoweekday(),
                DAY_NAMES[day.isoweekday()],
                per_day_slot[day],
                assignment.lesson.lesson_code,
            ),
        )


def normalize_seen_lesson_dates(conn: sqlite3.Connection) -> int:
    expected_rows = seen_lesson_expected_rows(conn)
    per_date_slot: Counter[str] = Counter()
    updated = 0
    for row in expected_rows:
        expected_date = date.fromisoformat(row["expected_recommended_date"])
        per_date_slot[row["expected_recommended_date"]] += 1
        cursor = conn.execute(
            """
            UPDATE lessons
            SET recommended_date = ?,
                day_index = ?,
                day_name = ?,
                slot_index = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE lesson_code = ?
              AND track_code = 'FO'
              AND lesson_type = 'lesson'
              AND is_seen = 1
            """,
            (
                expected_date.isoformat(),
                expected_date.isoweekday(),
                DAY_NAMES[expected_date.isoweekday()],
                per_date_slot[row["expected_recommended_date"]],
                row["lesson_code"],
            ),
        )
        updated += int(cursor.rowcount or 0)
    return updated


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser().resolve()
    with connect_db(db_path) as conn:
        before = sha256_file(db_path)
        report, assignments = build_report(conn=conn, args=args, db_path=db_path)
        if args.apply:
            if not report["is_feasible"]:
                raise ValueError(
                    "Plano FO inviável; --apply recusado porque há aulas não alocadas até a data final."
                )
            backup_path = create_backup(db_path)
            normalize_seen_lesson_dates(conn)
            apply_assignments(conn=conn, assignments=assignments, as_of_date=date.fromisoformat(report["as_of_date"]))
            conn.commit()
            report["backup_path"] = str(backup_path)
            report["db_sha256_after"] = sha256_file(db_path)
        else:
            after = sha256_file(db_path)
            report["db_sha256_after"] = after
            report["dry_run_db_unchanged"] = before == after
        print_report(report)
        if not args.apply:
            print(f"dry_run_db_unchanged: {str(report['dry_run_db_unchanged']).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
