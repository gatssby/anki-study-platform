#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "cronograma.db"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "gpe_un_migration" / "universo_narrado_gpe.csv"
DEFAULT_REPORT = PROJECT_ROOT / "output" / "gpe_un_migration" / "plan_migracao_un.json"
DEFAULT_HINTS = PROJECT_ROOT / "work" / "gpe_bridge" / "staging" / "gpe_final_matches.csv"
UN_SOURCE_SHEET = "universo_narrado_csv"
CSV_FIELDS = [
    "subject",
    "is_active",
    "topic",
    "subtopic",
    "lesson_title",
    "relative_path",
    "item_type",
    "lesson_order",
    "duration_seconds",
    "sequence_order",
]


@dataclass(frozen=True)
class GpeModule:
    subject: str
    order: int
    module_code: str
    module_name: str
    page: int | None
    pdf_name: str


@dataclass
class DbModule:
    subject: str
    module_path: str
    module_label: str
    lesson_count: int
    seen_count: int
    cut_count: int
    rows: list[sqlite3.Row]


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\x00", " ").replace("\uf0b7", " ").replace("•", " ")).strip()


def normalize_display(value: str | None) -> str:
    return unicodedata.normalize("NFC", (value or "").strip()).replace("\\", "/")


def normalize_key(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = text.replace("ﬁ", "fi").replace("ﬂ", "fl")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.lower().replace("\\", "/")
    text = re.sub(r"[^a-z0-9/]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def natural_sort_key(value: str | None) -> tuple[Any, ...]:
    text = normalize_key(value)
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text))


def token_set(value: str | None) -> set[str]:
    ignored = {"a", "as", "de", "da", "das", "do", "dos", "e", "em", "no", "na", "nos", "nas"}
    return {token for token in normalize_key(value).replace("/", " ").split() if token and token not in ignored}


def extract_pdf_pages(pdf_path: Path) -> list[tuple[int, str]]:
    if not pdf_path.exists():
        raise FileNotFoundError(f"GPE não encontrado: {pdf_path}")

    try:
        import fitz

        with fitz.open(str(pdf_path)) as doc:
            return [(index + 1, page.get_text() or "") for index, page in enumerate(doc)]
    except Exception:
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(pdf_path))
            return [(index, reader.pages[index - 1].extract_text() or "") for index in range(1, len(reader.pages) + 1)]
        except Exception as pypdf_error:
            raise RuntimeError(
                "Não foi possível extrair texto do PDF. Instale pypdf ou pymupdf para ler os GPEs."
            ) from pypdf_error


def extract_lines(text: str) -> list[str]:
    return [clean_text(line) for line in text.splitlines() if clean_text(line)]


MODULE_CODE_RE = re.compile(r"^[A-Z]{1,4}\d{2}$")
MODULE_CODE_TITLE_RE = re.compile(r"^([A-Z]{1,4}\d{2})\s*[-–—.]\s*(.+)$")
TITLE_MODULE_CODE_RE = re.compile(r"^(.+?)\s*[-–—]\s*([A-Z]{1,4}\d{2})$")

MODULE_CATEGORY_KEYS = {
    "algebra",
    "analise combinatoria",
    "aritimetica",
    "cinematica",
    "dinamica",
    "eletromagnetismo",
    "eletromagnetismo e fisica moderna",
    "eletrostatica",
    "frente a",
    "frente b",
    "frente c",
    "fundamentos",
    "funcoes",
    "geometria",
    "geometria analitica",
    "geometria espacial",
    "geometria plana",
    "matematica basica",
    "mecanica",
    "numeros complexos",
    "optica",
    "optica geometrica",
    "polinomios",
    "probabilidade",
    "progressoes",
    "teoria dos numeros",
    "termologia",
    "termodinamica",
    "trigonometria",
}

MONTH_KEYS = {
    "janeiro",
    "fevereiro",
    "marco",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
}

WEEKDAY_KEYS = {
    "segunda feira",
    "terca feira",
    "quarta feira",
    "quinta feira",
    "sexta feira",
    "sabado",
    "domingo",
}


def is_module_code(value: str) -> bool:
    return bool(MODULE_CODE_RE.fullmatch(value.strip().upper()))


def is_ignored_module_title_candidate(value: str) -> bool:
    key = normalize_key(value)
    if not key:
        return True
    if key in MODULE_CATEGORY_KEYS or key in MONTH_KEYS or key in WEEKDAY_KEYS:
        return True
    if re.fullmatch(r"\d{1,3}", key):
        return True
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", key):
        return True
    if "exercicio de fixacao" in key:
        return True
    return False


def extract_module_header_at(lines: list[str], index: int) -> tuple[str, str] | None:
    line = lines[index].strip()

    code_title = MODULE_CODE_TITLE_RE.match(line)
    if code_title:
        return code_title.group(1).upper(), clean_text(code_title.group(2))

    title_code = TITLE_MODULE_CODE_RE.match(line)
    if title_code:
        return title_code.group(2).upper(), clean_text(title_code.group(1))

    if not is_module_code(line):
        return None

    module_code = line.upper()
    candidate_window = lines[index + 1 : index + 9]
    for candidate in candidate_window:
        followup_code_title = MODULE_CODE_TITLE_RE.match(candidate)
        if followup_code_title and followup_code_title.group(1).upper() == module_code:
            return module_code, clean_text(followup_code_title.group(2))
        if is_module_code(candidate) or TITLE_MODULE_CODE_RE.match(candidate):
            break

    for candidate in candidate_window:
        if is_module_code(candidate) or TITLE_MODULE_CODE_RE.match(candidate):
            break
        if is_ignored_module_title_candidate(candidate):
            continue
        return module_code, candidate.strip()

    return None


def extract_gpe_modules(pdf_path: Path, subject: str) -> list[GpeModule]:
    modules: list[GpeModule] = []
    seen_codes: set[str] = set()
    for page_number, text in extract_pdf_pages(pdf_path):
        lines = extract_lines(text)
        for index in range(len(lines)):
            header = extract_module_header_at(lines=lines, index=index)
            if not header:
                continue
            module_code, module_name = header
            dedupe_key = f"{subject}:{module_code}"
            if dedupe_key in seen_codes:
                continue
            seen_codes.add(dedupe_key)
            modules.append(
                GpeModule(
                    subject=subject,
                    order=len(modules) + 1,
                    module_code=module_code,
                    module_name=module_name,
                    page=page_number,
                    pdf_name=pdf_path.name,
                )
            )
    return modules


def derive_module_path(relative_path: str | None) -> str:
    path = normalize_display(relative_path)
    for marker in ("/Aulas/", "/Material de Apoio/"):
        if marker in path:
            return path.split(marker, 1)[0].rstrip("/")
    if path.endswith("/Lista.pdf"):
        return path[: -len("/Lista.pdf")].rstrip("/")
    if "/" in path:
        return path.rsplit("/", 1)[0].rstrip("/")
    return path


def module_path_from_hint(value: str) -> str:
    path = normalize_display(value).rstrip("/")
    for marker in ("/Aulas", "/Material de Apoio"):
        if path.endswith(marker):
            return path[: -len(marker)].rstrip("/")
        marker_with_slash = f"{marker}/"
        if marker_with_slash in path:
            return path.split(marker_with_slash, 1)[0].rstrip("/")
    return derive_module_path(path)


def infer_topic_subtopic(module_path: str) -> tuple[str, str]:
    parts = [part for part in module_path.split("/") if part]
    topic = parts[1] if len(parts) > 1 else (parts[0] if parts else "")
    subtopic = parts[2] if len(parts) > 2 else "Sem Tópico"
    return topic, subtopic


def load_db_modules(db_path: Path) -> dict[str, list[DbModule]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM lessons
            WHERE source_sheet = ?
              AND track_code = 'UN'
              AND subject_prefix IN ('FIS', 'MAT')
            ORDER BY subject_prefix, relative_path, lesson_number, lesson_type
            """,
            (UN_SOURCE_SHEET,),
        ).fetchall()
    finally:
        conn.close()

    grouped: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        module_path = derive_module_path(row["relative_path"])
        if not module_path:
            continue
        grouped[(row["subject_prefix"], module_path)].append(row)

    modules_by_subject: dict[str, list[DbModule]] = {"FIS": [], "MAT": []}
    for (subject, module_path), module_rows in grouped.items():
        labels = [row["module_label"] for row in module_rows if row["module_label"]]
        module_label = Counter(labels).most_common(1)[0][0] if labels else module_path
        modules_by_subject.setdefault(subject, []).append(
            DbModule(
                subject=subject,
                module_path=module_path,
                module_label=module_label,
                lesson_count=len(module_rows),
                seen_count=sum(int(row["is_seen"] or 0) for row in module_rows),
                cut_count=sum(int(row["is_cut"] or 0) for row in module_rows),
                rows=module_rows,
            )
        )

    for modules in modules_by_subject.values():
        modules.sort(key=lambda module: natural_sort_key(module.module_path))
    return modules_by_subject


def load_mapping_hints(hints_path: Path, db_modules: dict[str, list[DbModule]]) -> dict[tuple[str, str], str]:
    if not hints_path.exists():
        return {}

    db_paths = {
        (module.subject, normalize_key(module.module_path)): module.module_path
        for modules in db_modules.values()
        for module in modules
    }
    hints: dict[tuple[str, str], str] = {}
    with hints_path.open("r", encoding="utf-8", newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        for row in reader:
            subject = (row.get("subject") or "").strip().upper()
            code = (row.get("module_code") or "").strip().upper()
            raw_path = row.get("confirmed_folder_path") or ""
            if subject not in {"FIS", "MAT"} or not code or not raw_path:
                continue
            module_path = module_path_from_hint(raw_path)
            canonical = db_paths.get((subject, normalize_key(module_path)))
            if canonical:
                hints[(subject, code)] = canonical
    return hints


def score_module(gpe_module: GpeModule, db_module: DbModule, hinted_path: str | None) -> int:
    if hinted_path and normalize_key(hinted_path) == normalize_key(db_module.module_path):
        return 220

    code = normalize_key(gpe_module.module_code).replace(" ", "")
    name = normalize_key(gpe_module.module_name)
    module_text = normalize_key(f"{db_module.module_path} {db_module.module_label}").replace("/", " ")
    compact_module_text = module_text.replace(" ", "")

    score = 0
    if code and re.search(rf"(^|[^a-z0-9]){re.escape(code)}([^a-z0-9]|$)", module_text):
        score += 120
    elif code and code in compact_module_text:
        score += 100

    if name:
        if name in module_text or module_text in name:
            score += 85
        gpe_tokens = token_set(name)
        db_tokens = token_set(module_text)
        if gpe_tokens:
            overlap = len(gpe_tokens & db_tokens) / len(gpe_tokens)
            score += round(overlap * 70)
            if overlap == 1:
                score += 15
    return score


def map_modules(
    gpe_modules: list[GpeModule],
    db_modules: dict[str, list[DbModule]],
    hints: dict[tuple[str, str], str],
) -> tuple[dict[GpeModule, DbModule], list[dict[str, Any]], list[dict[str, Any]]]:
    mapped: dict[GpeModule, DbModule] = {}
    not_found: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []

    for module in gpe_modules:
        hinted_path = hints.get((module.subject, module.module_code))
        scored = [
            (score_module(module, candidate, hinted_path), candidate)
            for candidate in db_modules.get(module.subject, [])
        ]
        scored = [(score, candidate) for score, candidate in scored if score > 0]
        scored.sort(key=lambda item: (-item[0], natural_sort_key(item[1].module_path)))

        if not scored or scored[0][0] < 45:
            not_found.append(module_to_report(module))
            continue

        best_score, best_candidate = scored[0]
        mapped[module] = best_candidate

        near_candidates = [
            {
                "score": score,
                "module_path": candidate.module_path,
                "module_label": candidate.module_label,
                "lesson_count": candidate.lesson_count,
            }
            for score, candidate in scored[:5]
            if score >= max(45, best_score - 15)
        ]
        if len(near_candidates) > 1:
            ambiguous.append(
                {
                    **module_to_report(module),
                    "selected_module_path": best_candidate.module_path,
                    "candidates": near_candidates,
                }
            )

    return mapped, not_found, ambiguous


def module_to_report(module: GpeModule) -> dict[str, Any]:
    return {
        "subject": module.subject,
        "order": module.order,
        "module_code": module.module_code,
        "module_name": module.module_name,
        "page": module.page,
        "pdf_name": module.pdf_name,
    }


def row_sort_key(row: sqlite3.Row) -> tuple[Any, ...]:
    lesson_number = row["lesson_number"]
    item_order = {"lesson": 0, "list": 1, "review": 2, "pending": 3}.get(row["lesson_type"], 9)
    return (
        lesson_number is None,
        lesson_number if lesson_number is not None else 999999,
        item_order,
        natural_sort_key(row["relative_path"]),
    )


def build_csv_rows(gpe_modules: list[GpeModule], mapped: dict[GpeModule, DbModule]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sequence_order = 1
    for module in gpe_modules:
        db_module = mapped.get(module)
        if not db_module:
            continue
        topic, subtopic = infer_topic_subtopic(db_module.module_path)
        for lesson in sorted(db_module.rows, key=row_sort_key):
            rows.append(
                {
                    "subject": module.subject,
                    "is_active": "1",
                    "topic": topic,
                    "subtopic": subtopic,
                    "lesson_title": lesson["title_raw"],
                    "relative_path": lesson["relative_path"] or "",
                    "item_type": lesson["lesson_type"],
                    "lesson_order": lesson["lesson_number"] if lesson["lesson_number"] is not None else "",
                    "duration_seconds": lesson["duration_seconds"] if lesson["duration_seconds"] is not None else "",
                    "sequence_order": sequence_order,
                }
            )
            sequence_order += 1
    return rows


def build_report(
    gpe_modules_by_subject: dict[str, list[GpeModule]],
    db_modules: dict[str, list[DbModule]],
    mapped: dict[GpeModule, DbModule],
    not_found: list[dict[str, Any]],
    ambiguous: list[dict[str, Any]],
    csv_rows: list[dict[str, Any]],
    db_path: Path,
    output_path: Path,
    hints: dict[tuple[str, str], str],
) -> dict[str, Any]:
    selected_paths_by_subject = defaultdict(set)
    for module, db_module in mapped.items():
        selected_paths_by_subject[module.subject].add(db_module.module_path)

    summary_by_subject: dict[str, Any] = {}
    for subject in ("FIS", "MAT"):
        modules = gpe_modules_by_subject.get(subject, [])
        found = [module for module in modules if module in mapped]
        selected_paths = selected_paths_by_subject[subject]
        outside_modules = [
            module
            for module in db_modules.get(subject, [])
            if module.module_path not in selected_paths
        ]
        included_rows = [
            row
            for row in csv_rows
            if row["subject"] == subject
        ]
        summary_by_subject[subject] = {
            "gpe_modules_total": len(modules),
            "gpe_modules_found": len(found),
            "gpe_modules_not_found": len([row for row in not_found if row["subject"] == subject]),
            "db_modules_total": len(db_modules.get(subject, [])),
            "db_modules_outside_gpe": len(outside_modules),
            "lessons_included": len(included_rows),
            "seen_lessons_included": sum(mapped[module].seen_count for module in found),
            "cut_lessons_included": sum(mapped[module].cut_count for module in found),
            "lessons_outside_active_gpe": sum(module.lesson_count for module in outside_modules),
        }

    found_report = []
    for module in gpe_modules_by_subject.get("FIS", []) + gpe_modules_by_subject.get("MAT", []):
        db_module = mapped.get(module)
        if not db_module:
            continue
        found_report.append(
            {
                **module_to_report(module),
                "module_path": db_module.module_path,
                "module_label": db_module.module_label,
                "lesson_count": db_module.lesson_count,
                "seen_count": db_module.seen_count,
                "cut_count": db_module.cut_count,
            }
        )

    outside_report = {}
    for subject in ("FIS", "MAT"):
        selected_paths = selected_paths_by_subject[subject]
        outside_report[subject] = [
            {
                "module_path": module.module_path,
                "module_label": module.module_label,
                "lesson_count": module.lesson_count,
                "seen_count": module.seen_count,
                "cut_count": module.cut_count,
            }
            for module in db_modules.get(subject, [])
            if module.module_path not in selected_paths
        ]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db_path": str(db_path),
        "csv_path": str(output_path),
        "resumo_por_materia": summary_by_subject,
        "modulos_gpe_encontrados": found_report,
        "modulos_gpe_nao_encontrados": not_found,
        "modulos_do_banco_fora_do_gpe": outside_report,
        "total_lessons_incluidas": len(csv_rows),
        "total_lessons_vistas_incluidas": sum(
            int(lesson["is_seen"] or 0)
            for module in mapped.values()
            for lesson in module.rows
        ),
        "total_lessons_cortadas_incluidas": sum(
            int(lesson["is_cut"] or 0)
            for module in mapped.values()
            for lesson in module.rows
        ),
        "total_lessons_un_existentes_fora_da_agenda": sum(
            subject_report["lessons_outside_active_gpe"]
            for subject_report in summary_by_subject.values()
        ),
        "alertas_de_mapeamento_ambiguo": ambiguous,
        "avisos": [
            "A tabela lessons não possui coluna explícita de ativo/inativo. O CSV marca is_active=1 apenas para itens incluídos, mas o importador atual descarta linhas inativas e não marca lessons antigas como inativas.",
            "Sem alteração de schema ou filtro novo na dashboard, consultas que usam apenas track_code='UN' continuam enxergando aulas UN fora do GPE como catálogo/histórico.",
            "O import UN foi ajustado para não apagar lessons fora do CSV; un_daily_assignments pode ser limpo porque é cache.",
        ],
        "mapping_hints": {
            "path": str(DEFAULT_HINTS),
            "loaded": len(hints),
        },
        "debug": {
            "gpe_modules_extracted": {
                subject: len(gpe_modules_by_subject.get(subject, []))
                for subject in ("FIS", "MAT")
            },
            "primeiros_20_modulos_extraidos": {
                subject: [
                    module_to_report(module)
                    for module in gpe_modules_by_subject.get(subject, [])[:20]
                ]
                for subject in ("FIS", "MAT")
            },
        },
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        json.dump(payload, file_obj, ensure_ascii=False, indent=2)
        file_obj.write("\n")


def backup_db(db_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = db_path.parent / "backups" / "manual"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"cronograma-before-un-gpe-{timestamp}.db"
    shutil.copy2(db_path, backup_path)
    return backup_path


def import_csv(db_path: Path, csv_path: Path) -> int:
    sys.path.insert(0, str(PROJECT_ROOT))
    from app.db import connect_db
    from app.importer import import_universe_narrado_csv

    conn = connect_db(db_path)
    try:
        return import_universe_narrado_csv(conn=conn, csv_path=csv_path)
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gera CSV seguro do Universo Narrado a partir dos novos GPEs.")
    parser.add_argument("--fisica-gpe", required=True, type=Path)
    parser.add_argument("--matematica-gpe", required=True, type=Path)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--dry-run", action="store_true", help="Gera CSV e relatório sem alterar o banco.")
    parser.add_argument("--apply", action="store_true", help="Gera CSV/relatório e faz backup, sem importar.")
    parser.add_argument("--import", dest="import_", action="store_true", help="Gera CSV/relatório, faz backup e importa no banco.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selected_modes = [args.dry_run, args.apply, args.import_]
    if sum(1 for value in selected_modes if value) > 1:
        print("Use apenas um modo: --dry-run, --apply ou --import.", file=sys.stderr)
        return 2
    if not any(selected_modes):
        args.dry_run = True

    db_modules = load_db_modules(args.db)
    gpe_modules_by_subject = {
        "FIS": extract_gpe_modules(args.fisica_gpe, "FIS"),
        "MAT": extract_gpe_modules(args.matematica_gpe, "MAT"),
    }
    gpe_modules = gpe_modules_by_subject["FIS"] + gpe_modules_by_subject["MAT"]
    hints = load_mapping_hints(DEFAULT_HINTS, db_modules)
    mapped, not_found, ambiguous = map_modules(gpe_modules, db_modules, hints)
    csv_rows = build_csv_rows(gpe_modules, mapped)
    report = build_report(
        gpe_modules_by_subject=gpe_modules_by_subject,
        db_modules=db_modules,
        mapped=mapped,
        not_found=not_found,
        ambiguous=ambiguous,
        csv_rows=csv_rows,
        db_path=args.db,
        output_path=args.output,
        hints=hints,
    )

    write_csv(args.output, csv_rows)
    write_json(args.report, report)

    backup_path = None
    if args.apply or args.import_:
        backup_path = backup_db(args.db)
        report["backup_path"] = str(backup_path)
        write_json(args.report, report)

    imported_count = None
    if args.import_:
        if not_found:
            print(
                f"Import abortado: {len(not_found)} módulo(s) do GPE não foram encontrados. Revise {args.report}.",
                file=sys.stderr,
            )
            return 1
        imported_count = import_csv(args.db, args.output)

    print(f"CSV gerado: {args.output} ({len(csv_rows)} linhas)")
    print(f"Relatório gerado: {args.report}")
    if backup_path:
        print(f"Backup criado: {backup_path}")
    if imported_count is not None:
        print(f"Importados/atualizados pelo importador UN: {imported_count}")
    elif args.dry_run:
        print("Dry-run concluído: banco não alterado.")
    elif args.apply:
        print("Apply concluído: artefatos e backup criados; banco não importado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
