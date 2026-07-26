from __future__ import annotations

import csv
import json
import re
import unicodedata
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_JSON = PROJECT_ROOT / "work" / "universo_narrado" / "raw" / "onedrive_tree.json"
OUTPUT_CSV = PROJECT_ROOT / "work" / "universo_narrado" / "staging" / "universo_narrado_map.csv"


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_label(name: str) -> str:
    name = name.strip().rstrip("/")
    return normalize_spaces(name)


def infer_subject(path_str: str) -> str:
    lowered = strip_accents(path_str).lower()
    if "licoes de fisica" in lowered:
        return "FIS"
    if "licoes de matematica" in lowered:
        return "MAT"
    return "UNK"


def infer_topic_and_subtopic(parts: list[str], subject: str) -> tuple[str, str]:
    if subject == "FIS":
        topic = clean_label(parts[1]) if len(parts) > 1 else ""
        subtopic = clean_label(parts[2]) if len(parts) > 2 else ""
        return topic, subtopic

    if subject == "MAT":
        topic = clean_label(parts[1]) if len(parts) > 1 else ""
        subtopic = clean_label(parts[2]) if len(parts) > 2 else ""
        return topic, subtopic

    topic = clean_label(parts[1]) if len(parts) > 1 else ""
    subtopic = clean_label(parts[2]) if len(parts) > 2 else ""
    return topic, subtopic


def infer_order(filename: str) -> int:
    stem = Path(filename).stem.strip()

    patterns = [
        r"^A?(\d+)\b",
        r"^(\d+)\s*[-.]",
        r"^(\d+)\b",
    ]

    for pattern in patterns:
        match = re.match(pattern, stem, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))

    return 999999


def clean_lesson_title(filename: str) -> str:
    stem = Path(filename).stem.strip()

    patterns = [
        r"^A?\d+\s*-\s*",
        r"^\d+\s*-\s*",
        r"^\d+\.\s*",
    ]

    title = stem
    for pattern in patterns:
        title = re.sub(pattern, "", title, count=1, flags=re.IGNORECASE)

    return normalize_spaces(title)


def is_video_lesson(relative_path: str) -> bool:
    path = Path(relative_path)
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return False

    parts = relative_path.split("/")
    normalized_parts = [strip_accents(p).lower() for p in parts]

    if "aulas" not in normalized_parts:
        return False

    if "material de apoio" in normalized_parts:
        return False

    return True


def load_items() -> list[dict]:
    with RAW_JSON.open("r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    items = load_items()
    rows: list[dict] = []

    for item in items:
        if item.get("IsDir"):
            continue

        relative_path = item.get("Path", "")
        if not relative_path or not is_video_lesson(relative_path):
            continue

        parts = relative_path.split("/")
        subject = infer_subject(relative_path)
        topic, subtopic = infer_topic_and_subtopic(parts, subject)
        filename = parts[-1]

        rows.append(
            {
                "source": "un",
                "subject": subject,
                "topic": topic,
                "subtopic": subtopic,
                "lesson_title": clean_lesson_title(filename),
                "lesson_order": infer_order(filename),
                "relative_path": relative_path,
                "is_active": 1,
                "notes": "",
            }
        )

    rows.sort(
        key=lambda r: (
            r["subject"],
            strip_accents(r["topic"]).lower(),
            strip_accents(r["subtopic"]).lower(),
            r["lesson_order"],
            strip_accents(r["lesson_title"]).lower(),
        )
    )

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "source",
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

    print(f"CSV gerado com {len(rows)} videoaulas:")
    print(OUTPUT_CSV)


if __name__ == "__main__":
    main()
