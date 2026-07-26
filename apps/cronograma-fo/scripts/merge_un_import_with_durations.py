from __future__ import annotations

import csv
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

UN_IMPORT_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "output" / "gpe_un_app_import.csv"
DURATIONS_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "output" / "un_video_durations.csv"
OUTPUT_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "output" / "gpe_un_app_import_with_durations.csv"


def load_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    un_rows = load_csv(UN_IMPORT_CSV)
    duration_rows = load_csv(DURATIONS_CSV)

    duration_map = {
        row["video_relative_path"].strip(): row.get("duration_seconds", "").strip()
        for row in duration_rows
        if row.get("video_relative_path")
    }

    output_rows = []
    matched = 0
    unmatched = 0

    for row in un_rows:
        rel = row["relative_path"].strip()
        duration_seconds = ""

        if rel.lower().endswith(".mp4"):
            duration_seconds = duration_map.get(rel, "")
            if duration_seconds:
                matched += 1
            else:
                unmatched += 1

        new_row = dict(row)
        new_row["duration_seconds"] = duration_seconds
        output_rows.append(new_row)

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = list(output_rows[0].keys()) if output_rows else []
    if "duration_seconds" not in fieldnames:
        fieldnames.append("duration_seconds")

    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)

    print(f"Arquivo gerado: {OUTPUT_CSV}")
    print(f"Total de linhas: {len(output_rows)}")
    print(f"Vídeos com duração: {matched}")
    print(f"Vídeos sem duração: {unmatched}")


if __name__ == "__main__":
    main()
