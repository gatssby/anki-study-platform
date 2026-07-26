import json
import glob
import hashlib
import os
from pathlib import Path

BASE = Path("/home/ubuntu/anki-gpt-sync")
DATA_DIR = BASE / "data"
STATE_DIR = BASE / "state"

CURRENT_SNAPSHOT = STATE_DIR / "current_snapshot.json"
CURRENT_INDEX = STATE_DIR / "notes_index.json"
CURRENT_MEDIA_REFS = STATE_DIR / "media_refs.txt"
LAST_DIFF = STATE_DIR / "last_diff.json"

# WARNING: this is a legacy reconcile script. Its build_index() output omits
# fields such as kind/compare_text that the live query API expects for
# /cards/search and kind-based filters. Keep it opt-in so it cannot silently
# publish an incompatible notes_index.json by accident.
LEGACY_RECONCILE_ENV = "ANKI_GPT_SYNC_ALLOW_LEGACY_RECONCILE"

def latest_snapshot_path():
    # Keep the same "latest snapshot" rule used by rebuild_state.sh:
    # timestamped filename order, not filesystem mtime.
    files = sorted(DATA_DIR.glob("*.json"))
    if not files:
        raise SystemExit("Nenhum snapshot encontrado.")
    return files[-1]

def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def note_signature(note: dict) -> str:
    relevant = {
        "deck": note.get("deck"),
        "root_deck": note.get("root_deck"),
        "note_type": note.get("note_type"),
        "tags": sorted(note.get("tags", [])),
        "field_names": note.get("field_names", []),
        "fields": note.get("fields", {}),
        "has_images": note.get("has_images", False),
        "image_refs": sorted(note.get("image_refs", [])),
    }
    raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def build_index(snapshot: dict):
    notes = snapshot.get("notes", [])
    index = {}
    media_refs = []
    seen_media = set()

    for note in notes:
        nid = str(note["note_id"])
        index[nid] = {
            "note_id": note["note_id"],
            "deck": note.get("deck"),
            "root_deck": note.get("root_deck"),
            "note_type": note.get("note_type"),
            "tags": note.get("tags", []),
            "field_names": note.get("field_names", []),
            "fields": note.get("fields", {}),
            "has_images": note.get("has_images", False),
            "image_refs": note.get("image_refs", []),
            "signature": note_signature(note),
        }

        for ref in note.get("image_refs", []):
            if ref not in seen_media:
                seen_media.add(ref)
                media_refs.append(ref)

    return index, media_refs

def main():
    if os.environ.get(LEGACY_RECONCILE_ENV) != "1":
        raise SystemExit(
            "Legacy reconcile disabled by default because it writes a "
            "notes_index.json schema incompatible with the live query API. "
            f"Use scripts/rebuild_state.sh for normal publication. "
            f"Set {LEGACY_RECONCILE_ENV}=1 only if you intentionally need "
            "this legacy diff workflow."
        )

    latest = latest_snapshot_path()
    snapshot = load_json(latest)

    old_index = {}
    if CURRENT_INDEX.exists():
        old_index = load_json(CURRENT_INDEX)

    new_index, media_refs = build_index(snapshot)

    old_ids = set(old_index.keys())
    new_ids = set(new_index.keys())

    inserted = sorted(new_ids - old_ids, key=int)
    removed = sorted(old_ids - new_ids, key=int)

    changed = []
    unchanged = []

    for nid in sorted(new_ids & old_ids, key=int):
        if new_index[nid]["signature"] != old_index[nid]["signature"]:
            changed.append(nid)
        else:
            unchanged.append(nid)

    diff = {
        "snapshot_file": str(latest),
        "note_count": len(new_index),
        "inserted_count": len(inserted),
        "removed_count": len(removed),
        "changed_count": len(changed),
        "unchanged_count": len(unchanged),
        "inserted_note_ids": inserted,
        "removed_note_ids": removed,
        "changed_note_ids": changed,
    }

    save_json(CURRENT_SNAPSHOT, snapshot)
    save_json(CURRENT_INDEX, new_index)
    save_json(LAST_DIFF, diff)
    CURRENT_MEDIA_REFS.write_text("\n".join(media_refs), encoding="utf-8")

    print("snapshot_file =", latest)
    print("note_count =", len(new_index))
    print("inserted_count =", len(inserted))
    print("removed_count =", len(removed))
    print("changed_count =", len(changed))
    print("media_ref_count =", len(media_refs))
    print("saved =", CURRENT_SNAPSHOT)
    print("saved =", CURRENT_INDEX)
    print("saved =", CURRENT_MEDIA_REFS)
    print("saved =", LAST_DIFF)

if __name__ == "__main__":
    main()
