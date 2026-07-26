from __future__ import annotations

import csv
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE = PROJECT_ROOT / "work" / "gpe_bridge" / "staging"

AUTO_CSV = BASE / "gpe_match_auto.csv"
PROMOTED_CSV = BASE / "gpe_match_promoted_auto.csv"
FINAL_REVIEW_CSV = BASE / "gpe_match_final_review_only.csv"

OUTPUT_CSV = BASE / "gpe_final_matches.csv"


def load_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def safe_int(value: str) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return 999999


def main() -> None:
    final_rows = []
    seen = set()

    def add_rows(rows: list[dict], source_tag: str) -> None:
        nonlocal final_rows, seen

        for row in rows:
            module_code = row.get("module_code", "").strip()
            subject = row.get("subject", "").strip()
            key = (subject, module_code)

            if key in seen:
                continue

            confirmed_folder_path = row.get("confirmed_folder_path", "").strip()

            if not confirmed_folder_path:
                continue

            status = row.get("status", "").strip() or source_tag

            final_rows.append(
                {
                    "subject": subject,
                    "module_code": module_code,
                    "module_name": row.get("module_name", "").strip(),
                    "start_page": row.get("start_page", "").strip(),
                    "lesson_candidate_count": row.get("lesson_candidate_count", "").strip(),
                    "confirmed_folder_path": confirmed_folder_path,
                    "status": status,
                    "notes": row.get("notes", "").strip(),
                }
            )
            seen.add(key)

    auto_rows = load_csv(AUTO_CSV)
    promoted_rows = load_csv(PROMOTED_CSV)
    review_rows = load_csv(FINAL_REVIEW_CSV)

    add_rows(auto_rows, "auto_ok")
    add_rows(promoted_rows, "promoted_auto")
    add_rows(review_rows, "manual")

    final_rows.sort(key=lambda r: (r["subject"], safe_int(r["start_page"]), r["module_code"]))

    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "subject",
                "module_code",
                "module_name",
                "start_page",
                "lesson_candidate_count",
                "confirmed_folder_path",
                "status",
                "notes",
            ],
        )
        writer.writeheader()
        writer.writerows(final_rows)

    print(f"Arquivo final gerado com {len(final_rows)} matches:")
    print(OUTPUT_CSV)


if __name__ == "__main__":
    main()
