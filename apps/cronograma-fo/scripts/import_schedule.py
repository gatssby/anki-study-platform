#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import DEFAULT_DB_PATH, connect_db, init_db
from app.importer import (
    build_empty_fo_import_preflight,
    format_fo_import_preflight,
    import_fo_lessons,
    import_universe_narrado_csv,
    parse_sheet,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Importa trilhas do cronograma para o banco SQLite.",
    )
    parser.add_argument(
        "--source",
        choices=["fo", "un"],
        default="fo",
        help="Fonte a importar: fo (Federal Online) ou un (Universo Narrado)",
    )
    parser.add_argument(
        "--xlsx",
        default="Cronogramas Extensivo UFPR 2026 - Federal Online.xlsx",
        help="Caminho do arquivo .xlsx",
    )
    parser.add_argument(
        "--sheet",
        default="02mar (30S)",
        help="Nome da aba para importar",
    )
    parser.add_argument(
        "--csv",
        default="work/gpe_bridge/output/gpe_un_app_import_with_durations.csv",
        help="Caminho do CSV oficial do Universo Narrado",
    )
    parser.add_argument(
        "--start-date",
        help="Data inicial opcional para distribuir o UN, em YYYY-MM-DD (padrao: 2026-03-13)",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="Caminho do banco SQLite",
    )
    parser.add_argument(
        "--replace-schedule-dates",
        action="store_true",
        help=(
            "FO apenas: substitui explicitamente recommended_date e os demais campos de agenda "
            "pelos valores da planilha. Por padrão, a agenda adaptativa existente é preservada."
        ),
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    db_path = Path(args.db).resolve()

    if args.source == "fo":
        xlsx_path = Path(args.xlsx).resolve()
        if not xlsx_path.exists():
            raise SystemExit(f"Arquivo não encontrado: {xlsx_path}")

        parsed = parse_sheet(workbook_path=xlsx_path, sheet_name=args.sheet)
        database_has_lessons = False
        if db_path.exists():
            with connect_db(db_path) as conn:
                database_has_lessons = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lessons'"
                ).fetchone() is not None

        if not database_has_lessons:
            empty_preflight = build_empty_fo_import_preflight(parsed)
            print(format_fo_import_preflight(empty_preflight))
            empty_preflight.require_safe()
            with connect_db(db_path) as conn:
                init_db(conn)
                result = import_fo_lessons(
                    conn=conn,
                    lessons=parsed,
                    replace_schedule_dates=args.replace_schedule_dates,
                )
        else:
            with connect_db(db_path) as conn:
                result = import_fo_lessons(
                    conn=conn,
                    lessons=parsed,
                    replace_schedule_dates=args.replace_schedule_dates,
                    preflight_reporter=lambda report: print(format_fo_import_preflight(report)),
                )
        count = result.processed_count
        print(
            "Resultado FO: "
            f"{result.updated_count} atualizados, "
            f"{result.inserted_count} novos, "
            f"{result.preserved_missing_count} ausentes preservados."
        )
        if args.replace_schedule_dates:
            print("Datas da planilha restauradas explicitamente; snapshots FO foram limpos.")
        else:
            print("Agenda adaptativa existente preservada.")
    else:
        if args.replace_schedule_dates:
            raise SystemExit("--replace-schedule-dates só pode ser usado com --source fo")
        with connect_db(db_path) as conn:
            init_db(conn)
            csv_path = Path(args.csv).resolve()
            if not csv_path.exists():
                raise SystemExit(f"Arquivo não encontrado: {csv_path}")
            start_date = date.fromisoformat(args.start_date) if args.start_date else None
            count = import_universe_narrado_csv(
                conn=conn,
                csv_path=csv_path,
                start_date=start_date,
            )

    print(f"Importação concluída ({args.source.upper()}): {count} itens processados.")
    print(f"Banco atualizado em: {db_path}")


if __name__ == "__main__":
    main()
