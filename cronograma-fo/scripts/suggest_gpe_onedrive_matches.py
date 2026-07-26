from __future__ import annotations

import csv
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
GPE_BLOCKS_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "staging" / "gpe_blocks.csv"
ONEDRIVE_INDEX_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "staging" / "onedrive_folder_index.csv"

SUGGESTIONS_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "staging" / "gpe_onedrive_match_suggestions.csv"
AUTO_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "staging" / "gpe_match_auto.csv"
REVIEW_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "staging" / "gpe_match_review_only.csv"


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


def normalize_text(text: str) -> str:
    text = strip_accents(text).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_module_name(text: str) -> str:
    text = normalize_text(text)

    replacements = {
        "bases de numeracao": "bases numericas",
        "base de numeracao": "bases numericas",
        "bases numeracao": "bases numericas",
        "funcao": "funcoes",
        "funcao afim": "funcao afim",
        "funcao quadratica": "funcao quadratica",
        "funcao modular": "funcao modular",
        "funcao composta e inversa": "funcao composta e inversa",
        "intuicao e definicoes": "definicao",
        "intuicao e definicoes sobre funcoes": "definicao",
        "aritimetica": "aritmetica",
        "conjuntos numericos": "conjuntos numericos",
        "teoria de conjuntos": "teoria dos conjuntos",
        "fatoracao e manipulacoes algebricas": "fatoracao e manipulacao algebrica",
        "potenciacao e radiciacao": "potenciacao e radiciacao",
        "razao e proporcao": "proporcionalidade",
        "divisibilidade mdc e mmc": "divisbilidade mdc e mmc",
        "semelhanca e teorema de tales": "semelhanca de triangulo",
        "semelhanca e teorema de tales ": "semelhanca de triangulo",
        "triangulo retangulo": "triangulo retangulo",
        "arcos e angulos": "arcos e angulos",
        "ciclo trigonometrico": "ciclo trigonometrico",
        "trigonometria no triangulo retangulo": "trigonometria no triangulo retangulo",
        "introducao as equacoes trigonometricas": "introducao as equacoes trigonometricas",
        "arcos compostos": "arcos compostos",
        "inequacoes trigonometricas": "inequacoes trigonometricas",
        "funcoes trigonometricas": "funcoes trigonometricas",
        "angulos e conceitos iniciais": "angulos",
        "circunferencia": "circunferencia",
        "poligonos": "poligonos",
        "quadrilateros": "quadrilateros",
        "trigonometria em triangulos quaisquer": "trigonometria em triangulos quaisquer",
        "area de poligonos": "areas de poligonos",
        "geometria de posicao": "geometria de posicao",
        "principio fundamental da contagem": "principio fundamental da contagem",
        "fatorial e permutacoes": "fatorial e permutacoes",
        "arranjos e combinacoes": "arranjos e combinacoes",
        "teoria das probabilidades": "teoria das probabilidades",
        "probabilidade condicional": "probabilidade condicional",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def load_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def build_candidates(block: dict, folders: list[dict]) -> list[dict]:
    subject = block["subject"]
    module_name = block["module_name"]
    lesson_count = int(block["lesson_candidate_count"] or 0)

    module_norm = normalize_module_name(module_name)

    scored = []

    for folder in folders:
        if folder["subject"] != subject:
            continue

        parent_norm = normalize_module_name(folder["parent_folder"])
        top_norm = normalize_module_name(folder["top_folder"])
        folder_norm = normalize_module_name(folder["folder_path"])
        video_count = int(folder["video_count"] or 0)

        sim_parent = similarity(module_norm, parent_norm)
        sim_top = similarity(module_norm, top_norm)
        sim_folder = similarity(module_norm, folder_norm)

        count_score = 0.0
        if lesson_count > 0 and video_count > 0:
            ratio = min(lesson_count, video_count) / max(lesson_count, video_count)
            count_score = ratio

        score = (
            sim_parent * 0.62 +
            sim_top * 0.10 +
            sim_folder * 0.13 +
            count_score * 0.15
        )

        scored.append(
            {
                "subject": subject,
                "module_code": block["module_code"],
                "module_name": module_name,
                "start_page": block["start_page"],
                "lesson_candidate_count": lesson_count,
                "folder_path": folder["folder_path"],
                "parent_folder": folder["parent_folder"],
                "top_folder": folder["top_folder"],
                "video_count": video_count,
                "sim_parent": round(sim_parent, 4),
                "sim_top": round(sim_top, 4),
                "sim_folder": round(sim_folder, 4),
                "count_score": round(count_score, 4),
                "score": round(score, 4),
            }
        )

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:5]


def should_auto_ok(candidates: list[dict]) -> bool:
    if not candidates:
        return False

    best = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None

    if best["score"] >= 0.84 and best["sim_parent"] >= 0.80:
        return True

    if best["score"] >= 0.80 and best["sim_parent"] >= 0.88:
        return True

    if best["score"] >= 0.77 and best["sim_parent"] >= 0.93:
        return True

    if second is not None:
        gap = best["score"] - second["score"]
        if best["score"] >= 0.78 and gap >= 0.14 and best["sim_parent"] >= 0.76:
            return True
        if best["score"] >= 0.75 and gap >= 0.20 and best["sim_parent"] >= 0.72:
            return True

    return False


def main() -> None:
    gpe_blocks = load_csv(GPE_BLOCKS_CSV)
    onedrive_folders = load_csv(ONEDRIVE_INDEX_CSV)

    suggestion_rows = []
    auto_rows = []
    review_rows = []

    for block in gpe_blocks:
        candidates = build_candidates(block, onedrive_folders)

        for rank, cand in enumerate(candidates, start=1):
            suggestion_rows.append(
                {
                    "subject": cand["subject"],
                    "module_code": cand["module_code"],
                    "module_name": cand["module_name"],
                    "start_page": cand["start_page"],
                    "lesson_candidate_count": cand["lesson_candidate_count"],
                    "candidate_rank": rank,
                    "score": cand["score"],
                    "sim_parent": cand["sim_parent"],
                    "sim_top": cand["sim_top"],
                    "count_score": cand["count_score"],
                    "top_folder": cand["top_folder"],
                    "parent_folder": cand["parent_folder"],
                    "video_count": cand["video_count"],
                    "folder_path": cand["folder_path"],
                }
            )

        if not candidates:
            review_rows.append(
                {
                    "subject": block["subject"],
                    "module_code": block["module_code"],
                    "module_name": block["module_name"],
                    "start_page": block["start_page"],
                    "lesson_candidate_count": block["lesson_candidate_count"],
                    "suggested_folder_path": "",
                    "suggested_parent_folder": "",
                    "suggested_video_count": "",
                    "status": "review",
                    "notes": "sem candidatos",
                }
            )
            continue

        best = candidates[0]

        if should_auto_ok(candidates):
            auto_rows.append(
                {
                    "subject": best["subject"],
                    "module_code": best["module_code"],
                    "module_name": best["module_name"],
                    "start_page": best["start_page"],
                    "lesson_candidate_count": best["lesson_candidate_count"],
                    "confirmed_folder_path": best["folder_path"],
                    "confirmed_parent_folder": best["parent_folder"],
                    "video_count": best["video_count"],
                    "status": "auto_ok",
                    "notes": "",
                }
            )
        else:
            review_rows.append(
                {
                    "subject": best["subject"],
                    "module_code": best["module_code"],
                    "module_name": best["module_name"],
                    "start_page": best["start_page"],
                    "lesson_candidate_count": best["lesson_candidate_count"],
                    "suggested_folder_path": best["folder_path"],
                    "suggested_parent_folder": best["parent_folder"],
                    "suggested_video_count": best["video_count"],
                    "status": "review",
                    "notes": "",
                }
            )

    with SUGGESTIONS_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "subject",
                "module_code",
                "module_name",
                "start_page",
                "lesson_candidate_count",
                "candidate_rank",
                "score",
                "sim_parent",
                "sim_top",
                "count_score",
                "top_folder",
                "parent_folder",
                "video_count",
                "folder_path",
            ],
        )
        writer.writeheader()
        writer.writerows(suggestion_rows)

    with AUTO_CSV.open("w", encoding="utf-8", newline="") as f:
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
        writer.writerows(auto_rows)

    with REVIEW_CSV.open("w", encoding="utf-8", newline="") as f:
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
        writer.writerows(review_rows)

    print(f"Sugestões totais: {len(suggestion_rows)}")
    print(f"Auto aprovados: {len(auto_rows)}")
    print(f"Para revisão: {len(review_rows)}")
    print(AUTO_CSV)
    print(REVIEW_CSV)


if __name__ == "__main__":
    main()
