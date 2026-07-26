import json
import sys
from pathlib import Path

DATA_DIR = Path("/home/ubuntu/anki-gpt-sync/data")

def latest_snapshot():
    files = sorted(DATA_DIR.glob("*.json"))
    if not files:
        raise SystemExit("Nenhum snapshot encontrado.")
    return files[-1]

def load_latest():
    path = latest_snapshot()
    with path.open("r", encoding="utf-8") as f:
        return path, json.load(f)

def cmd_roots(data):
    roots = sorted({note["root_deck"] for note in data.get("notes", [])})
    for r in roots:
        print(r)

def cmd_decks(data, root):
    decks = sorted({note["deck"] for note in data.get("notes", []) if note["root_deck"] == root})
    for d in decks:
        print(d)

def cmd_count(data, deck):
    count = sum(1 for note in data.get("notes", []) if note["deck"] == deck)
    print(count)

def cmd_sample(data, deck, limit=5):
    notes = [n for n in data.get("notes", []) if n["deck"] == deck][:limit]
    for n in notes:
        print("=" * 80)
        print("note_id:", n["note_id"])
        print("deck:", n["deck"])
        print("root_deck:", n["root_deck"])
        print("note_type:", n["note_type"])
        print("tags:", ", ".join(n["tags"]))
        print("fields:")
        for k, v in n["fields"].items():
            print(f"  - {k}: {str(v)[:300]}")

def main():
    path, data = load_latest()
    print(f"[snapshot] {path}", file=sys.stderr)

    if len(sys.argv) < 2:
        raise SystemExit("Uso: roots | decks <root> | count <deck> | sample <deck> [limit]")

    cmd = sys.argv[1]

    if cmd == "roots":
        cmd_roots(data)
    elif cmd == "decks":
        root = sys.argv[2]
        cmd_decks(data, root)
    elif cmd == "count":
        deck = sys.argv[2]
        cmd_count(data, deck)
    elif cmd == "sample":
        deck = sys.argv[2]
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        cmd_sample(data, deck, limit)
    else:
        raise SystemExit("Comando inválido.")

if __name__ == "__main__":
    main()
