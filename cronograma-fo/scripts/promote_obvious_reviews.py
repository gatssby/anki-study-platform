from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUT_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "staging" / "gpe_match_review_only.csv"
PROMOTED_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "staging" / "gpe_match_promoted_auto.csv"
FINAL_REVIEW_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "staging" / "gpe_match_final_review_only.csv"


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


def normalize(text: str) -> str:
    text = strip_accents(text).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def looks_obviously_right(module_name: str, folder_path: str) -> bool:
    m = normalize(module_name)
    f = normalize(folder_path)

    direct_pairs = [
        ("divisibilidade mdc e mmc", "divisbilidade mdc e mmc"),
        ("teoria de conjuntos", "teoria dos conjuntos"),
        ("funcao afim", "funcao afim"),
        ("arcos e angulos", "arcos e angulos"),
        ("angulos e conceitos iniciais", "angulos"),
        ("triangulo retangulo", "triangulo retangulo"),
        ("circunferencia", "circunferencia"),
        ("triangulos e pontos notaveis", "triangulos e pontos notaveis"),
        ("poligonos", "poligonos"),
        ("quadrilateros", "quadrilateros"),
        ("trigonometria em triangulos quaisquer", "trigonometria em triangulos quaisquer"),
        ("area de poligonos", "areas de poligonos"),
        ("area de circulo", "areas de circulos"),
        ("porcentagem", "porcentagem"),
        ("juros", "juros"),
        ("estatistica", "estatistica"),
        ("prismas", "prismas"),
        ("piramides", "piramides"),
        ("cilindros", "cilindros"),
        ("cones", "cones"),
        ("esferas", "esferas"),
    ]

    for a, b in direct_pairs:
        if a in m and b in f:
            return True

    return False


def is_clearly_wrong(module_name: str, folder_path: str) -> bool:
    m = normalize(module_name)
    f = normalize(folder_path)

    wrong_pairs = [
        ("bases de numeracao", "lugares geometricos"),
        ("intuicao e definicoes", "inscricao de solidos"),
        ("progressao aritmetica", "aritmetica modular"),
        ("progressao geometrica", "lugares geometricos"),
    ]

    for a, b in wrong_pairs:
        if a in m and b in f:
            return True

    return False


def main() -> None:
    promoted = []
    still_review = []

    with INPUT_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            module_name = row["module_name"]
            folder_path = row["suggested_folder_path"]

            if is_clearly_wrong(module_name, folder_path):
                still_review.append(row)
                continue

            if looks_obviously_right(module_name, folder_path):
                promoted.append(
                    {
                        "subject": row["subject"],
                        "module_code": row["module_code"],
                        "module_name": row["module_name"],
                        "start_page": row["start_page"],
                        "lesson_candidate_count": row["lesson_candidate_count"],
                        "confirmed_folder_path": row["suggested_folder_path"],
                        "confirmed_parent_folder": row["suggested_parent_folder"],
                        "video_count": row["suggested_video_count"],
                        "status": "promoted_auto",
                        "notes": "",
                    }
                )
            else:
                still_review.append(row)

    with PROMOTED_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "subject",
                "module_code",
                "module_name",
                "start_page",
                "lesson_candidate_count",
                "confirmed_folder_path",
                "confirmed_parent_folder",
                "video_count",
                "status",
                "notes",
            ],
        )
        writer.writeheader()
        writer.writerows(promoted)

    with FINAL_REVIEW_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "subject",
                "module_code",
                "module_name",
                "start_page",
                "lesson_candidate_count",
                "suggested_folder_path",
                "suggested_parent_folder",
                "suggested_video_count",
                "status",
                "notes",
            ],
        )
        writer.writeheader()
        writer.writerows(still_review)

    print(f"Promovidos automaticamente: {len(promoted)}")
    print(f"Ainda para revisão manual: {len(still_review)}")
    print(PROMOTED_CSV)
    print(FINAL_REVIEW_CSV)


if __name__ == "__main__":
    main()
