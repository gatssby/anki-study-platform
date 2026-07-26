#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen


def load_json(path):
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def int_or_none(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def iter_notes(notes_index):
    if isinstance(notes_index, dict):
        return [note for note in notes_index.values() if isinstance(note, dict)]
    if isinstance(notes_index, list):
        return [note for note in notes_index if isinstance(note, dict)]
    return []


def note_cards(note):
    cards = note.get("cards", [])
    if not isinstance(cards, list):
        return []
    return [card for card in cards if isinstance(card, dict)]


def card_deck_id(card):
    return int_or_none(card.get("deck_id") if card.get("deck_id") is not None else card.get("did"))


def card_id(card):
    return int_or_none(card.get("card_id") if card.get("card_id") is not None else card.get("id"))


def note_id(note, card=None):
    if card is not None:
        cid = int_or_none(card.get("note_id"))
        if cid is not None:
            return cid
    return int_or_none(note.get("note_id"))


def deck_name(deck):
    return str(deck.get("deck_name") or deck.get("name") or "")


def deck_id(deck):
    return int_or_none(deck.get("deck_id") if deck.get("deck_id") is not None else deck.get("id"))


def count_indexes(notes):
    cards_by_deck_id = {}
    cards_by_deck_name = {}
    notes_by_deck_id = {}
    cards_without_id = 0
    cards_without_deck_id = 0
    cards_without_deck_name = 0
    cards_without_note_id = 0
    orphan_cards = 0

    for note in notes:
        current_note_id = note_id(note)
        seen_note_deck_ids = set()
        for card in note_cards(note):
            current_card_id = card_id(card)
            current_deck_id = card_deck_id(card)
            current_deck_name = str(card.get("deck_name") or note.get("deck") or "")
            current_card_note_id = note_id(note, card)

            if current_card_id is None:
                cards_without_id += 1
            if current_deck_id is None:
                cards_without_deck_id += 1
            if not current_deck_name:
                cards_without_deck_name += 1
            if current_card_note_id is None:
                cards_without_note_id += 1
            if current_note_id is not None and current_card_note_id is not None and current_note_id != current_card_note_id:
                orphan_cards += 1

            if current_deck_id is not None:
                cards_by_deck_id[current_deck_id] = cards_by_deck_id.get(current_deck_id, 0) + 1
                seen_note_deck_ids.add(current_deck_id)
            if current_deck_name:
                cards_by_deck_name[current_deck_name] = cards_by_deck_name.get(current_deck_name, 0) + 1

        for current_deck_id in seen_note_deck_ids:
            notes_by_deck_id[current_deck_id] = notes_by_deck_id.get(current_deck_id, 0) + 1

    return {
        "cards_by_deck_id": cards_by_deck_id,
        "cards_by_deck_name": cards_by_deck_name,
        "notes_by_deck_id": notes_by_deck_id,
        "cards_without_id": cards_without_id,
        "cards_without_deck_id": cards_without_deck_id,
        "cards_without_deck_name": cards_without_deck_name,
        "cards_without_note_id": cards_without_note_id,
        "orphan_cards": orphan_cards,
    }


def api_get(api_base, path, params):
    if not api_base:
        return None
    url = api_base.rstrip("/") + path + "?" + urlencode(params)
    try:
        with urlopen(url, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def endpoint_counts(api_base, name):
    if not api_base:
        return {}
    query_deck = f'deck:"{name}"'
    calls = {
        "getCardsByDeck_card": ("/cards/by-deck", {"deck": name, "kind": "card", "limit": "1"}),
        "getCardsByDeck_note": ("/cards/by-deck", {"deck": name, "kind": "note", "limit": "1"}),
        "searchCards_card": ("/cards/search", {"deck": name, "kind": "card", "limit": "1"}),
        "searchCards_note": ("/cards/search", {"deck": name, "kind": "note", "limit": "1"}),
        "searchRealCards": ("/cards/search-real", {"deck": name, "limit": "1"}),
        "getCardIdsForQuery_deck": ("/cards/query-ids", {"deck": name}),
        "getCardIdsForQuery_q": ("/cards/query-ids", {"q": query_deck}),
        "getCardIdsForQuery_prefix": ("/cards/query-ids", {"prefix": name}),
        "searchCards_prefix_card": ("/cards/search", {"prefix": name, "kind": "card", "limit": "1"}),
    }
    out = {}
    for label, (path, params) in calls.items():
        payload = api_get(api_base, path, params)
        if not isinstance(payload, dict):
            out[label] = None
            continue
        out[label] = payload.get("count")
        if label.startswith("getCardIdsForQuery"):
            out[label] = payload.get("total_cards_found", payload.get("count"))
    return out


def main():
    parser = argparse.ArgumentParser(description="Diagnose consistency between deck counts and card/note indexes.")
    parser.add_argument("--state-dir", default="/home/ubuntu/anki-gpt-sync/state")
    parser.add_argument("--notes-index")
    parser.add_argument("--decks-index")
    parser.add_argument("--api-base", default="")
    parser.add_argument("--deck", default="")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--fail-on-divergence", action="store_true")
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    notes_path = Path(args.notes_index) if args.notes_index else state_dir / "notes_index.json"
    decks_path = Path(args.decks_index) if args.decks_index else state_dir / "decks_index.json"

    notes_index = load_json(notes_path)
    decks_index = load_json(decks_path)
    notes = iter_notes(notes_index)
    decks = decks_index.get("decks", []) if isinstance(decks_index, dict) else []
    indexes = count_indexes(notes)

    divergences = []
    positive_decks = 0
    for deck in decks:
        if not isinstance(deck, dict):
            continue
        expected_cards = int_or_none(deck.get("card_count")) or 0
        expected_notes = int_or_none(deck.get("note_count")) or 0
        if expected_cards <= 0:
            continue
        current_deck_id = deck_id(deck)
        current_deck_name = deck_name(deck)
        if args.deck and args.deck not in {str(current_deck_id), current_deck_name}:
            continue
        positive_decks += 1
        raw_by_id = indexes["cards_by_deck_id"].get(current_deck_id, 0)
        raw_by_name = indexes["cards_by_deck_name"].get(current_deck_name, 0)
        materialized = raw_by_id
        if raw_by_id < expected_cards or raw_by_name < expected_cards:
            endpoint_summary = endpoint_counts(args.api_base, current_deck_name)
            divergences.append({
                "deck_id": current_deck_id,
                "deck_name": current_deck_name,
                "getDecks_card_count": expected_cards,
                "getDecks_note_count": expected_notes,
                "raw_cards_by_deck_id": raw_by_id,
                "raw_cards_by_deck_name": raw_by_name,
                "materialized_cards_by_deck_id": materialized,
                "raw_notes_by_deck_id": indexes["notes_by_deck_id"].get(current_deck_id, 0),
                "endpoint_counts": endpoint_summary,
                "difference": expected_cards - raw_by_id,
                "included_in_study_system": deck.get("included_in_study_system"),
            })

    report = {
        "notes_index": str(notes_path),
        "decks_index": str(decks_path),
        "snapshot_notes_indexed": len(notes),
        "snapshot_cards_indexed": sum(len(note_cards(note)) for note in notes),
        "positive_decks_checked": positive_decks,
        "divergent_decks": len(divergences),
        "card_field_issues": {
            "cards_without_id": indexes["cards_without_id"],
            "cards_without_deck_id": indexes["cards_without_deck_id"],
            "cards_without_deck_name": indexes["cards_without_deck_name"],
            "cards_without_note_id": indexes["cards_without_note_id"],
            "orphan_cards": indexes["orphan_cards"],
        },
        "divergences": divergences[: max(args.limit, 0)],
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_on_divergence and divergences:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
