from __future__ import annotations

import csv
import json
import re
import unicodedata
from pathlib import PurePosixPath
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUT_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "staging" / "gpe_final_lesson_list.csv"
OUTPUT_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "output" / "gpe_un_app_import.csv"
RAW_TREE_JSON = PROJECT_ROOT / "work" / "gpe_bridge" / "raw" / "onedrive_tree.json"
ONEDRIVE_ROOT = Path("/Users/gatsby/Library/CloudStorage/OneDrive-Personal/Universo Narrado")


def load_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_raw_tree(path: Path = RAW_TREE_JSON) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def normalize_path_key(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text.strip()).replace("\\", "/")
    return re.sub(r"/+", "/", normalized).rstrip("/")


def infer_topic(folder_path: str) -> tuple[str, str]:
    parts = folder_path.split("/")

    # Esperado:
    # Lições de Matemática/12. Funções/02. Função afim/Aulas
    # Lições de Física/3. Frente A/A01 - 1. Introducao.../Aulas
    top_folder = parts[1] if len(parts) > 1 else ""
    parent_folder = parts[2] if len(parts) > 2 else ""

    return top_folder, parent_folder


def clean_topic_display(label: str) -> str:
    cleaned = label.strip()
    cleaned = re.sub(r"^[A-Z]+\d+\s*-\s*", "", cleaned)
    cleaned = re.sub(r"^\d+[\.\-]?\s*", "", cleaned)
    return cleaned.strip()


def is_list_pdf_name(filename: str) -> bool:
    name = filename.lower()
    if not name.endswith(".pdf"):
        return False
    if "teoria" in name:
        return False
    return "lista" in name or "exercicio" in name or "exercício" in name or "exercicios" in name or "exercícios" in name


def natural_sort_key(text: str) -> tuple:
    parts = re.split(r"(\d+)", unicodedata.normalize("NFC", text.lower()))
    return tuple(int(part) if part.isdigit() else part for part in parts)


def build_pdf_index(raw_tree_items: list[dict]) -> dict[str, list[str]]:
    pdfs_by_parent: dict[str, list[str]] = {}
    for item in raw_tree_items:
        if item.get("IsDir"):
            continue
        relative_path = normalize_path_key(item.get("Path", ""))
        if not relative_path:
            continue
        filename = PurePosixPath(relative_path).name
        if not is_list_pdf_name(filename):
            continue
        parent_key = normalize_path_key(str(PurePosixPath(relative_path).parent))
        pdfs_by_parent.setdefault(parent_key, []).append(relative_path)

    for paths in pdfs_by_parent.values():
        paths.sort(key=natural_sort_key)
    return pdfs_by_parent


def infer_list_relative_paths_from_tree(row: dict, pdfs_by_parent: dict[str, list[str]]) -> list[str]:
    folder_path = row["folder_path"].strip().replace("\\", "/").rstrip("/")
    module_dir = str(PurePosixPath(folder_path).parent)
    subject = row["subject"].strip().upper()

    candidate_parent_dirs = [(normalize_path_key(module_dir), module_dir)]
    if subject == "FIS":
        material_dir = f"{module_dir}/Material de Apoio"
        candidate_parent_dirs.insert(0, (normalize_path_key(material_dir), material_dir))

    paths: list[str] = []
    seen: set[str] = set()
    for parent_key, parent_path in candidate_parent_dirs:
        for relative_path in pdfs_by_parent.get(parent_key, []):
            filename = PurePosixPath(relative_path).name
            canonical_relative_path = f"{parent_path}/{filename}"
            if canonical_relative_path in seen:
                continue
            paths.append(canonical_relative_path)
            seen.add(canonical_relative_path)
    return paths


def infer_list_relative_paths_from_filesystem(row: dict) -> list[str]:
    relative_video_path = row["video_relative_path"].strip()
    full_video_path = ONEDRIVE_ROOT / relative_video_path
    aulas_dir = full_video_path.parent
    module_code = row["module_code"].strip()
    subject = row["subject"].strip().upper()

    if subject == "MAT":
        try:
            return [
                str(candidate.relative_to(ONEDRIVE_ROOT))
                for candidate in sorted(aulas_dir.parent.glob("*.pdf"), key=lambda p: natural_sort_key(p.name))
                if is_list_pdf_name(candidate.name)
            ]
        except OSError:
            return []

    if subject == "FIS":
        material_dir = aulas_dir.parent / "Material de Apoio"
        candidates = [
            material_dir / f"{module_code} - Exercicios.pdf",
            material_dir / f"{module_code} - Exercicio.pdf",
            material_dir / f"{module_code} - exercícios.pdf",
            material_dir / f"{module_code} - exercício.pdf",
            material_dir / f"{module_code} - Lista.pdf",
            material_dir / f"{module_code} - lista.pdf",
            material_dir / f"{module_code} - Lista Militar.pdf",
            material_dir / f"{module_code} - lista militar.pdf",
            material_dir / f"{module_code} - Lista de Exercicios.pdf",
            material_dir / f"{module_code} - Lista de exercícios.pdf",
            material_dir / f"{module_code} - lista de exercicios.pdf",
            material_dir / f"{module_code} - lista de exercícios.pdf",
        ]
        for candidate in candidates:
            name = candidate.name.lower()
            if "teoria" in name:
                continue
            if ("exercicio" in name or "exercícios" in name or "lista" in name) and candidate.exists():
                return [str(candidate.relative_to(ONEDRIVE_ROOT))]
        try:
            return [
                str(candidate.relative_to(ONEDRIVE_ROOT))
                for candidate in sorted(material_dir.glob("*.pdf"), key=lambda p: natural_sort_key(p.name))
                if is_list_pdf_name(candidate.name)
            ]
        except OSError:
            return []

    return []


def infer_list_relative_paths(row: dict, pdfs_by_parent: dict[str, list[str]]) -> list[str]:
    paths = infer_list_relative_paths_from_tree(row=row, pdfs_by_parent=pdfs_by_parent)
    if paths:
        return paths
    return infer_list_relative_paths_from_filesystem(row=row)


def list_title(display_topic: str, relative_path: str, total_lists: int) -> str:
    if total_lists <= 1:
        return f"{display_topic} • Lista"
    stem = PurePosixPath(relative_path).stem.strip()
    return f"{display_topic} • {clean_topic_display(stem)}"


def main() -> None:
    lessons = load_csv(INPUT_CSV)
    pdfs_by_parent = build_pdf_index(load_raw_tree())

    rows = []
    sequence_order = 1
    grouped_by_folder: dict[str, list[dict]] = {}
    for row in lessons:
        grouped_by_folder.setdefault(row["folder_path"], []).append(row)

    for folder_path, grouped_rows in grouped_by_folder.items():
        first_row = grouped_rows[0]
        topic, subtopic = infer_topic(folder_path)
        display_topic = clean_topic_display(subtopic if subtopic and subtopic != "Sem Tópico" else topic)

        for row in grouped_rows:
            lesson_title = Path(row["video_filename"]).stem.strip()

            rows.append(
                {
                    "source": "un_gpe",
                    "item_type": "lesson",
                    "sequence_order": sequence_order,
                    "subject": row["subject"],
                    "topic": topic,
                    "subtopic": subtopic,
                    "lesson_title": lesson_title,
                    "lesson_order": row["global_order"],
                    "relative_path": row["video_relative_path"],
                    "is_active": 1,
                    "notes": f'{row["module_code"]} | {row["module_name"]}',
                }
            )
            sequence_order += 1

        list_relative_paths = infer_list_relative_paths(first_row, pdfs_by_parent=pdfs_by_parent)
        for list_relative_path in list_relative_paths:
            rows.append(
                {
                    "source": "un_gpe",
                    "item_type": "list",
                    "sequence_order": sequence_order,
                    "subject": first_row["subject"],
                    "topic": topic,
                    "subtopic": subtopic,
                    "lesson_title": list_title(display_topic, list_relative_path, len(list_relative_paths)),
                    "lesson_order": "",
                    "relative_path": list_relative_path,
                    "is_active": 1,
                    "notes": f'{first_row["module_code"]} | {first_row["module_name"]}',
                }
            )
            sequence_order += 1

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "source",
                "item_type",
                "sequence_order",
                "subject",
                "topic",
                "subtopic",
                "lesson_title",
                "lesson_order",
                "relative_path",
                "is_active",
                "notes",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Arquivo de import do app gerado com {len(rows)} itens:")
    print(OUTPUT_CSV)


if __name__ == "__main__":
    main()
