from __future__ import annotations

import csv
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_JSON = PROJECT_ROOT / "work" / "gpe_bridge" / "raw" / "onedrive_tree.json"
OUTPUT_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "staging" / "onedrive_folder_index.csv"

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_label(name: str) -> str:
    return normalize_spaces(name.strip().rstrip("/"))


def infer_subject(path_str: str) -> str:
    lowered = strip_accents(path_str).lower()
    if "licoes de fisica" in lowered:
        return "FIS"
    if "licoes de matematica" in lowered:
        return "MAT"
    return "UNK"


def infer_order(name: str) -> int:
    stem = Path(name).stem.strip()
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


def load_items() -> list[dict]:
    with RAW_JSON.open("r", encoding="utf-8") as f:
        return json.load(f)


def is_video_file(path_str: str) -> bool:
    suffix = Path(path_str).suffix.lower()
    return suffix in VIDEO_EXTENSIONS


def main() -> None:
    items = load_items()

    folders: dict[str, dict] = {}
    folder_videos: defaultdict[str, list[dict]] = defaultdict(list)

    for item in items:
        if item.get("IsDir"):
            continue

        relative_path = item.get("Path", "")
        if not relative_path or not is_video_file(relative_path):
            continue

        parts = relative_path.split("/")
        normalized_parts = [strip_accents(p).lower() for p in parts]

        if "aulas" not in normalized_parts:
            continue
        if "material de apoio" in normalized_parts:
            continue

        aulas_idx = normalized_parts.index("aulas")
        folder_path = "/".join(parts[: aulas_idx + 1])
        parent_folder = parts[aulas_idx - 1] if aulas_idx - 1 >= 0 else ""
        top_folder = parts[1] if len(parts) > 1 else ""
        subject = infer_subject(relative_path)

        if folder_path not in folders:
            folders[folder_path] = {
                "subject": subject,
                "top_folder": clean_label(top_folder),
                "parent_folder": clean_label(parent_folder),
                "folder_path": folder_path,
            }

        folder_videos[folder_path].append(
            {
                "filename": parts[-1],
                "lesson_order": infer_order(parts[-1]),
                "relative_path": relative_path,
            }
        )

    rows = []
    for folder_path, meta in folders.items():
        videos = sorted(
            folder_videos[folder_path],
            key=lambda v: (
                v["lesson_order"],
                strip_accents(v["filename"]).lower(),
            ),
        )

        video_names = [v["filename"] for v in videos]
        first_video = video_names[0] if video_names else ""
        last_video = video_names[-1] if video_names else ""

        rows.append(
            {
                "subject": meta["subject"],
                "top_folder": meta["top_folder"],
                "parent_folder": meta["parent_folder"],
                "folder_path": meta["folder_path"],
                "video_count": len(videos),
                "first_video": first_video,
                "last_video": last_video,
                "video_names_joined": " || ".join(video_names),
            }
        )

    rows.sort(
        key=lambda r: (
            r["subject"],
            strip_accents(r["top_folder"]).lower(),
            strip_accents(r["parent_folder"]).lower(),
            strip_accents(r["folder_path"]).lower(),
        )
    )

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "subject",
                "top_folder",
                "parent_folder",
                "folder_path",
                "video_count",
                "first_video",
                "last_video",
                "video_names_joined",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"CSV gerado com {len(rows)} pastas de aulas:")
    print(OUTPUT_CSV)


if __name__ == "__main__":
    main()
