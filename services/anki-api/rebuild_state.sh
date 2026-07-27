#!/usr/bin/env bash
set -euo pipefail

BASE=/home/ubuntu/anki-gpt-sync
DATA=$BASE/data
STATE=$BASE/state
SCRIPTS=$BASE/scripts
LOCK_FILE=$BASE/rebuild_state.lock

exec 9>"$LOCK_FILE"
flock 9

echo "[0/6] Publish latest snapshot to state/notes_index.json..."
# Filenames are timestamped as YYYY-MM-DD_HH-MM-SS.json, so lexicographic order
# is a stable and explicit definition of "latest snapshot" across scripts.
LATEST_JSON="$(
python3 - <<PY
from pathlib import Path

data_dir = Path("$DATA")
files = sorted(p for p in data_dir.glob("*.json") if p.is_file())
if not files:
    raise SystemExit("Nenhum snapshot encontrado em data/.")
print(files[-1])
PY
)"
python3 - <<PY
import json
from pathlib import Path

latest = Path("$LATEST_JSON")
state_path = Path("$STATE/notes_index.json")
decks_path = Path("$STATE/decks_index.json")
status_path = Path("$STATE/snapshot_status.json")

with latest.open("r", encoding="utf-8") as f:
    payload = json.load(f)

notes = payload.get("notes", [])
by_id = {str(n["note_id"]): n for n in notes if isinstance(n, dict) and "note_id" in n}
decks = payload.get("decks", [])
if not isinstance(decks, list):
    decks = []

total_cards = payload.get("total_cards")
if not isinstance(total_cards, int):
    total_cards = sum(len(n.get("cards", [])) or 1 for n in by_id.values())

total_notes = payload.get("total_notes")
if not isinstance(total_notes, int):
    total_notes = len(by_id)

total_decks = payload.get("total_decks")
if not isinstance(total_decks, int):
    total_decks = len(decks) if decks else len({n.get("deck", "") for n in by_id.values() if n.get("deck")})

generated_at = payload.get("generated_at") or payload.get("timestamp")

state_path.write_text(
    json.dumps(by_id, ensure_ascii=False, indent=2),
    encoding="utf-8"
)

decks_path.write_text(
    json.dumps({
        "generated_at": generated_at,
        "profile": payload.get("profile"),
        "snapshot_version": payload.get("snapshot_version"),
        "total_decks": total_decks,
        "total_cards": total_cards,
        "total_notes": total_notes,
        "decks": decks,
    }, ensure_ascii=False, indent=2),
    encoding="utf-8"
)

status_path.write_text(
    json.dumps({
        "generated_at": generated_at,
        "timestamp": payload.get("timestamp"),
        "source_snapshot": str(latest),
        "source": payload.get("source"),
        "event": payload.get("event"),
        "profile": payload.get("profile"),
        "snapshot_version": payload.get("snapshot_version"),
        "snapshot_note_count": len(by_id),
        "notes_with_images": payload.get("notes_with_images"),
        "total_decks": total_decks,
        "total_cards": total_cards,
        "total_notes": total_notes,
    }, ensure_ascii=False, indent=2),
    encoding="utf-8"
)

print("latest =", latest)
print("published =", state_path)
print("published =", decks_path)
print("published =", status_path)
print("notes =", len(by_id))
print("decks =", total_decks)
print("cards =", total_cards)
PY

echo "[1/6] Publish current media refs..."
python3 - <<PY
import json
import html
from html.parser import HTMLParser
from pathlib import Path

state_path = Path("$STATE/notes_index.json")
media_refs_path = Path("$STATE/media_refs.txt")


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


def extract_media_refs_from_html(value):
    parser = ImageSrcParser()
    parser.feed(value)
    parser.close()
    return [ref for ref in parser.refs if ref]


def extract_media_refs_from_fields(fields):
    refs = []
    for value in (fields or {}).values():
        if not isinstance(value, str) or not value:
            continue
        refs.extend(extract_media_refs_from_html(value))
    return refs


def normalize_media_refs(note):
    fields_refs = extract_media_refs_from_fields(note.get("fields", {}))
    if fields_refs:
        return fields_refs

    refs = note.get("image_refs", [])
    if not isinstance(refs, list):
        return []
    return [html.unescape(ref).strip() for ref in refs if isinstance(ref, str) and ref.strip()]


with state_path.open("r", encoding="utf-8") as f:
    notes_index = json.load(f)

if isinstance(notes_index, dict):
    notes = notes_index.values()
elif isinstance(notes_index, list):
    notes = notes_index
else:
    notes = []

seen = set()
media_refs = []
for note in notes:
    if not isinstance(note, dict):
        continue
    for ref in normalize_media_refs(note):
        if ref and ref not in seen:
            seen.add(ref)
            media_refs.append(ref)

media_refs_path.write_text("\n".join(media_refs) + ("\n" if media_refs else ""), encoding="utf-8")
print("published =", media_refs_path)
print("media_refs =", len(media_refs))
PY

echo "[2/6] Rebuild note_media_index..."
python3 "$SCRIPTS/build_note_media_index.py"

echo "[2b/7] Publish transactional state generation..."
PYTHONPATH="$SCRIPTS" python3 - <<PY
import json
from pathlib import Path
from state_store import publish_generation

state = Path("$STATE")
objects = {
    name: json.loads((state / name).read_text(encoding="utf-8"))
    for name in (
        "notes_index.json",
        "decks_index.json",
        "note_media_index.json",
        "snapshot_status.json",
    )
}
status = objects["snapshot_status.json"]
manifest = publish_generation(state, objects, metadata={
    "source": "rebuild_state",
    "generated_at": status.get("generated_at"),
    "snapshot_version": status.get("snapshot_version"),
    "total_notes": status.get("total_notes"),
    "total_cards": status.get("total_cards"),
    "total_decks": status.get("total_decks"),
})
print("generation_id =", manifest["generation_id"])
PY

echo "[3/7] Diagnose deck/card index consistency..."
DIAG_ARGS=(--state-dir "$STATE" --limit 20)
if [[ "${ANKI_GPT_FAIL_ON_DECK_DIVERGENCE:-0}" == "1" ]]; then
  DIAG_ARGS+=(--fail-on-divergence)
fi
python3 "$SCRIPTS/diagnose_deck_index_consistency.py" "${DIAG_ARGS[@]}" > "$STATE/deck_index_diagnostics.json"
python3 - <<PY
import json
from pathlib import Path

diagnostics_path = Path("$STATE/deck_index_diagnostics.json")
report = json.loads(diagnostics_path.read_text(encoding="utf-8"))
print("deck_diagnostics =", diagnostics_path)
print("positive_decks_checked =", report.get("positive_decks_checked"))
print("divergent_decks =", report.get("divergent_decks"))
print("snapshot_cards_indexed =", report.get("snapshot_cards_indexed"))
PY

echo "[4/7] Restart query API..."
PID="$(ss -ltnp | awk '/:8767 / {match($0,/pid=([0-9]+)/,m); if (m[1]!="") print m[1]}' | head -n1 || true)"
if [ -n "${PID:-}" ]; then
  kill "$PID" || true
  sleep 1
fi

tmux kill-session -t anki-query-api 2>/dev/null || true
tmux new-session -d -s anki-query-api "bash $SCRIPTS/start_query_api.sh" 9>&-

sleep 2

echo "[5/7] Smoke test..."
python3 - <<PY
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

token = os.environ.get("ANKI_GPT_TAGGING_TOKEN", "").strip()
if not token:
    token_path = Path("$BASE/tagging_token.txt")
    raw_token = token_path.read_text(encoding="utf-8")
    lines = raw_token.splitlines()
    token = raw_token.strip()
    if len(lines) != 1 or not token or any(char.isspace() for char in token):
        raise SystemExit("Smoke test failed: tagging token file must contain exactly one token")


def fetch_json(url):
    request = Request(url, headers={"X-Tagging-Token": token}, method="GET")
    return json.loads(urlopen(request, timeout=10).read().decode("utf-8"))


roots = fetch_json("http://127.0.0.1:8767/roots")
status = fetch_json("http://127.0.0.1:8767/snapshot/status")
accent = fetch_json(
    "http://127.0.0.1:8767/cards/search?"
    + urlencode({"q": "fermenta\u00e7\u00e3o", "limit": 3})
)
ascii_ = fetch_json("http://127.0.0.1:8767/cards/search?q=fermentacao&limit=3")

roots_list = roots.get("roots")
if not isinstance(roots_list, list) or not roots_list or not all(isinstance(r, str) and r for r in roots_list):
    raise SystemExit(f"Smoke test failed for /roots: {roots}")

for label, payload in [("accent", accent), ("ascii", ascii_)]:
    cards = payload.get("cards")
    if not isinstance(cards, list):
        raise SystemExit(f"Smoke test failed for /cards/search ({label}): missing cards list: {payload}")
    if payload.get("limit") != 3:
        raise SystemExit(f"Smoke test failed for /cards/search ({label}): unexpected limit echo: {payload}")
    if payload.get("returned") != len(cards):
        raise SystemExit(f"Smoke test failed for /cards/search ({label}): returned/cards mismatch: {payload}")
    if payload.get("count", 0) < payload.get("returned", 0):
        raise SystemExit(f"Smoke test failed for /cards/search ({label}): count/returned mismatch: {payload}")

if accent.get("count", 0) <= 0 or accent.get("returned", 0) <= 0:
    raise SystemExit(f"Smoke test failed for /cards/search accent query: expected at least one result: {accent}")

accent_ids = [card.get("note_id") for card in accent.get("cards", [])]
ascii_ids = [card.get("note_id") for card in ascii_.get("cards", [])]
if accent.get("count") != ascii_.get("count") or accent_ids != ascii_ids:
    raise SystemExit(
        "Smoke test failed for /cards/search accent-insensitive behavior: "
        f"accent={accent} ascii={ascii_}"
    )

if not status.get("generated_at"):
    raise SystemExit(f"Smoke test failed for /snapshot/status: missing generated_at: {status}")
if not isinstance(status.get("total_decks"), int):
    raise SystemExit(f"Smoke test failed for /snapshot/status: missing total_decks: {status}")

target_deck = "#UFPR::Biologia::Citologia::Estruturas::• Citoplasma"
deck_status_url = "http://127.0.0.1:8767/snapshot/status?" + urlencode({"deck": target_deck})
deck_status = fetch_json(deck_status_url)
if deck_status.get("requested_deck_found"):
    deck_search_url = "http://127.0.0.1:8767/cards/search-real?" + urlencode({
        "deck": target_deck,
        "limit": 3,
    })
    deck_search = fetch_json(deck_search_url)
    if deck_search.get("count", 0) <= 0:
        raise SystemExit(f"Smoke test failed for target deck card search: {deck_search}")
else:
    deck_search = {
        "skipped": True,
        "reason": "target_deck_not_present_in_latest_uploaded_snapshot",
        "deck": target_deck,
    }

print(json.dumps({
    "roots_count": len(roots_list),
    "roots_sample": roots_list[:10],
    "snapshot_generated_at": status.get("generated_at"),
    "snapshot_total_decks": status.get("total_decks"),
    "search_query": accent.get("query"),
    "search_count": accent.get("count"),
    "search_returned": accent.get("returned"),
    "search_note_ids": accent_ids,
    "target_deck_check": {
        "deck": target_deck,
        "found": deck_status.get("requested_deck_found"),
        "search_count": deck_search.get("count"),
        "search_returned": deck_search.get("returned"),
        "skipped": deck_search.get("skipped", False),
    },
}, ensure_ascii=False, indent=2))
PY

echo "[6/7] State sample..."
python3 - <<PY
import json
from pathlib import Path

p = Path("$STATE/notes_index.json")
data = json.loads(p.read_text(encoding="utf-8"))
sample = data.get("1618420535525", {})
print(json.dumps({
    "note_id": sample.get("note_id"),
    "note_type": sample.get("note_type"),
    "kind": sample.get("kind"),
    "compare_text": sample.get("compare_text"),
}, ensure_ascii=False, indent=2))
PY

echo "[6/7] Cleanup retention..."
python3 "$SCRIPTS/cleanup_retention.py" \
  --apply \
  --keep-data-json 20 \
  --logs-days 14 \
  --operations-days 30 \
  --media-temp-days 1

echo "[7/7] Done."
