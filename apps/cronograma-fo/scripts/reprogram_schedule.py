#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backup import create_timestamped_backup, describe_backup_contents, inspect_backup, validate_backup_schema
from app.db import DEFAULT_DB_PATH, connect_db, init_db
from app.reprogramming import (
    ScheduleSettings,
    add_unavailability,
    apply_reprogramming,
    auto_adapt_if_enabled,
    build_reprogram_report,
    cut_lesson,
    current_local_date,
    diagnose_distribution,
    get_schedule_settings,
    list_cut_lessons,
    list_unavailability,
    merge_settings,
    remove_unavailability,
    save_schedule_settings,
    uncut_lesson,
    validate_backup_against_source,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reprograma o cronograma UN + FO com dry-run e apply seguros.",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="Caminho do banco SQLite.",
    )
    parser.add_argument(
        "--as-of-date",
        help="Data de referencia para o recalculo em YYYY-MM-DD (padrao: hoje em America/Sao_Paulo).",
    )
    add_recalc_arguments(parser)

    recalc_parent = argparse.ArgumentParser(add_help=False)
    add_recalc_arguments(recalc_parent)

    subparsers = parser.add_subparsers(dest="command")

    show_settings_parser = subparsers.add_parser("show-settings", help="Mostra configuracoes persistidas.")
    show_settings_parser.set_defaults(handler=handle_show_settings)

    set_exam_parser = subparsers.add_parser("set-exam-date", help="Define a data da prova.")
    set_exam_parser.add_argument("exam_date", help="Data da prova em YYYY-MM-DD.")
    set_exam_parser.set_defaults(handler=handle_set_exam_date)

    set_target_parser = subparsers.add_parser(
        "set-target-finish-date",
        help="Define data fixa para terminar todas as aulas.",
    )
    set_target_parser.add_argument("target_finish_date", help="Data em YYYY-MM-DD.")
    set_target_parser.set_defaults(handler=handle_set_target_finish_date)

    set_offset_parser = subparsers.add_parser(
        "set-finish-offset-days",
        help="Define termino relativo em X dias antes da prova.",
    )
    set_offset_parser.add_argument("days", type=int, help="Dias antes da prova.")
    set_offset_parser.set_defaults(handler=handle_set_finish_offset)

    set_capacity_parser = subparsers.add_parser(
        "set-capacity",
        help="Define referencias diarias informativas, sem limitar o planner.",
    )
    set_capacity_parser.add_argument("--weekday", type=int, required=True, help="Referencia informativa de segunda a sexta.")
    set_capacity_parser.add_argument("--saturday", type=int, required=True, help="Referencia informativa de sabado.")
    set_capacity_parser.add_argument("--sunday", type=int, required=True, help="Referencia informativa de domingo.")
    set_capacity_parser.set_defaults(handler=handle_set_capacity)

    set_flags_parser = subparsers.add_parser(
        "set-flags",
        help="Define flags persistidas da reprogramacao.",
    )
    add_boolean_setting_flags(set_flags_parser)
    set_flags_parser.set_defaults(handler=handle_set_flags)

    list_unavailability_parser = subparsers.add_parser(
        "list-unavailability",
        help="Lista indisponibilidades cadastradas.",
    )
    list_unavailability_parser.set_defaults(handler=handle_list_unavailability)

    add_unavailability_parser = subparsers.add_parser(
        "add-unavailability",
        help="Cadastra indisponibilidade ou capacidade parcial.",
    )
    add_unavailability_parser.add_argument("--date", help="Dia unico em YYYY-MM-DD.")
    add_unavailability_parser.add_argument("--start-date", help="Inicio do intervalo em YYYY-MM-DD.")
    add_unavailability_parser.add_argument("--end-date", help="Fim do intervalo em YYYY-MM-DD.")
    add_unavailability_parser.add_argument("--reason", help="Motivo opcional.")
    capacity_group = add_unavailability_parser.add_mutually_exclusive_group(required=True)
    capacity_group.add_argument("--capacity-percent", type=int, help="Capacidade disponivel entre 0 e 100.")
    capacity_group.add_argument(
        "--unavailable",
        action="store_true",
        help="Atalho para capacidade 0%%.",
    )
    add_unavailability_parser.set_defaults(handler=handle_add_unavailability)

    remove_unavailability_parser = subparsers.add_parser(
        "remove-unavailability",
        help="Remove uma indisponibilidade pelo id.",
    )
    remove_unavailability_parser.add_argument("id", type=int, help="ID da indisponibilidade.")
    remove_unavailability_parser.set_defaults(handler=handle_remove_unavailability)

    list_cuts_parser = subparsers.add_parser("list-cuts", help="Lista aulas cortadas.")
    list_cuts_parser.set_defaults(handler=handle_list_cuts)

    cut_parser = subparsers.add_parser("cut-lesson", help="Corta manualmente uma aula.")
    cut_parser.add_argument("lesson_code", help="Codigo da aula.")
    cut_parser.add_argument("--reason", help="Motivo opcional do corte.")
    cut_parser.set_defaults(handler=handle_cut_lesson)

    uncut_parser = subparsers.add_parser("uncut-lesson", help="Remove o corte manual de uma aula.")
    uncut_parser.add_argument("lesson_code", help="Codigo da aula.")
    uncut_parser.set_defaults(handler=handle_uncut_lesson)

    recalc_parser = subparsers.add_parser(
        "recalculate",
        parents=[recalc_parent],
        help="Roda dry-run ou apply do recálculo.",
    )
    recalc_parser.set_defaults(handler=handle_recalculate)

    validate_backup_parser = subparsers.add_parser(
        "validate-backup",
        help="Valida um backup e confirma aulas vistas + exercicios feitos.",
    )
    validate_backup_parser.add_argument("--backup", help="Arquivo .db para validar. Se omitido, gera um backup novo.")
    validate_backup_parser.set_defaults(handler=handle_validate_backup)

    diagnose_parser = subparsers.add_parser(
        "diagnose-distribution",
        help="Diagnostica a distribuicao atual de FO e UN no banco.",
    )
    diagnose_parser.set_defaults(handler=handle_diagnose_distribution)

    parser.set_defaults(handler=handle_default)
    return parser


def add_recalc_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--exam-date", help="Data da prova em YYYY-MM-DD.")
    finish_group = parser.add_mutually_exclusive_group()
    finish_group.add_argument("--target-finish-date", help="Data fixa para terminar em YYYY-MM-DD.")
    finish_group.add_argument(
        "--finish-offset-days-before-exam",
        type=int,
        help="Termino relativo em X dias antes da prova.",
    )
    add_boolean_setting_flags(parser)
    parser.add_argument("--max-daily-minutes-weekday", type=int, help="Teto diario de segunda a sexta.")
    parser.add_argument("--max-daily-minutes-saturday", type=int, help="Teto diario de sabado.")
    parser.add_argument("--max-daily-minutes-sunday", type=int, help="Teto diario de domingo.")
    parser.add_argument(
        "--diagnose-lesson-prefix",
        action="append",
        default=None,
        help="Exibe ordem/status/data das aulas cujo codigo comeca pelo prefixo (ex.: POR2).",
    )
    parser.add_argument(
        "--save-settings",
        action="store_true",
        help="Persiste as flags informadas mesmo em dry-run.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true", help="So simula, sem alterar a base.")
    mode_group.add_argument("--apply", action="store_true", help="Aplica a reprogramacao com backup automatico.")


def add_boolean_setting_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--include-weekends",
        dest="include_weekends",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Inclui ou exclui sabados e domingos.",
    )
    parser.add_argument(
        "--include-vacations",
        dest="include_vacations",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Inclui ou exclui dias fora do calendario util original.",
    )
    parser.add_argument(
        "--cut-review-free",
        dest="cut_review_free",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Corta automaticamente aulas de revisao/livre.",
    )
    parser.add_argument(
        "--preserve-english-cut",
        dest="preserve_english_cut",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Mantem aulas de ingles cortadas automaticamente.",
    )
    parser.add_argument(
        "--auto-adapt",
        dest="auto_adapt_enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Liga ou desliga a reprogramacao auto-adaptavel.",
    )


def handle_default(args: argparse.Namespace) -> int:
    if has_recalc_intent(args):
        return handle_recalculate(args)
    return handle_show_settings(args)


def handle_show_settings(args: argparse.Namespace) -> int:
    with open_db(args.db) as conn:
        settings = get_schedule_settings(conn)
        entries = list_unavailability(conn)
        cuts = list_cut_lessons(conn)

    effective_target = settings.effective_target_finish_date()
    print("Configuracoes persistidas:")
    print(f"- prova: {format_date(settings.exam_date)}")
    print(f"- target_finish_date: {format_date(settings.target_finish_date)}")
    print(f"- finish_offset_days_before_exam: {format_optional_int(settings.finish_offset_days_before_exam)}")
    print(f"- target efetiva: {format_date(effective_target)}")
    print(f"- include_weekends: {yes_no(settings.include_weekends)}")
    print(f"- include_vacations: {yes_no(settings.include_vacations)}")
    print(f"- cut_review_free: {yes_no(settings.cut_review_free)}")
    print(f"- preserve_english_cut: {yes_no(settings.preserve_english_cut)}")
    print(f"- auto_adapt_enabled: {yes_no(settings.auto_adapt_enabled)}")
    print(f"- max_daily_minutes_weekday: {settings.max_daily_minutes_weekday}")
    print(f"- max_daily_minutes_saturday: {settings.max_daily_minutes_saturday}")
    print(f"- max_daily_minutes_sunday: {settings.max_daily_minutes_sunday}")
    print(f"- indisponibilidades: {len(entries)}")
    print(f"- aulas cortadas: {len(cuts)}")
    return 0


def handle_set_exam_date(args: argparse.Namespace) -> int:
    exam_date = parse_required_date(args.exam_date)
    with open_db(args.db) as conn:
        settings = merge_settings(get_schedule_settings(conn), exam_date=exam_date)
        save_schedule_settings(conn, settings)
        conn.commit()
    print(f"Data da prova atualizada para {exam_date.isoformat()}.")
    print_auto_adapt_result(args.db)
    return 0


def handle_set_target_finish_date(args: argparse.Namespace) -> int:
    target_finish_date = parse_required_date(args.target_finish_date)
    with open_db(args.db) as conn:
        settings = merge_settings(
            get_schedule_settings(conn),
            target_finish_date=target_finish_date,
            finish_offset_days_before_exam=None,
        )
        save_schedule_settings(conn, settings)
        conn.commit()
    print(f"Data-alvo fixa atualizada para {target_finish_date.isoformat()}.")
    print_auto_adapt_result(args.db)
    return 0


def handle_set_finish_offset(args: argparse.Namespace) -> int:
    with open_db(args.db) as conn:
        settings = merge_settings(
            get_schedule_settings(conn),
            target_finish_date=None,
            finish_offset_days_before_exam=max(int(args.days), 0),
        )
        save_schedule_settings(conn, settings)
        conn.commit()
    print(f"Termino relativo atualizado para {max(int(args.days), 0)} dia(s) antes da prova.")
    print_auto_adapt_result(args.db)
    return 0


def handle_set_capacity(args: argparse.Namespace) -> int:
    with open_db(args.db) as conn:
        settings = merge_settings(
            get_schedule_settings(conn),
            max_daily_minutes_weekday=max(int(args.weekday), 0),
            max_daily_minutes_saturday=max(int(args.saturday), 0),
            max_daily_minutes_sunday=max(int(args.sunday), 0),
        )
        save_schedule_settings(conn, settings)
        conn.commit()
    print("Tetos diarios atualizados.")
    print_auto_adapt_result(args.db)
    return 0


def handle_set_flags(args: argparse.Namespace) -> int:
    with open_db(args.db) as conn:
        settings = merge_settings(
            get_schedule_settings(conn),
            include_weekends=args.include_weekends,
            include_vacations=args.include_vacations,
            cut_review_free=args.cut_review_free,
            preserve_english_cut=args.preserve_english_cut,
            auto_adapt_enabled=args.auto_adapt_enabled,
        )
        save_schedule_settings(conn, settings)
        conn.commit()
    print("Flags persistidas atualizadas.")
    print_auto_adapt_result(args.db)
    return 0


def handle_list_unavailability(args: argparse.Namespace) -> int:
    with open_db(args.db) as conn:
        entries = list_unavailability(conn)
    if not entries:
        print("Nenhuma indisponibilidade cadastrada.")
        return 0
    print("Indisponibilidades:")
    for entry in entries:
        reason = f" motivo={entry.reason}" if entry.reason else ""
        print(
            f"- id={entry.id} {entry.start_date.isoformat()}..{entry.end_date.isoformat()} "
            f"capacidade={entry.capacity_percent}%{reason}"
        )
    return 0


def handle_add_unavailability(args: argparse.Namespace) -> int:
    if args.date and (args.start_date or args.end_date):
        raise SystemExit("Use --date para dia unico ou --start-date/--end-date para intervalo, nao ambos.")
    if args.date:
        start_date_value = parse_required_date(args.date)
        end_date_value = start_date_value
    else:
        if not args.start_date or not args.end_date:
            raise SystemExit("Informe --date ou o par --start-date/--end-date.")
        start_date_value = parse_required_date(args.start_date)
        end_date_value = parse_required_date(args.end_date)
    if end_date_value < start_date_value:
        raise SystemExit("end-date nao pode ser anterior a start-date.")

    capacity_percent = 0 if args.unavailable else int(args.capacity_percent)
    with open_db(args.db) as conn:
        entry_id = add_unavailability(
            conn,
            start_date_value=start_date_value,
            end_date_value=end_date_value,
            capacity_percent=capacity_percent,
            reason=args.reason,
        )
        conn.commit()
    print(
        f"Indisponibilidade cadastrada: id={entry_id} {start_date_value.isoformat()}.."
        f"{end_date_value.isoformat()} capacidade={capacity_percent}%."
    )
    print_auto_adapt_result(args.db)
    return 0


def handle_remove_unavailability(args: argparse.Namespace) -> int:
    with open_db(args.db) as conn:
        removed = remove_unavailability(conn, int(args.id))
        conn.commit()
    if removed == 0:
        raise SystemExit(f"Indisponibilidade {args.id} nao encontrada.")
    print(f"Indisponibilidade {args.id} removida.")
    print_auto_adapt_result(args.db)
    return 0


def handle_list_cuts(args: argparse.Namespace) -> int:
    with open_db(args.db) as conn:
        rows = list_cut_lessons(conn)
    if not rows:
        print("Nenhuma aula cortada.")
        return 0
    print("Aulas cortadas:")
    for row in rows:
        reason = row.get("cut_reason") or "-"
        source = row.get("cut_source") or "-"
        module_label = row.get("module_label") or "-"
        print(
            f"- {row['lesson_code']} track={row['track_code']} subject={row.get('subject_prefix') or '-'} "
            f"frente={module_label} source={source} reason={reason} title={row['title_raw']}"
        )
    return 0


def handle_cut_lesson(args: argparse.Namespace) -> int:
    with open_db(args.db) as conn:
        updated = cut_lesson(conn, lesson_code=args.lesson_code, reason=args.reason)
        conn.commit()
    if updated == 0:
        raise SystemExit(f"Aula {args.lesson_code} nao encontrada.")
    print(f"Aula {args.lesson_code} cortada manualmente.")
    print_auto_adapt_result(args.db)
    return 0


def handle_uncut_lesson(args: argparse.Namespace) -> int:
    with open_db(args.db) as conn:
        updated = uncut_lesson(conn, lesson_code=args.lesson_code)
        conn.commit()
    if updated == 0:
        raise SystemExit(f"Aula {args.lesson_code} nao encontrada.")
    print(f"Corte removido da aula {args.lesson_code}.")
    print_auto_adapt_result(args.db)
    return 0


def handle_recalculate(args: argparse.Namespace) -> int:
    with open_db(args.db) as conn:
        persisted = get_schedule_settings(conn)
        effective_settings = build_effective_settings(args=args, persisted=persisted)
        as_of_date = parse_required_date(args.as_of_date) if args.as_of_date else current_local_date()

        if args.apply:
            report = apply_reprogramming(
                conn=conn,
                settings=effective_settings,
                as_of_date=as_of_date,
                db_path=args.db,
                diagnostic_lesson_prefixes=tuple(args.diagnose_lesson_prefix or ()),
            )
            conn.commit()
            print_report(report, mode="apply")
            if report.backup_path:
                print(f"backup_automatico: {report.backup_path}")
            return 0

        report = build_reprogram_report(
            conn=conn,
            settings=effective_settings,
            as_of_date=as_of_date,
            diagnostic_lesson_prefixes=tuple(args.diagnose_lesson_prefix or ()),
        )
        if args.save_settings:
            save_schedule_settings(conn, effective_settings)
            conn.commit()
        print_report(report, mode="dry-run")
    return 0


def handle_validate_backup(args: argparse.Namespace) -> int:
    backup_path: Path
    with open_db(args.db) as conn:
        if args.backup:
            backup_path = Path(args.backup).expanduser().resolve()
        else:
            backup_path = create_timestamped_backup(
                db_path=args.db,
                backup_dir=Path(args.db).expanduser().resolve().parent / "backups",
                prefix="cronograma-validate-",
            )
        inspection = validate_backup_schema(backup_path)
        comparison = validate_backup_against_source(conn, backup_path)

    print("Inspecao do backup:")
    for line in describe_backup_contents(inspection):
        print(f"- {line}")
    print("Comparacao com a base atual:")
    print(f"- aulas_vistas_atual: {comparison['current_seen_lessons']}")
    print(f"- aulas_vistas_backup: {comparison['backup_seen_lessons']}")
    print(f"- exercicios_totais_atual: {comparison['current_exercise_tasks']}")
    print(f"- exercicios_totais_backup: {comparison['backup_exercise_tasks']}")
    print(f"- exercicios_feitos_atual: {comparison['current_done_exercises']}")
    print(f"- exercicios_feitos_backup: {comparison['backup_done_exercises']}")
    print(f"- validação: {'ok' if comparison['matches'] else 'divergente'}")
    return 0 if comparison["matches"] else 1


def handle_diagnose_distribution(args: argparse.Namespace) -> int:
    as_of_date = parse_required_date(args.as_of_date) if args.as_of_date else current_local_date()
    with open_db(args.db) as conn:
        diagnostics = diagnose_distribution(conn=conn, as_of_date=as_of_date)
    print_distribution_diagnostics(diagnostics)
    return 1 if diagnostics.get("warnings") else 0


def build_effective_settings(args: argparse.Namespace, persisted: ScheduleSettings) -> ScheduleSettings:
    exam_date = parse_optional_date(getattr(args, "exam_date", None))
    target_finish_date = parse_optional_date(getattr(args, "target_finish_date", None))
    finish_offset = getattr(args, "finish_offset_days_before_exam", None)
    if finish_offset is not None:
        finish_offset = max(int(finish_offset), 0)

    if target_finish_date is not None:
        finish_offset = None

    return merge_settings(
        persisted,
        exam_date=exam_date,
        target_finish_date=target_finish_date,
        finish_offset_days_before_exam=finish_offset,
        include_weekends=getattr(args, "include_weekends", None),
        include_vacations=getattr(args, "include_vacations", None),
        cut_review_free=getattr(args, "cut_review_free", None),
        preserve_english_cut=getattr(args, "preserve_english_cut", None),
        auto_adapt_enabled=getattr(args, "auto_adapt_enabled", None),
        max_daily_minutes_weekday=getattr(args, "max_daily_minutes_weekday", None),
        max_daily_minutes_saturday=getattr(args, "max_daily_minutes_saturday", None),
        max_daily_minutes_sunday=getattr(args, "max_daily_minutes_sunday", None),
    )


def print_report(report: Any, mode: str) -> None:
    available_days = [day for day in report.available_days if day.is_available]
    unavailable_days = [day for day in report.available_days if not day.is_available]
    print(f"modo: {mode}")
    print(f"prova_configurada: {format_date(report.exam_date)}")
    print(f"data_alvo_termino: {report.target_finish_date.isoformat()}")
    print(f"data_referencia: {report.as_of_date.isoformat()}")
    print("unidade_carga_capacidade: minutos")
    print(f"dias_disponiveis: {len(available_days)}")
    print(f"dias_indisponiveis: {len(unavailable_days)}")
    print(f"indisponibilidades_cadastradas: {len(report.explicit_unavailability)}")
    print(f"carga_total_restante_UN: {report.remaining_units_by_track.get('UN', 0)}")
    print(f"carga_total_restante_FO: {report.remaining_units_by_track.get('FO', 0)}")
    print(f"carga_total_restante: {report.total_remaining_units}")
    print(f"aulas_restantes_FO: {report.remaining_lesson_count_by_track.get('FO', 0)}")
    print(f"aulas_restantes_UN: {report.remaining_lesson_count_by_track.get('UN', 0)}")
    print(f"aulas_distribuidas_FO: {report.distributed_lesson_count_by_track.get('FO', 0)}")
    print(f"aulas_distribuidas_UN: {report.distributed_lesson_count_by_track.get('UN', 0)}")
    print(f"aulas_nao_alocadas_FO: {report.unallocated_lesson_count_by_track.get('FO', 0)}")
    print(f"aulas_nao_alocadas_UN: {report.unallocated_lesson_count_by_track.get('UN', 0)}")
    print(
        "diagnostico_fo_isolado_sem_competicao_UN_nao_alocadas: "
        f"{report.fo_plan_summary.get('standalone_unallocated_lesson_count', 0)}"
    )
    for track_code in ("FO", "UN"):
        duration = report.duration_diagnostics.get(track_code, {})
        print(
            f"duracoes_{track_code}: "
            f"aulas_com_duracao_real={duration.get('real_duration_lesson_count', 0)} "
            f"aulas_com_fallback={duration.get('fallback_lesson_count', 0)} "
            f"minutos_duracao_real={duration.get('real_duration_minutes', 0)} "
            f"minutos_fallback={duration.get('fallback_minutes', 0)} "
            f"fallback_por_aula={duration.get('fallback_minutes_per_lesson', 0)}"
        )
    print(f"media_aulas_por_dia: {report.average_lessons_per_day:.2f}")
    print(f"carga_total_minutos: {report.total_remaining_units}")
    print(f"media_minutos_por_dia: {report.average_minutes_per_day:.2f}")
    print(f"maior_carga_diaria: {report.max_daily_load_units}")
    print(f"menor_carga_diaria: {report.min_daily_load_units}")
    print(
        "tetos_configurados_informativos_sem_efeito_no_planner: "
        f"weekday={report.settings.max_daily_minutes_weekday} "
        f"saturday={report.settings.max_daily_minutes_saturday} "
        f"sunday={report.settings.max_daily_minutes_sunday}"
    )
    print(f"aulas_cortadas_revisao_livre: {report.cut_summary['review_free']}")
    print(f"aulas_ingles_preservadas_cortadas: {report.cut_summary['english']}")
    print(f"aulas_cortadas_manual: {report.cut_summary['manual']}")
    print(f"distribuicoes_geradas: {report.assignment_count}")
    print(f"status_distribuicao: {'completa' if report.feasible else 'erro_estrutural'}")
    print_distribution_summary(report.distribution_diagnostics)
    if report.validation_errors:
        print("validacao_erros:")
        for error in report.validation_errors:
            print(f"- {error}")

    print("primeiras_14_datas:")
    for row in report.first_days:
        print(
            f"- {row['date']} aulas={row['lesson_count']} carga={row['units']} "
            f"capacidade={row['capacity_units']} FO={row['FO']} UN={row['UN']}"
        )

    print("ultimas_14_datas:")
    for row in report.last_days:
        print(
            f"- {row['date']} aulas={row['lesson_count']} carga={row['units']} "
            f"capacidade={row['capacity_units']} FO={row['FO']} UN={row['UN']}"
        )

    print("distribuicao_por_semana:")
    for row in report.weekly_distribution:
        print(
            f"- semana={row['week_start']} dias={row['days']} carga={row['units']} "
            f"FO={row['FO']} UN={row['UN']}"
        )

    print("distribuicao_por_track:")
    for row in report.track_distribution:
        print(f"- track={row['track_code']} aulas={row['lesson_count']} carga={row['units']}")

    print("distribuicao_por_materia_frente:")
    for row in report.group_distribution:
        print(
            f"- track={row['track_code']} grupo={row['group_label']} "
            f"aulas={row['lesson_count']} carga={row['units']}"
        )

    print(f"dias_acima_do_teto: {len(report.overflow_days)}")
    if report.overflow_days:
        print("detalhes_dias_acima_do_teto:")
        for row in report.overflow_days:
            print(
                f"- {row['date']} carga={row['assigned_units']} capacidade={row['capacity_units']} "
                f"overflow={row['overflow_units']}"
            )
    print_lesson_order_diagnostics(report.lesson_order_diagnostics)


def print_lesson_order_diagnostics(diagnostics: list[dict[str, Any]]) -> None:
    if not diagnostics:
        return
    print("diagnostico_ordem_pedagogica:")
    for diagnostic in diagnostics:
        print(
            f"pedagogical_monotonicity={diagnostic['pedagogical_monotonicity']}"
        )
        violation = diagnostic.get("first_violation")
        if violation:
            print(
                f"{violation['previous_lesson_code']} date={violation['previous_date']} "
                f"slot={violation['previous_slot_index']}"
            )
            print(
                f"{violation['lesson_code']} date={violation['date']} "
                f"slot={violation['slot_index']}"
            )
        print(
            f"- prefixo={diagnostic['prefix']} "
            f"status={'ok' if diagnostic['is_valid'] else 'erro'} "
            f"projetadas={diagnostic['projected_lesson_count']} "
            f"nao_alocadas={diagnostic['unallocated_lesson_count']}"
        )
        for error in diagnostic["errors"]:
            print(f"  erro={error}")
        for entry in diagnostic["entries"]:
            print(
                f"  codigo={entry['lesson_code']} status={entry['status']} "
                f"data_projetada={entry['projected_date'] or '-'} "
                f"slot_projetado={entry['projected_slot_index'] or '-'} "
                f"data_atual={entry['current_date'] or '-'} "
                f"slot_atual={entry['current_slot_index'] or '-'} "
                f"motivo={entry['reason'] or '-'}"
            )


def print_distribution_summary(diagnostics: dict[str, Any]) -> None:
    if not diagnostics:
        return
    print("validacao_distribuicao:")
    for track_code in ("FO", "UN"):
        track = diagnostics.get("tracks", {}).get(track_code, {})
        print(
            f"- track={track_code} aulas={track.get('lesson_count', 0)} "
            f"primeira={track.get('first_date') or '-'} ultima={track.get('last_date') or '-'} "
            f"datas_distintas={track.get('distinct_dates', 0)} "
            f"antes_hoje={track.get('before_today_count', 0)} "
            f"apos_alvo={track.get('after_target_count', 0)}"
        )
    warnings = diagnostics.get("warnings") or []
    if warnings:
        print("validacao_avisos:")
        for warning in warnings:
            print(f"- {warning}")


def print_distribution_diagnostics(diagnostics: dict[str, Any]) -> None:
    settings = diagnostics.get("settings") or {}
    print("Diagnostico da distribuicao:")
    print(f"- modo: {diagnostics.get('mode')}")
    print(f"- data_referencia: {diagnostics.get('as_of_date')}")
    print(f"- prova: {settings.get('exam_date', '-')}")
    print(f"- target_finish_date: {settings.get('target_finish_date', '-')}")
    print(f"- target_efetiva: {settings.get('effective_target_finish_date', diagnostics.get('target_finish_date'))}")
    print(f"- include_weekends: {yes_no(bool(settings.get('include_weekends')))}")
    print("Fontes da home/dashboard:")
    for track_code in ("FO", "UN"):
        source = diagnostics.get("sources", {}).get(track_code, {})
        print(
            f"- {track_code}: {source.get('source_table')}.{source.get('source_field')} "
            f"({source.get('notes')})"
        )
    cache_tables = diagnostics.get("cache_tables") or {}
    if cache_tables:
        print("Tabelas materializadas/cache:")
        for name, cache in cache_tables.items():
            print(
                f"- {name}: source_of_truth={yes_no(bool(cache.get('source_of_truth')))} "
                f"linhas={cache.get('row_count', '-')} primeira={cache.get('first_date') or '-'} "
                f"ultima={cache.get('last_date') or '-'} datas_distintas={cache.get('distinct_dates', '-')}"
            )

    print("Distribuicao ativa por track:")
    for track_code in ("FO", "UN"):
        track = diagnostics.get("tracks", {}).get(track_code, {})
        print(
            f"- {track_code}: aulas={track.get('lesson_count', 0)} carga={track.get('units', 0)} "
            f"primeira={track.get('first_date') or '-'} ultima={track.get('last_date') or '-'} "
            f"datas_distintas={track.get('distinct_dates', 0)} "
            f"antes_hoje={track.get('before_today_count', 0)} "
            f"apos_alvo={track.get('after_target_count', 0)}"
        )
        print(f"  top_10_dias_{track_code}:")
        for day in track.get("top_days", []):
            print(
                f"  - {day['date']} aulas={day['lesson_count']} carga={day['units']}"
            )

    pending = diagnostics.get("non_reprogrammable_pending_before_today") or []
    print("Pendencias antigas fora do escopo reprogramavel:")
    if not pending:
        print("- 0")
    for row in pending:
        print(
            f"- track={row['track_code']} tipo={row['lesson_type']} aulas={row['lesson_count']} "
            f"primeira={row['first_date']} ultima={row['last_date']}"
        )

    warnings = diagnostics.get("warnings") or []
    print("Avisos:")
    if not warnings:
        print("- 0")
    for warning in warnings:
        print(f"- {warning}")


def has_recalc_intent(args: argparse.Namespace) -> bool:
    keys = [
        "apply",
        "dry_run",
        "exam_date",
        "target_finish_date",
        "finish_offset_days_before_exam",
        "include_weekends",
        "include_vacations",
        "cut_review_free",
        "preserve_english_cut",
        "auto_adapt_enabled",
        "max_daily_minutes_weekday",
        "max_daily_minutes_saturday",
        "max_daily_minutes_sunday",
        "diagnose_lesson_prefix",
        "save_settings",
    ]
    return any(getattr(args, key, None) not in {None, False} for key in keys)


def open_db(db_path: str) -> Any:
    path = Path(db_path).expanduser().resolve()
    conn = connect_db(path)
    init_db(conn)
    return conn


def parse_required_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_optional_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def format_date(value: date | None) -> str:
    return value.isoformat() if value else "-"


def format_optional_int(value: int | None) -> str:
    return str(value) if value is not None else "-"


def yes_no(value: bool) -> str:
    return "true" if value else "false"


def print_auto_adapt_result(db_path: str) -> None:
    applied, reason = auto_adapt_if_enabled(db_path=db_path)
    if applied:
        print("auto_adapt: apply executado automaticamente.")
    elif reason == "infeasible":
        print("auto_adapt: configurado, mas nao aplicado por erro estrutural no dry-run.")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.handler(args)
    except ValueError as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
