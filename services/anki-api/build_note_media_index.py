import json
import html
from html.parser import HTMLParser
from pathlib import Path

STATE_DIR = Path("/home/ubuntu/anki-gpt-sync/state")
INDEX_PATH = STATE_DIR / "notes_index.json"
MEDIA_DIR = STATE_DIR / "media"
OUT_PATH = STATE_DIR / "note_media_index.json"
SUMMARY_PATH = STATE_DIR / "note_media_summary.json"


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


class ImageSrcParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "img":
            return
        for name, value in attrs:
            if name and name.lower() == "src" and value:
                self.refs.append(html.unescape(value).strip())
                return


def extract_media_refs_from_html(value: str) -> list[str]:
    parser = ImageSrcParser()
    parser.feed(value)
    parser.close()
    return [ref for ref in parser.refs if ref]


def dedupe_refs(refs: list[str]) -> list[str]:
    seen = set()
    out = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def extract_media_refs_from_fields(fields: dict) -> list[str]:
    refs = []

    for value in (fields or {}).values():
        if not isinstance(value, str) or not value:
            continue

        for ref in extract_media_refs_from_html(value):
            ref = (ref or "").strip()
            if ref:
                refs.append(ref)

    return dedupe_refs(refs)


def normalize_media_refs(note: dict) -> list[str]:
    fields_refs = extract_media_refs_from_fields(note.get("fields", {}))
    if fields_refs:
        return fields_refs

    image_refs = note.get("image_refs")
    if isinstance(image_refs, list):
        refs = []
        for ref in image_refs:
            if isinstance(ref, str):
                ref = html.unescape(ref).strip()
                if ref:
                    refs.append(ref)
        if refs:
            return dedupe_refs(refs)

    return []


def iter_notes(notes_index):
    if isinstance(notes_index, list):
        for note in notes_index:
            if isinstance(note, dict):
                yield note
    elif isinstance(notes_index, dict):
        for k, v in notes_index.items():
            if isinstance(v, dict):
                note = dict(v)
                note.setdefault("note_id", k)
                yield note


def main():
    notes_index = load_json(INDEX_PATH)
    media_files_set = {p.name for p in MEDIA_DIR.iterdir() if p.is_file()}

    out = {}
    notes_total = 0
    notes_with_images = 0
    notes_with_all_media_resolved = 0
    notes_with_external_media = 0
    notes_with_broken_media = 0
    total_resolved_refs = 0
    total_external_refs = 0
    total_broken_refs = 0

    for note in iter_notes(notes_index):
        note_id = str(note.get("note_id"))
        media_refs = normalize_media_refs(note)

        resolved_media = []
        external_media = []
        broken_media = []

        for ref in media_refs:
            ref = (ref or "").strip()
            if not ref:
                continue

            if ref.startswith("http://") or ref.startswith("https://"):
                external_media.append(ref)
            elif ref.startswith("blob:"):
                broken_media.append(ref)
            elif ref in media_files_set:
                resolved_media.append(ref)
            else:
                broken_media.append(ref)

        has_images = bool(media_refs)

        out[note_id] = {
            "has_images": has_images,
            "resolved_media": resolved_media,
            "external_media": external_media,
            "broken_media": broken_media,
        }

        notes_total += 1

        if has_images:
            notes_with_images += 1

        if resolved_media and not external_media and not broken_media:
            notes_with_all_media_resolved += 1

        if external_media:
            notes_with_external_media += 1

        if broken_media:
            notes_with_broken_media += 1

        total_resolved_refs += len(resolved_media)
        total_external_refs += len(external_media)
        total_broken_refs += len(broken_media)

    summary = {
        "notes_total": notes_total,
        "notes_with_images": notes_with_images,
        "notes_with_all_media_resolved": notes_with_all_media_resolved,
        "notes_with_external_media": notes_with_external_media,
        "notes_with_broken_media": notes_with_broken_media,
        "total_resolved_refs": total_resolved_refs,
        "total_external_refs": total_external_refs,
        "total_broken_refs": total_broken_refs,
    }

    save_json(OUT_PATH, out)
    save_json(SUMMARY_PATH, summary)

    print(f"saved = {OUT_PATH}")
    print(f"saved = {SUMMARY_PATH}")
    print(f"notes_total = {notes_total}")
    print(f"notes_with_images = {notes_with_images}")
    print(f"notes_with_all_media_resolved = {notes_with_all_media_resolved}")
    print(f"notes_with_external_media = {notes_with_external_media}")
    print(f"notes_with_broken_media = {notes_with_broken_media}")
    print(f"total_resolved_refs = {total_resolved_refs}")
    print(f"total_external_refs = {total_external_refs}")
    print(f"total_broken_refs = {total_broken_refs}")


if __name__ == "__main__":
    main()
