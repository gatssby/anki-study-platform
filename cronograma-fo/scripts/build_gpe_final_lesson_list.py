from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE = PROJECT_ROOT / "work" / "gpe_bridge" / "staging"

FINAL_MATCHES_CSV = BASE / "gpe_final_matches.csv"
ONEDRIVE_FOLDER_INDEX_CSV = BASE / "onedrive_folder_index.csv"
OUTPUT_CSV = BASE / "gpe_final_lesson_list.csv"


def load_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def normalize_path(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.lower().strip()
    text = text.replace("\\", "/")
    text = re.sub(r"\s+", " ", text)
    return text


def main() -> None:
    final_matches = load_csv(FINAL_MATCHES_CSV)
    folder_index = load_csv(ONEDRIVE_FOLDER_INDEX_CSV)

    folder_map_raw = {
        row["folder_path"].strip(): row
        for row in folder_index
    }

    folder_map_norm = {
        normalize_path(row["folder_path"].strip()): row
        for row in folder_index
    }

    rows = []
    global_order = 1
    missing = []

    for module_order, match in enumerate(final_matches, start=1):
        folder_path = match["confirmed_folder_path"].strip()

        folder_row = folder_map_raw.get(folder_path)
        if folder_row is None:
            folder_row = folder_map_norm.get(normalize_path(folder_path))

        if folder_row is None:
            print(f"[WARN] Pasta não encontrada no índice: {folder_path}")
            missing.append(folder_path)
            continue

        video_names = [
            v.strip()
            for v in folder_row["video_names_joined"].split(" || ")
            if v.strip()
        ]

        for lesson_order_in_module, video_name in enumerate(video_names, start=1):
            video_relative_path = f'{folder_row["folder_path"].strip()}/{video_name}'

            rows.append(
                {
                    "subject": match["subject"],
                    "module_code": match["module_code"],
                    "module_name": match["module_name"],
                    "module_order": module_order,
                    "lesson_order_in_module": lesson_order_in_module,
                    "global_order": global_order,
                    "folder_path": folder_row["folder_path"].strip(),
                    "video_filename": video_name,
                    "video_relative_path": video_relative_path,
                    "source_start_page": match["start_page"],
                    "match_status": match["status"],
                }
            )
            global_order += 1

    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "subject",
                "module_code",
                "module_name",
                "module_order",
                "lesson_order_in_module",
                "global_order",
                "folder_path",
                "video_filename",
                "video_relative_path",
                "source_start_page",
                "match_status",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Arquivo final de aulas gerado com {len(rows)} vídeos:")
    print(OUTPUT_CSV)
    print(f"Pastas faltantes: {len(missing)}")


if __name__ == "__main__":
    main()
