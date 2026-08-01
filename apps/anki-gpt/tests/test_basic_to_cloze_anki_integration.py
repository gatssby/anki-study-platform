from __future__ import annotations

import importlib.util
import http.client
import json
import os
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_organization_with_collection(collection, runtime_dir: Path):
    old_aqt = sys.modules.get("aqt")
    old_runtime_paths = sys.modules.pop("runtime_paths", None)
    old_runtime_dir = os.environ.get("ANKI_GPT_RUNTIME_DIR")
    sys.modules["aqt"] = types.SimpleNamespace(
        mw=types.SimpleNamespace(
            col=collection,
            pm=types.SimpleNamespace(name="Temporary Basic to Cloze Audit"),
        )
    )
    os.environ["ANKI_GPT_RUNTIME_DIR"] = str(runtime_dir)
    sys.path.insert(0, str(ROOT / "addon-local"))
    try:
        path = ROOT / "addon-local" / "organization.py"
        spec = importlib.util.spec_from_file_location("anki_gpt_basic_to_cloze_integration", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(ROOT / "addon-local"))
        if old_aqt is None:
            sys.modules.pop("aqt", None)
        else:
            sys.modules["aqt"] = old_aqt
        if old_runtime_paths is None:
            sys.modules.pop("runtime_paths", None)
        else:
            sys.modules["runtime_paths"] = old_runtime_paths
        if old_runtime_dir is None:
            os.environ.pop("ANKI_GPT_RUNTIME_DIR", None)
        else:
            os.environ["ANKI_GPT_RUNTIME_DIR"] = old_runtime_dir


def load_query_api(runtime_dir: Path):
    backend_dir = ROOT.parents[1] / "services" / "anki-api"
    fo_contracts_dir = ROOT.parents[1] / "packages" / "fo-contracts"
    inserted = [str(backend_dir), str(fo_contracts_dir)]
    for value in reversed(inserted):
        sys.path.insert(0, value)
    try:
        path = backend_dir / "query_api.py"
        spec = importlib.util.spec_from_file_location(
            "anki_gpt_basic_to_cloze_http_integration",
            path,
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        for value in inserted:
            sys.path.remove(value)

    token_file = runtime_dir / "tagging_token.txt"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("fixture-token", encoding="utf-8")
    module.TAGGING_TOKEN_FILE = token_file
    module.ORGANIZATION_OPERATIONS_DIR = runtime_dir / "organization" / "operations"
    module.ACTION_LOG_PATH = runtime_dir / "debug" / "action_log.jsonl"
    return module


def request_test_server(query_api, method: str, path: str, body=None):
    server = query_api.ThreadingHTTPServer(("127.0.0.1", 0), query_api.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            server.server_port,
            timeout=5,
        )
        encoded = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        connection.request(
            method,
            path,
            body=encoded,
            headers={
                "Content-Type": "application/json",
                "X-Tagging-Token": "fixture-token",
            },
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def rename_default_models(collection):
    names = collection.models.all_names_and_ids()
    basic_id = next(int(item.id) for item in names if item.name == "Basic")
    cloze_id = next(int(item.id) for item in names if item.name == "Cloze")
    basic_model = collection.models.get(basic_id)
    cloze_model = collection.models.get(cloze_id)
    basic_model["name"] = "prettify-minimal-basic"
    cloze_model["name"] = "prettify-minimal-cloze"
    collection.models.update_dict(basic_model)
    collection.models.update_dict(cloze_model)
    return basic_model


def snapshot_note(collection, note_id: int) -> dict:
    note = collection.get_note(note_id)
    model = collection.models.get(note.mid)
    field_names = [field["name"] for field in model["flds"]]
    cards = []
    for card_id in collection.db.list("select id from cards where nid = ? order by ord, id", note_id):
        card = collection.get_card(int(card_id))
        deck = collection.decks.get(card.did)
        cards.append({
            "card_id": int(card.id),
            "note_id": note_id,
            "deck_id": int(card.did),
            "deck_name": deck["name"],
            "ord": int(card.ord),
            "queue": int(card.queue),
            "type": int(card.type),
            "due": int(card.due),
            "ivl": int(card.ivl),
            "factor": int(card.factor),
            "reps": int(card.reps),
            "lapses": int(card.lapses),
            "left": int(card.left),
            "odue": int(card.odue),
            "odid": int(card.odid),
        })
    return {
        "note_id": note_id,
        "model_id": int(note.mid),
        "mod": int(note.mod),
        "usn": int(note.usn),
        "deck": cards[0]["deck_name"],
        "root_deck": cards[0]["deck_name"].split("::", 1)[0],
        "note_type": model["name"],
        "kind": "cloze" if model["type"] == 1 else "basic",
        "tags": list(note.tags),
        "field_names": field_names,
        "fields": {name: note[name] for name in field_names},
        "cards": cards,
    }


def publish_snapshot_to_test_backend(query_api, note_snapshot: dict):
    objects = {
        "notes_index.json": {str(note_snapshot["note_id"]): note_snapshot},
        "note_media_index.json": {},
        "decks_index.json": {
            "decks": [{
                "deck_id": note_snapshot["cards"][0]["deck_id"],
                "deck_name": note_snapshot["deck"],
            }],
        },
        "snapshot_status.json": {"generated_at": "temporary-test"},
    }
    query_api.STATE_CACHE = types.SimpleNamespace(
        snapshot=lambda: (objects, {"generation_id": f"temporary-{id(objects)}"}),
        metrics=lambda: {"generation_id": f"temporary-{id(objects)}"},
    )
    query_api.DERIVED_STATE_CACHE.update({"key": None, "value": None, "hits": 0, "misses": 0})


def test_real_anki_temp_collection_converts_in_place_and_preserves_state(tmp_path):
    anki_collection = pytest.importorskip("anki.collection")
    collection = anki_collection.Collection(str(tmp_path / "collection.anki2"))
    try:
        basic_model = rename_default_models(collection)

        deck_id = collection.decks.id("Fixture::Basic to Cloze")
        note = collection.new_note(basic_model)
        note["Front"] = "Em uma teia alimentar, o nível trófico de um organismo é fixo?"
        note["Back"] = "Não. O mesmo organismo pode ocupar diferentes níveis tróficos."
        note.tags = ["FO", "preservar"]
        collection.add_note(note, deck_id)
        note_id = int(note.id)

        organization = load_organization_with_collection(collection, tmp_path / "runtime")
        source = collection.get_note(note_id)
        payload = {
            "note_id": note_id,
            "source_front": source["Front"],
            "source_back": source["Back"],
            "text": ("Em uma teia alimentar, o nível trófico de um organismo {{c1::não é fixo}}."),
            "back_extra": (
                "O mesmo organismo pode ocupar diferentes níveis tróficos "
                "conforme a cadeia alimentar considerada."
            ),
            "target_model_name": "prettify-minimal-cloze",
            **organization.note_precondition(source, ["Front", "Back"]),
        }
        before_cards = organization.basic_to_cloze_card_state(note_id)
        result = organization.convert_basic_to_cloze(payload, dry_run=False)
        changed = collection.get_note(note_id)
        after_cards = organization.basic_to_cloze_card_state(note_id)

        assert result["converted"] is True
        assert int(changed.id) == note_id
        assert changed["Text"] == payload["text"]
        assert changed["Back Extra"] == payload["back_extra"]
        assert changed.tags == ["FO", "preservar"]
        assert result["card_ids_before"] == result["card_ids_after"]
        assert before_cards == after_cards
    finally:
        collection.close()


def test_gpt_schema_http_backend_addon_round_trip_preserves_structure(tmp_path):
    from jsonschema import Draft202012Validator

    anki_collection = pytest.importorskip("anki.collection")
    collection = anki_collection.Collection(str(tmp_path / "e2e.anki2"))
    try:
        basic_model = rename_default_models(collection)
        deck_name = "Fixture::Basic to Cloze E2E"
        deck_id = collection.decks.id(deck_name)
        note = collection.new_note(basic_model)
        note["Front"] = "Em uma teia alimentar, o nível trófico de um organismo é fixo?"
        note["Back"] = "Não. O mesmo organismo pode ocupar diferentes níveis tróficos."
        note.tags = ["FO", "preservar"]
        collection.add_note(note, deck_id)
        note_id = int(note.id)

        original_card = collection.get_card(
            int(collection.db.scalar("select id from cards where nid = ?", note_id))
        )
        original_card.queue = 2
        original_card.type = 2
        original_card.due = 42
        original_card.ivl = 9
        original_card.factor = 2450
        original_card.reps = 7
        original_card.lapses = 1
        collection.update_card(original_card)

        runtime_dir = tmp_path / "runtime"
        organization = load_organization_with_collection(collection, runtime_dir / "addon")
        query_api = load_query_api(runtime_dir / "backend")

        publish_snapshot_to_test_backend(query_api, snapshot_note(collection, note_id))
        read_status, read_body = request_test_server(
            query_api,
            "GET",
            f"/notes/info?ids={note_id}",
        )
        assert read_status == 200
        canonical = read_body["notes"][0]
        assert canonical["note_id"] == note_id
        assert canonical["note_type"] == "prettify-minimal-basic"
        assert canonical["precondition_available"] is True

        payload = {
            "note_id": note_id,
            "source_front": canonical["fields"]["Front"],
            "source_back": canonical["fields"]["Back"],
            "text": "Em uma teia alimentar, o nível trófico de um organismo {{c1::não é fixo}}.",
            "back_extra": (
                "O mesmo organismo pode ocupar diferentes níveis tróficos "
                "conforme a cadeia alimentar considerada."
            ),
            "target_model_name": "prettify-minimal-cloze",
            "expected_content_hash": canonical["expected_content_hash"],
            "expected_mod": canonical["expected_mod"],
            "expected_usn": canonical["expected_usn"],
            "expected_model_id": canonical["expected_model_id"],
            "execution_mode": "direct",
            "dry_run": False,
            "requested_by": "gpt-fixture",
            "reason": "Teste ponta a ponta temporário",
        }

        schema = json.loads(
            (ROOT.parents[1] / "contracts" / "openapi" / "gpt-action-compact.openapi.json")
            .read_text(encoding="utf-8")
        )
        request_schema = (
            schema["paths"]["/organization/convert-basic-to-cloze"]["post"]
            ["requestBody"]["content"]["application/json"]["schema"]
        )
        root_validator = Draft202012Validator(schema)
        validator = root_validator.evolve(schema=request_schema)
        assert list(validator.iter_errors(payload)) == []

        create_status, create_body = request_test_server(
            query_api,
            "POST",
            "/organization/convert-basic-to-cloze",
            payload,
        )
        assert create_status == 200
        response_schema = (
            schema["paths"]["/organization/convert-basic-to-cloze"]["post"]
            ["responses"]["200"]["content"]["application/json"]["schema"]
        )
        response_validator = root_validator.evolve(schema=response_schema)
        assert list(response_validator.iter_errors(create_body)) == []
        assert create_body["operation"]["operation_type"] == "convert_basic_to_cloze"

        execution = organization.execute_organization_operation(create_body["operation"])
        assert execution["ok"] is True
        assert execution["status"] == "done"
        result = execution["result"]
        assert result["converted"] is True
        assert result["card_ids_before"] == [int(original_card.id)]
        assert result["card_ids_after"] == [int(original_card.id)]
        assert result["new_card_ids"] == []
        assert result["card_state_before"] == result["card_state_after"]

        publish_snapshot_to_test_backend(query_api, snapshot_note(collection, note_id))
        reread_status, reread_body = request_test_server(
            query_api,
            "GET",
            f"/notes/info?ids={note_id}",
        )
        assert reread_status == 200
        final_note = reread_body["notes"][0]
        assert final_note["note_id"] == note_id
        assert final_note["note_type"] == "prettify-minimal-cloze"
        assert final_note["fields"] == {
            "Text": payload["text"],
            "Back Extra": payload["back_extra"],
        }
        assert final_note["deck"] == deck_name
        assert final_note["tags"] == ["FO", "preservar"]
        assert final_note["card_count"] == 1
        assert final_note["cards"][0]["card_id"] == int(original_card.id)
        assert final_note["cards"][0]["due"] == 42
        assert final_note["cards"][0]["ivl"] == 9
        assert final_note["cards"][0]["reps"] == 7
    finally:
        collection.close()


def test_real_anki_temp_collection_preserves_media_audio_mathjax_and_c2_scheduling(tmp_path):
    anki_collection = pytest.importorskip("anki.collection")
    collection = anki_collection.Collection(str(tmp_path / "c2.anki2"))
    try:
        basic_model = rename_default_models(collection)
        deck_id = collection.decks.id("Fixture::Cloze multiplicity")
        organization = load_organization_with_collection(collection, tmp_path / "runtime")

        structural = collection.new_note(basic_model)
        structural["Front"] = (
            '<img src="ciclo.png"><br>Na fórmula \\(E=mc^2\\), qual grandeza '
            'é representada por \\(m\\)? [sound:explicacao.mp3]'
        )
        structural["Back"] = "Massa."
        collection.add_note(structural, deck_id)
        structural_id = int(structural.id)
        structural_source = collection.get_note(structural_id)
        structural_payload = {
            "note_id": structural_id,
            "source_front": structural_source["Front"],
            "source_back": structural_source["Back"],
            "text": (
                '<img src="ciclo.png"><br>Na fórmula \\(E=mc^2\\), \\(m\\) '
                'representa a {{c1::massa}}. [sound:explicacao.mp3]'
            ),
            "back_extra": "",
            "target_model_name": "prettify-minimal-cloze",
            **organization.note_precondition(structural_source, ["Front", "Back"]),
        }
        structural_result = organization.convert_basic_to_cloze(
            structural_payload,
            dry_run=False,
        )
        structural_after = collection.get_note(structural_id)
        assert structural_result["converted"] is True
        for marker in ('<img src="ciclo.png">', "[sound:explicacao.mp3]", r"\(E=mc^2\)", r"\(m\)"):
            assert marker in structural_after["Text"]

        multiple = collection.new_note(basic_model)
        multiple["Front"] = "Quais são as duas etapas independentes da nitrificação?"
        multiple["Back"] = "Amônia em nitrito; depois, nitrito em nitrato."
        multiple.tags = ["nitrificação", "preservar"]
        collection.add_note(multiple, deck_id)
        multiple_id = int(multiple.id)
        original_card_id = int(
            collection.db.scalar("select id from cards where nid = ?", multiple_id)
        )
        original_card = collection.get_card(original_card_id)
        original_card.queue = 2
        original_card.type = 2
        original_card.due = 73
        original_card.ivl = 14
        original_card.factor = 2500
        original_card.reps = 12
        original_card.lapses = 2
        collection.update_card(original_card)

        multiple_source = collection.get_note(multiple_id)
        multiple_payload = {
            "note_id": multiple_id,
            "source_front": multiple_source["Front"],
            "source_back": multiple_source["Back"],
            "text": (
                "A nitrificação converte {{c1::amônia em nitrito}} e depois "
                "{{c2::nitrito em nitrato}}."
            ),
            "back_extra": "",
            "target_model_name": "prettify-minimal-cloze",
            **organization.note_precondition(multiple_source, ["Front", "Back"]),
        }
        before = organization.basic_to_cloze_card_state(multiple_id)
        result = organization.convert_basic_to_cloze(multiple_payload, dry_run=False)
        after = organization.basic_to_cloze_card_state(multiple_id)

        assert result["converted"] is True
        assert result["preserved_card_ids"] == [original_card_id]
        assert len(result["new_card_ids"]) == 1
        assert len(after) == 2
        c1 = next(card for card in after if card["ord"] == 0)
        c2 = next(card for card in after if card["ord"] == 1)
        assert c1["card_id"] == original_card_id
        assert c1["scheduling"] == before[0]["scheduling"]
        assert c2["card_id"] == result["new_card_ids"][0]
        assert c2["scheduling"]["queue"] == 0
        assert c2["scheduling"]["type"] == 0
        assert c2["scheduling"]["due"] != 73
        assert c2["scheduling"]["ivl"] == 0
        assert c2["scheduling"]["factor"] == 0
        assert c2["scheduling"]["reps"] == 0
        assert c2["scheduling"]["lapses"] == 0
        assert c2["scheduling"]["left"] == 0
        assert c2["scheduling"]["odue"] == 0
        assert c2["scheduling"]["odid"] == 0
        assert c1["deck_id"] == c2["deck_id"] == deck_id
        assert collection.get_note(multiple_id).tags == ["nitrificação", "preservar"]
    finally:
        collection.close()
