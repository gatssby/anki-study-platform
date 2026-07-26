from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUT_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "staging" / "gpe_pdf_pages.csv"
OUTPUT_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "staging" / "gpe_blocks.csv"


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_line(line: str) -> str:
    return normalize_spaces(line.replace("\uf0b7", " ").replace("•", " ").strip())


def extract_lines(text: str) -> list[str]:
    return [clean_line(x) for x in text.splitlines() if clean_line(x)]


def extract_module_header(lines: list[str]) -> tuple[str, str] | None:
    # MAT: "Aritimética - CNJ02" com nome do módulo na linha anterior
    for i, line in enumerate(lines):
        m = re.match(r"^(.+?)\s*-\s*([A-Z]{1,4}\d{2})$", line)
        if m:
            module_code = m.group(2).strip()
            module_name = lines[i - 1].strip() if i > 0 else module_code
            return module_code, module_name

    # FIS: "A03. MRUV", "F01. Cinemática", etc.
    for line in lines:
        m = re.match(r"^([A-Z]{1,3}\d{2})\.\s+(.+)$", line)
        if m:
            module_code = m.group(1).strip()
            module_name = m.group(2).strip()
            return module_code, module_name

    return None


def is_stop_page(lines: list[str]) -> bool:
    joined = " ".join(strip_accents(x).lower() for x in lines)
    stop_tokens = [
        "lista de exercicios",
        "lista de exercicio",
        "embasamento",
        "parabens",
        "mais uma etapa concluida",
        "ao finalizar esse topico",
        "chegou a hora de fazer a lista",
        "simulado",
        "revisao geral",
        "diagrama 3np",
    ]
    return any(token in joined for token in stop_tokens)


def is_noise(line: str) -> bool:
    s = strip_accents(line).lower()

    if not s:
        return True

    noise_tokens = [
        "segunda-feira", "terca-feira", "quarta-feira", "quinta-feira",
        "sexta-feira", "sabado", "domingo",
        "janeiro", "fevereiro", "marco", "abril", "maio", "junho",
        "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
        "salve salve",
        "material teorico",
        "material didatido",
        "material didatico",
        "exercicios personalizados",
        "clique ou escaneie o qrcode",
        "clique aqui",
        "tempo estimado",
        "instagram",
        "telegram",
        "qrcode",
        "material didatico",
        "embasamento",
        "parabens",
        "guia personalizado de estudos para",
        "medicina",
        "pagina ",
        "voce pode acessar todas as listas",
        "os exercicios listados abaixo",
        "marque quando acertar",
        "marque quando errar",
        "eita, agora a gente subiu a regua",
        "esse tal de galileu",
        "fez sentido pra voce",
    ]

    if any(token in s for token in noise_tokens):
        return True

    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", s):
        return True

    if re.fullmatch(r"\d{1,3}", s):
        return True

    if line.startswith("'") and line.endswith("'"):
        return True

    return False


def is_nonvideo_item(line: str) -> bool:
    s = strip_accents(line).lower()

    blocked = [
        "exercicio de fixacao",
        "lista de exercicios",
        "lista de exercicio",
        "revisao",
        "simulado",
        "questao",
        "resolucao",
        "diagrama 3np",
    ]
    return any(token in s for token in blocked)


def collect_lessons(lines: list[str]) -> list[str]:
    lessons = []
    for line in lines:
        if is_noise(line):
            continue
        if is_nonvideo_item(line):
            continue
        # evita recontar cabeçalho de módulo como aula
        if re.match(r"^[A-Z]{1,3}\d{2}\.\s+.+$", line):
            continue
        lessons.append(line)

    seen = set()
    result = []
    for lesson in lessons:
        key = strip_accents(lesson).lower()
        if key not in seen:
            seen.add(key)
            result.append(lesson)
    return result


def main() -> None:
    pages = []

    with INPUT_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pages.append(
                {
                    "subject": row["subject"],
                    "pdf_name": row["pdf_name"],
                    "page": int(row["page"]),
                    "lines": extract_lines(row["text"]),
                }
            )

    rows = []
    current = None

    def flush_current():
        nonlocal current
        if not current:
            return
        lesson_candidates = collect_lessons(current["lesson_lines"])
        if lesson_candidates:
            rows.append(
                {
                    "subject": current["subject"],
                    "pdf_name": current["pdf_name"],
                    "start_page": current["start_page"],
                    "module_code": current["module_code"],
                    "module_name": current["module_name"],
                    "lesson_candidates_joined": " || ".join(lesson_candidates),
                    "lesson_candidate_count": len(lesson_candidates),
                }
            )
        current = None

    for page in pages:
        header = extract_module_header(page["lines"])

        if header:
            flush_current()
            module_code, module_name = header
            current = {
                "subject": page["subject"],
                "pdf_name": page["pdf_name"],
                "start_page": page["page"],
                "module_code": module_code,
                "module_name": module_name,
                "lesson_lines": [],
            }

            # para Física, as aulas já podem começar na mesma página do cabeçalho
            current["lesson_lines"].extend(page["lines"])
            continue

        if current is None:
            continue

        if is_stop_page(page["lines"]):
            flush_current()
            continue

        current["lesson_lines"].extend(page["lines"])

    flush_current()

    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "subject",
                "pdf_name",
                "start_page",
                "module_code",
                "module_name",
                "lesson_candidates_joined",
                "lesson_candidate_count",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"CSV gerado com {len(rows)} blocos candidatos:")
    print(OUTPUT_CSV)


if __name__ == "__main__":
    main()
