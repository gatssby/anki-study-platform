import importlib.util
import ast
from datetime import datetime, timedelta, timezone
import json
import http.client
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


@pytest.fixture(scope="module")
def query_api():
    backend_dir = ROOT / "remote-backend"
    sys.path.insert(0, str(backend_dir))
    try:
        yield load_module("anki_gpt_query_api_audit", backend_dir / "query_api.py")
    finally:
        sys.path.remove(str(backend_dir))


@pytest.fixture(scope="module")
def organization():
    sys.path.insert(0, str(ROOT / "addon-local"))
    old_aqt = sys.modules.get("aqt")
    old_runtime_paths = sys.modules.pop("runtime_paths", None)
    old_runtime_override = os.environ.get("ANKI_GPT_RUNTIME_DIR")
    sys.modules["aqt"] = types.SimpleNamespace(mw=types.SimpleNamespace())
    with tempfile.TemporaryDirectory(prefix="anki-gpt-addon-tests-") as runtime_dir:
        os.environ["ANKI_GPT_RUNTIME_DIR"] = runtime_dir
        try:
            yield load_module("anki_gpt_organization_audit", ROOT / "addon-local" / "organization.py")
        finally:
            sys.path.remove(str(ROOT / "addon-local"))
            sys.modules.pop("runtime_paths", None)
            if old_runtime_paths is not None:
                sys.modules["runtime_paths"] = old_runtime_paths
            if old_runtime_override is None:
                os.environ.pop("ANKI_GPT_RUNTIME_DIR", None)
            else:
                os.environ["ANKI_GPT_RUNTIME_DIR"] = old_runtime_override
            if old_aqt is None:
                sys.modules.pop("aqt", None)
            else:
                sys.modules["aqt"] = old_aqt


def test_fo_path_traversal_is_rejected(query_api):
    with pytest.raises(ValueError, match="invalid_relative_path"):
        query_api.validate_fo_relative_path("../private.pdf")
    with pytest.raises(ValueError, match="invalid_relative_path"):
        query_api.validate_fo_transcript_relative_path("../../etc/passwd")


def make_fo_fixture(tmp_path):
    import sqlite3

    root = tmp_path / "transcripts"
    root.mkdir()
    first = root / "Geografia" / "Aula 01.transcricao.md"
    first.parent.mkdir()
    first.write_text("Cartografia e projeções cartográficas. Escala cartográfica.", encoding="utf-8")
    queue = tmp_path / "queue.sqlite"
    connection = sqlite3.connect(queue)
    connection.execute(
        """create table transcription_queue (
        output_relative_path text, materia text, frente text, aula_number integer,
        aula_title text, tipo text, status text)"""
    )
    connection.execute(
        "insert into transcription_queue values (?, ?, ?, ?, ?, ?, 'done')",
        ("Federal Online Transcrições/Geografia/Aula 01.transcricao.md", "Geografia", "Geografia I", 1, "Cartografia", "Aula"),
    )
    connection.commit()
    connection.close()
    return root, queue, first


def test_fo_fts_rebuild_incremental_verify_and_accent_search(query_api, monkeypatch, tmp_path):
    module = load_module("fo_search_index_audit", ROOT / "remote-backend" / "rebuild_fo_search_index.py")
    root, queue, first = make_fo_fixture(tmp_path)
    index = tmp_path / "search.sqlite"
    records, missing = module.source_records(queue, root)
    assert missing == 0
    module.rebuild(index, records)
    assert module.verify_index(index) == {"integrity": "ok", "documents": 1, "fts_rows": 1}
    monkeypatch.setattr(query_api, "FO_SEARCH_INDEX_PATH", index)

    accented = query_api.search_fo_transcript_index("projeções cartográficas", limit=10)
    ascii_query = query_api.search_fo_transcript_index("projecoes cartogra", limit=10)
    assert accented["count"] == 1
    assert ascii_query["count"] == 1
    assert query_api.search_fo_transcript_index('"projeções cartográficas"', limit=10)["count"] == 1
    assert query_api.search_fo_transcript_index('"cartográficas projeções"', limit=10)["count"] == 0
    assert "Cartografia" in accented["items"][0]["metadata"]["aula_title"]
    assert len(accented["items"][0]["matches"][0]["snippet"]) < 500

    first.write_text("Conteúdo alterado sobre mapas temáticos.", encoding="utf-8")
    records, _ = module.source_records(queue, root)
    changed = module.incremental(index, records)
    assert changed["updated"] == 1
    assert query_api.search_fo_transcript_index("mapas tem", limit=10)["count"] == 1

    first.unlink()
    records, missing = module.source_records(queue, root)
    removed = module.incremental(index, records)
    assert missing == 1
    assert removed["removed"] == 1
    assert module.verify_index(index)["documents"] == 0


def test_fo_fts_handles_empty_invalid_duplicate_and_failed_rebuild(query_api, tmp_path, monkeypatch):
    import shutil
    import sqlite3

    module = load_module("fo_search_index_edge_audit", ROOT / "remote-backend" / "rebuild_fo_search_index.py")
    root, queue, first = make_fo_fixture(tmp_path)
    empty = root / "Geografia" / "Vazia.transcricao.md"
    invalid = root / "Geografia" / "Invalida.transcricao.md"
    empty.write_bytes(b"")
    invalid.write_bytes(b"conteudo \xff artificial")
    connection = sqlite3.connect(queue)
    rows = [
        ("Federal Online Transcrições/Geografia/Vazia.transcricao.md", "Geografia", "G", 2, "Vazia", "Aula"),
        ("Federal Online Transcrições/Geografia/Invalida.transcricao.md", "Geografia", "G", 3, "Invalida", "Aula"),
        # Duplicate source row must collapse to one indexed path.
        ("Federal Online Transcrições/Geografia/Invalida.transcricao.md", "Geografia", "G", 3, "Invalida", "Aula"),
    ]
    connection.executemany(
        "insert into transcription_queue values (?, ?, ?, ?, ?, ?, 'done')",
        rows,
    )
    connection.commit()
    connection.close()

    records, missing = module.source_records(queue, root)
    assert missing == 0
    index = tmp_path / "edge.sqlite"
    module.rebuild(index, records)
    verified = module.verify_index(index)
    assert verified["documents"] == 3
    monkeypatch.setattr(query_api, "FO_SEARCH_INDEX_PATH", index)
    assert query_api.search_fo_transcript_index("artificial", limit=10)["count"] == 1

    corrupt = tmp_path / "corrupt.sqlite"
    shutil.copy2(index, corrupt)
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        module.verify_index(corrupt)

    original_hash = __import__("hashlib").sha256(index.read_bytes()).hexdigest()
    original_upsert = module.upsert_record
    monkeypatch.setattr(module, "upsert_record", lambda *_args: (_ for _ in ()).throw(RuntimeError("fixture rebuild failure")))
    with pytest.raises(RuntimeError, match="fixture rebuild failure"):
        module.rebuild(index, records)
    assert __import__("hashlib").sha256(index.read_bytes()).hexdigest() == original_hash
    monkeypatch.setattr(module, "upsert_record", original_upsert)


def test_visual_normalizer_preserves_cloze_and_structural_html(query_api):
    source = (
        '<div><img src="x.png"><br><table><tr><td>'
        '{{c1::<span class="kw">Átomo</span>::<span class="hint">Conceito</span>}}'
        "</td></tr></table></div>"
    )
    normalized, stats = query_api.normalize_visual_html(source)
    assert normalized == source
    assert stats["normalized"] is False


def test_visual_normalizer_removes_only_documented_visual_wrappers(query_api):
    source = '<b>Termo</b> <span style="color:red">visual</span> <em>científico</em>'
    normalized, stats = query_api.normalize_visual_html(source)
    assert normalized == "Termo visual <em>científico</em>"
    assert stats["removed_visual_wrappers_count"] == 2


def test_note_summary_uses_defined_plain_text_extractor(organization):
    note = {
        "Text": "<div>Água &amp; ação<br><b>artificial</b></div>",
        "Back Extra": "<span class=\"hint\">dica</span>",
    }
    summary = organization.note_summary(note)
    assert summary == "Água & ação artificial dica"
    assert "<" not in summary


@pytest.mark.parametrize(
    "unsafe",
    [
        "<script>alert(1)</script>",
        '<img src="x" onerror="alert(1)">',
        '<a href="javascript:alert(1)">x</a>',
        '<iframe src="https://invalid.example"></iframe>',
        '<form action="https://invalid.example"><input></form>',
    ],
)
def test_visual_normalizers_reject_active_html(query_api, organization, unsafe):
    for module in (query_api, organization):
        with pytest.raises(ValueError, match="unsafe_html"):
            module.normalize_visual_html(unsafe)


@pytest.mark.parametrize(
    "safe",
    [
        '<span class="kw">Termo</span>',
        '<span class="hint">dica</span>',
        '<span class="kw extra" data-kind="fixture">Termo</span>',
        '{{c1::<span class="kw">Termo</span>::<span class="hint">Dica</span>}}',
        r"\(x^2 + y^2\) [latex]x^2[/latex]",
        '<br><ul><li>Um</li></ul><table><tr><td>Dois</td></tr></table>',
        '<img src="fixture.png" alt="mídia"><a href="https://example.invalid">link</a>',
        'Água &amp; ação &#231; <em style="color:red" data-kind="fixture">Unicode</em>',
        '',
        '<div><span class="kw">não fechado',
    ],
)
def test_visual_normalizer_semantic_round_trip(query_api, organization, safe):
    backend_value, _ = query_api.normalize_visual_html(safe)
    addon_value, _ = organization.normalize_visual_html(backend_value)
    second_value, _ = organization.normalize_visual_html(addon_value)
    assert second_value == addon_value
    assert "{{c1::" in second_value if "{{c1::" in safe else True
    assert "fixture.png" in second_value if "fixture.png" in safe else True


def test_updates_id_hydration_verifies_hash(organization, monkeypatch):
    remote = {
        "updates_id": "nfupd-test",
        "note_updates": [{"note_id": 1, "fields": {"Text": "novo"}}],
    }
    remote["sha256"] = organization.note_field_updates_sha256(remote)
    monkeypatch.setattr(organization, "load_remote_note_field_updates", lambda _updates_id: remote)
    hydrated = organization.hydrate_update_note_fields_payload_from_updates_id(
        {
            "updates_id": "nfupd-test",
            "updates_sha256": remote["sha256"],
            "note_updates_count": 1,
            "note_ids_count": 1,
            "dry_run": True,
        }
    )
    assert hydrated["note_updates"] == remote["note_updates"]


def test_backend_preserves_valid_note_preconditions(query_api):
    expected_hash = "a" * 64
    normalized = query_api.normalize_update_note_fields_payload({
        "note_updates": [{
            "note_id": 1,
            "fields": {"Text": "next"},
            "expected_content_hash": expected_hash,
            "expected_mod": 2,
            "expected_usn": 3,
            "expected_model_id": 4,
        }],
        "dry_run": False,
    })
    update = normalized["note_updates"][0]
    assert update["expected_content_hash"] == expected_hash
    assert update["expected_mod"] == 2


def test_backend_rejects_nested_note_precondition_with_expected_format(query_api):
    with pytest.raises(
        ValueError,
        match="expected top-level keys expected_content_hash, expected_mod, expected_usn, expected_model_id",
    ):
        query_api.normalize_update_note_fields_payload({
            "note_updates": [{
                "note_id": 1,
                "fields": {"Text": "next"},
                "precondition": {"fields": {"Text": "before"}},
            }],
            "dry_run": False,
        })


def test_backend_rejects_apply_without_required_top_level_hash(query_api):
    with pytest.raises(
        ValueError,
        match="missing_note_precondition: every note update in apply v2 requires top-level expected_content_hash",
    ):
        query_api.normalize_update_note_fields_payload({
            "note_updates": [{"note_id": 1, "fields": {"Text": "next"}}],
            "dry_run": False,
        })


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"execution_mode": "preview"}, "preview"),
        ({"execution_mode": "direct"}, "direct"),
        ({"dry_run": True}, "preview"),
        ({"dry_run": False}, "direct"),
        ({"execution_mode": "preview", "dry_run": True}, "preview"),
        ({"execution_mode": "direct", "dry_run": False}, "direct"),
    ],
)
def test_execution_mode_contract_normalizes_legacy_and_canonical_inputs(
    query_api, payload, expected
):
    assert query_api.normalize_execution_mode(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"execution_mode": "direct", "dry_run": True},
        {"execution_mode": "preview", "dry_run": False},
    ],
)
def test_execution_mode_contract_rejects_incompatible_inputs(query_api, payload):
    with pytest.raises(ValueError, match="execution_mode_dry_run_mismatch"):
        query_api.normalize_execution_mode(payload)


def test_new_operation_defaults_direct_and_preview_explicitly_overrides(query_api):
    base = {
        "operation_type": "update_note_fields",
        "payload": {
            "note_updates": [{
                "note_id": 1,
                "fields": {"Text": "next"},
                "expected_content_hash": "a" * 64,
            }],
        },
    }
    direct = query_api.build_organization_operation(base)
    preview = query_api.build_organization_operation({
        **base,
        "execution_mode": "preview",
    })
    assert direct["operation_schema_version"] == 3
    assert direct["execution_mode"] == "direct"
    assert direct["dry_run"] is False
    assert direct["payload"]["execution_mode"] == "direct"
    assert direct["payload"]["dry_run"] is False
    assert direct["confirmed_by_user"] is False
    assert direct["risk_level"] == "standard"
    assert preview["execution_mode"] == "preview"
    assert preview["payload"]["dry_run"] is True
    assert query_api.operation_risk_level("reorder_cards_by_material") == "structural"
    assert query_api.operation_risk_level("update_note_fields") == "standard"


def test_persisted_legacy_operation_mode_is_inferred_without_rewriting_source(
    query_api, monkeypatch, tmp_path
):
    operations_dir = tmp_path / "operations"
    operations_dir.mkdir()
    monkeypatch.setattr(query_api, "ORGANIZATION_OPERATIONS_DIR", operations_dir)
    path = operations_dir / "orgop-legacy.json"
    path.write_text(json.dumps({
        "operation_id": "orgop-legacy",
        "operation_type": "update_note_fields",
        "operation_schema_version": 2,
        "status": "pending",
        "payload": {"note_updates": [{"note_id": 1}]},
    }), encoding="utf-8")

    loaded = query_api.load_organization_operations()
    assert loaded[0]["execution_mode"] == "preview"
    assert loaded[0]["dry_run"] is True
    assert json.loads(path.read_text(encoding="utf-8")).get("execution_mode") is None


def test_backend_has_no_duplicate_exact_route_branches():
    tree = ast.parse((ROOT / "remote-backend" / "query_api.py").read_text(encoding="utf-8"))
    for method_name in ("do_GET", "do_POST", "do_HEAD"):
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == method_name)
        routes = []
        for node in ast.walk(method):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
                continue
            values = [node.left, *node.comparators]
            if not any(isinstance(value, ast.Name) and value.id == "path" for value in values):
                continue
            routes.extend(
                value.value
                for value in values
                if isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and value.value.startswith("/")
            )
        duplicates = sorted(route for route in set(routes) if routes.count(route) > 1)
        assert duplicates == [], f"{method_name} has duplicate exact branches: {duplicates}"


def test_gpt_openapi_documents_concrete_note_updates_json_contract():
    from jsonschema import Draft202012Validator, RefResolver

    schema = json.loads(
        (ROOT / "gpt-knowledge" / "schema gpt.json").read_text(encoding="utf-8")
    )
    components = schema["components"]["schemas"]
    assert components["ExecutionMode"]["default"] == "direct"
    assert components["ExecutionMode"]["enum"] == ["preview", "direct"]
    note_update = components["NoteFieldUpdate"]
    assert note_update["required"] == ["note_id", "fields"]
    assert "precondition" not in note_update["properties"]
    assert (
        note_update["properties"]["expected_content_hash"]["pattern"]
        == "^[0-9a-f]{64}$"
    )

    request_schema = components["NoteFieldUpdatesCreateRequest"]
    encoded_updates = request_schema["properties"]["note_updates_json"]
    assert encoded_updates["contentMediaType"] == "application/json"
    assert (
        encoded_updates["contentSchema"]["items"]["$ref"]
        == "#/components/schemas/NoteFieldUpdate"
    )
    assert "dry_run_operation_id" in request_schema["properties"]

    schema_cases = [
        (
            schema,
            "/organization/update-note-fields-create",
            {"updates_id": "nfupd-fixture"},
        ),
        (
            json.loads(
                (
                    ROOT
                    / "remote-backend"
                    / "anki_gpt_full_schema_30ops_stable.openapi.json"
                ).read_text(encoding="utf-8")
            ),
            "/organization/update-note-fields",
            {"note_updates": [{"note_id": 1, "fields": {"Text": "fixture"}}]},
        ),
        (
            json.loads(
                (
                    ROOT
                    / "remote-backend"
                    / "gpt_builder_organization_wrappers.openapi.json"
                ).read_text(encoding="utf-8")
            ),
            "/organization/update-note-fields",
            {"note_updates": [{"note_id": 1, "fields": {"Text": "fixture"}}]},
        ),
    ]
    for document, path, base_payload in schema_cases:
        operation_schema = (
            document["paths"][path]["post"]["requestBody"]["content"]
            ["application/json"]["schema"]
        )
        validator = Draft202012Validator(
            operation_schema,
            resolver=RefResolver.from_schema(document),
        )
        assert not list(validator.iter_errors({
            **base_payload,
            "execution_mode": "direct",
            "dry_run": False,
        }))
        assert not list(validator.iter_errors({
            **base_payload,
            "execution_mode": "preview",
            "dry_run": True,
        }))
        assert list(validator.iter_errors({
            **base_payload,
            "execution_mode": "direct",
            "dry_run": True,
        }))
        assert list(validator.iter_errors({
            **base_payload,
            "execution_mode": "preview",
            "dry_run": False,
        }))


def test_compact_openapi_is_gpt_builder_compatible_and_preserves_23_operations():
    schema_path = REPO_ROOT / "contracts" / "openapi" / "gpt-action-compact.openapi.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator_module = load_module(
        "anki_gpt_openapi_validator_regression",
        REPO_ROOT / "scripts" / "validate_openapi.py",
    )

    compatibility = schema["components"]["schemas"]["ExecutionModeCompatibility"]
    assert compatibility["properties"]["execution_mode"]["$ref"].endswith("/ExecutionMode")
    assert compatibility["properties"]["dry_run"]["type"] == "boolean"

    reorder_schema = (
        schema["paths"]["/organization/reorder-order-create"]["post"]
        ["requestBody"]["content"]["application/json"]["schema"]
    )
    assert reorder_schema == {
        "$ref": "#/components/schemas/ReorderOrderCreateRequest",
    }
    assert validator_module.object_schemas_without_properties(schema) == []

    operation_ids = [
        operation["operationId"]
        for item in schema["paths"].values()
        for method, operation in item.items()
        if method in validator_module.METHODS
    ]
    assert len(operation_ids) == 23
    assert len(set(operation_ids)) == 23
    assert "X-Tagging-Token" not in schema_path.read_text(encoding="utf-8")


def test_gpt_builder_object_scanner_covers_nested_schema_locations():
    validator_module = load_module(
        "anki_gpt_openapi_validator_nested_regression",
        REPO_ROOT / "scripts" / "validate_openapi.py",
    )
    artificial = {
        "components": {
            "schemas": {
                "Container": {
                    "type": "object",
                    "properties": {
                        "payload": {
                            "anyOf": [
                                {
                                    "type": "array",
                                    "items": {
                                        "additionalProperties": {
                                            "type": "object",
                                        },
                                    },
                                },
                            ],
                        },
                    },
                },
            },
        },
    }
    assert validator_module.object_schemas_without_properties(artificial) == [
        "$.components.schemas.Container.properties.payload.anyOf[0].items."
        "additionalProperties"
    ]


def test_canonical_gpt_knowledge_requires_direct_default_and_explicit_preview():
    canonical_paths = [
        ROOT / "gpt-knowledge" / "03_padrao_de_reescrita.md",
        ROOT / "gpt-knowledge" / "05_fluxo_decks_grandes.md",
        ROOT / "gpt-knowledge" / "06_fluxo_fo_materiais_transcricoes.md",
        ROOT / "gpt-knowledge" / "INSTRUCTIONS_CURTAS_GPT_BUILDER.md",
    ]
    combined = "\n".join(
        path.read_text(encoding="utf-8").casefold()
        for path in canonical_paths
    )
    assert 'execution_mode: "direct"' in combined or "execution_mode: direct" in combined
    assert "default" in combined and "direct" in combined
    assert "preview" in combined and "somente" in combined and "explicit" in combined
    forbidden = [
        "toda operacao que altera anki exige dry_run",
        "use `dry_run: true` primeiro",
        "rode `dry_run: true`",
        "pedir confirmacao conjunta antes de qualquer `dry_run: false`",
    ]
    assert all(phrase not in combined for phrase in forbidden)


def test_atomic_json_write_leaves_valid_document(query_api, tmp_path):
    target = tmp_path / "state" / "index.json"
    query_api.atomic_write_json(target, {"version": 2, "items": [1, 2, 3]})
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "version": 2,
        "items": [1, 2, 3],
    }
    assert not (target.parent / ".index.json.tmp").exists()


def generation_objects(marker):
    return {
        "notes_index.json": {"1": {"note_id": 1, "marker": marker}},
        "decks_index.json": {
            "decks": [{"id": 10, "name": "Root"}, {"id": 11, "name": "Root::Child"}],
            "total_decks": 2,
            "total_cards": 3,
            "marker": marker,
        },
        "note_media_index.json": {"1": {"has_images": False}, "marker": marker},
        "snapshot_status.json": {"generated_at": marker, "total_notes": 1},
    }


def test_generation_manifest_uses_deck_rows_not_envelope_keys(query_api, tmp_path):
    state_store, _cache = generation_cache(query_api, tmp_path)
    manifest = state_store.publish_generation(
        tmp_path,
        generation_objects("counts"),
        metadata={"counts": {"decks": 4}},
    )
    assert manifest["counts"] == {
        "total_deck_count": 2,
        "indexed_deck_count": 2,
        "deck_partition_count": 1,
        "total_card_count": 3,
        "total_note_count": 1,
        "media_note_count": 2,
    }


def test_generation_rejects_declared_deck_count_mismatch(query_api, tmp_path):
    state_store, _cache = generation_cache(query_api, tmp_path)
    objects = generation_objects("mismatch")
    objects["decks_index.json"]["total_decks"] = 7
    with pytest.raises(ValueError, match="generation_total_deck_count_mismatch"):
        state_store.publish_generation(tmp_path, objects)


def generation_cache(query_api, state_dir):
    state_store = sys.modules[query_api.GenerationStateCache.__module__]
    legacy = {}
    for name, value in generation_objects("legacy").items():
        path = state_dir / name
        state_store.atomic_write_json(path, value)
        legacy[name] = path
    return state_store, state_store.GenerationStateCache(state_dir, legacy)


def test_transactional_generation_publish_cache_and_rollback(query_api, tmp_path):
    state_store, cache = generation_cache(query_api, tmp_path)
    old_manifest = state_store.publish_generation(tmp_path, generation_objects("old"))
    old_pointer = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    old_objects, loaded_manifest = cache.snapshot()
    assert old_objects["snapshot_status.json"]["generated_at"] == "old"
    assert loaded_manifest["generation_id"] == old_manifest["generation_id"]
    cache.snapshot()
    assert cache.metrics()["hits"] == 1

    new_manifest = state_store.publish_generation(tmp_path, generation_objects("new"))
    new_objects, loaded_manifest = cache.snapshot()
    assert new_objects["snapshot_status.json"]["generated_at"] == "new"
    assert loaded_manifest["generation_id"] == new_manifest["generation_id"]

    state_store.atomic_write_json(tmp_path / "current.json", old_pointer)
    rolled_back, loaded_manifest = cache.snapshot()
    assert rolled_back["snapshot_status.json"]["generated_at"] == "old"
    assert loaded_manifest["generation_id"] == old_manifest["generation_id"]


def configure_query_state_paths(query_api, monkeypatch, root):
    state = root / "state"
    data = root / "data"
    state.mkdir(parents=True)
    media = state / "media"
    media.mkdir()
    paths = {
        "notes_index.json": state / "notes_index.json",
        "decks_index.json": state / "decks_index.json",
        "note_media_index.json": state / "note_media_index.json",
        "snapshot_status.json": state / "snapshot_status.json",
    }
    monkeypatch.setattr(query_api, "STATE_DIR", state)
    monkeypatch.setattr(query_api, "DATA_DIR", data)
    monkeypatch.setattr(query_api, "MEDIA_DIR", media)
    monkeypatch.setattr(query_api, "NOTES_INDEX_PATH", paths["notes_index.json"])
    monkeypatch.setattr(query_api, "DECKS_INDEX_PATH", paths["decks_index.json"])
    monkeypatch.setattr(query_api, "NOTE_MEDIA_INDEX_PATH", paths["note_media_index.json"])
    monkeypatch.setattr(query_api, "SNAPSHOT_STATUS_PATH", paths["snapshot_status.json"])
    monkeypatch.setattr(query_api, "STATE_CACHE", query_api.GenerationStateCache(state, paths))
    return state, data, media


def test_full_snapshot_publish_activates_complete_generation(query_api, monkeypatch, tmp_path):
    state, data, media = configure_query_state_paths(query_api, monkeypatch, tmp_path)
    (media / "image.png").write_bytes(b"fixture")
    payload = {
        "generated_at": "2026-07-11T12:00:00+00:00",
        "timestamp": "2026-07-11T12:00:00+00:00",
        "snapshot_version": 2,
        "profile": "fixture",
        "total_notes": 1,
        "total_cards": 1,
        "total_decks": 1,
        "notes_with_images": 1,
        "decks": [{"deck_id": 10, "deck_name": "Fixture", "card_count": 1, "note_count": 1}],
        "notes": [{
            "note_id": 1,
            "fields": {"Text": '<img src="image.png">content'},
            "cards": [{"card_id": 2, "note_id": 1, "deck_id": 10, "deck_name": "Fixture"}],
        }],
    }
    response = query_api.publish_full_snapshot_payload(payload)
    assert response["ok"] is True
    assert response["index_schema_version"] == 3
    assert (state / "current.json").exists()
    notes, media_index = query_api.load_state()
    assert notes["1"]["note_id"] == 1
    assert media_index["1"]["resolved_media"] == ["image.png"]
    assert len(list(data.glob("*.json"))) == 1

    pointer = (state / "current.json").read_bytes()
    invalid = dict(payload)
    invalid["notes"] = [payload["notes"][0], payload["notes"][0]]
    invalid["total_notes"] = 2
    with pytest.raises(ValueError, match="snapshot_note_ids_invalid_or_duplicate"):
        query_api.publish_full_snapshot_payload(invalid)
    assert (state / "current.json").read_bytes() == pointer
    assert len(list(data.glob("*.json"))) == 1


def test_incomplete_or_corrupt_generation_never_replaces_verified_cache(query_api, tmp_path):
    state_store, cache = generation_cache(query_api, tmp_path)
    manifest = state_store.publish_generation(tmp_path, generation_objects("good"))
    good, _ = cache.snapshot()
    assert good["snapshot_status.json"]["generated_at"] == "good"

    generation_dir = tmp_path / "generations" / manifest["generation_id"]
    (generation_dir / "notes_index.json").write_text("{}", encoding="utf-8")
    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    current["nonce"] = "force-signature-change"
    state_store.atomic_write_json(tmp_path / "current.json", current)
    still_good, _ = cache.snapshot()
    assert still_good is good
    assert cache.metrics()["reload_failures"] == 1


def test_publish_failure_before_pointer_swap_keeps_current_generation(query_api, tmp_path, monkeypatch):
    state_store, cache = generation_cache(query_api, tmp_path)
    first = state_store.publish_generation(tmp_path, generation_objects("first"))
    pointer_before = (tmp_path / "current.json").read_bytes()
    original = state_store.canonical_json_bytes

    def fail_on_decks(value):
        if isinstance(value, dict) and value.get("marker") == "broken":
            raise RuntimeError("fixture_mid_publish_failure")
        return original(value)

    monkeypatch.setattr(state_store, "canonical_json_bytes", fail_on_decks)
    with pytest.raises(RuntimeError, match="fixture_mid_publish_failure"):
        state_store.publish_generation(tmp_path, generation_objects("broken"))
    assert (tmp_path / "current.json").read_bytes() == pointer_before
    objects, manifest = cache.snapshot()
    assert objects["snapshot_status.json"]["generated_at"] == "first"
    assert manifest["generation_id"] == first["generation_id"]


@pytest.mark.parametrize("error_number", [13, 28])
def test_generation_permission_or_disk_full_keeps_current_pointer(
    query_api, tmp_path, monkeypatch, error_number
):
    state_store, _cache = generation_cache(query_api, tmp_path)
    state_store.publish_generation(tmp_path, generation_objects("stable"))
    pointer_before = (tmp_path / "current.json").read_bytes()
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.name == "notes_index.json" and path.parent.name.startswith(".gen-"):
            raise OSError(error_number, "artificial filesystem failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    with pytest.raises(OSError) as raised:
        state_store.publish_generation(tmp_path, generation_objects("must-not-activate"))
    assert raised.value.errno == error_number
    assert (tmp_path / "current.json").read_bytes() == pointer_before
    assert not list((tmp_path / "generations").glob(".*.tmp"))


def test_fresh_process_recovers_previous_valid_generation(query_api, tmp_path):
    state_store, _cache = generation_cache(query_api, tmp_path)
    old = state_store.publish_generation(tmp_path, generation_objects("old"))
    new = state_store.publish_generation(tmp_path, generation_objects("new"))
    assert new["previous_generation_id"] == old["generation_id"]
    (tmp_path / "generations" / new["generation_id"] / "notes_index.json").write_text("{}", encoding="utf-8")

    legacy = {name: tmp_path / name for name in generation_objects("legacy")}
    fresh = state_store.GenerationStateCache(tmp_path, legacy)
    objects, manifest = fresh.snapshot()
    assert objects["snapshot_status.json"]["generated_at"] == "old"
    assert manifest["generation_id"] == old["generation_id"]
    assert manifest["recovered_from_invalid_pointer"] is True


def test_generation_retention_never_selects_active_or_previous(query_api, tmp_path):
    state_store, _cache = generation_cache(query_api, tmp_path)
    manifests = [
        state_store.publish_generation(tmp_path, generation_objects(f"g{index}"))
        for index in range(5)
    ]
    cleanup = load_module("cleanup_retention_audit", ROOT / "remote-backend" / "cleanup_retention.py")
    candidates, scanned = cleanup.generation_candidates(tmp_path, keep=3)
    names = {item.path.name for item in candidates}
    assert scanned == 5
    assert manifests[-1]["generation_id"] not in names
    assert manifests[-2]["generation_id"] not in names
    assert len(candidates) == 2


def test_retention_inventory_finds_sensitive_archives_backups_and_temporaries(tmp_path):
    cleanup = load_module("cleanup_retention_policy", ROOT / "remote-backend" / "cleanup_retention.py")
    base = tmp_path / "service"
    state = base / "state"
    (state / "generations").mkdir(parents=True)
    (base / "scripts").mkdir()
    compressed = base / "requests.log.1.gz"
    backup = base / "scripts" / "query_api.py.bak-20260101"
    temporary = state / "generations" / ".gen-fixture.tmp"
    compressed.write_bytes(b"fixture")
    backup.write_text("fixture", encoding="utf-8")
    temporary.mkdir()
    old = datetime.now(timezone.utc).timestamp() - 120 * 86400
    for path in (compressed, backup, temporary):
        os.utime(path, (old, old))

    archives, archive_scanned = cleanup.compressed_log_candidates(base, 30)
    backups, backup_scanned = cleanup.backup_candidates(base, 90)
    temporaries, temp_scanned = cleanup.temporary_candidates(base, state, 2)
    assert archive_scanned == backup_scanned == temp_scanned == 1
    assert [candidate.path for candidate in archives] == [compressed]
    assert [candidate.path for candidate in backups] == [backup]
    assert [candidate.path for candidate in temporaries] == [temporary]


def test_nginx_proposal_is_scoped_and_preserves_shared_vhost():
    proposal = REPO_ROOT / "ops" / "anki-api" / "nginx"
    default_deny = (proposal / "default-deny.conf").read_text(encoding="utf-8")
    gatsby = (proposal / "gatsby-anki.conf").read_text(encoding="utf-8")
    harness = (proposal / "test-nginx.conf").read_text(encoding="utf-8")
    assert default_deny.count("default_server") == 4
    assert "return 444" in default_deny
    assert "proxy_pass http://127.0.0.1:8767" in gatsby
    assert "8766" in gatsby and "proxy_pass http://127.0.0.1:8766" not in gatsby
    assert "Strict-Transport-Security" in gatsby and "limit_req zone=anki_gpt_all" in gatsby
    assert "include /etc/nginx/sites-available/n8n.gatssby.com.br;" in harness


def test_gitignore_covers_runtime_secrets_and_generated_state():
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for required in (
        ".env",
        "*.gz",
        "*.sqlite*",
        "backups",
        "state",
        "snapshots",
        "generations",
        "media",
        "__pycache__",
    ):
        assert required in ignore
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert not any(Path(path).name in {"tagging_token.txt", ".env"} for path in tracked)
    assert not any(path.startswith("files/") for path in tracked)


def test_ci_workflow_contains_offline_quality_gates():
    workflow = (REPO_ROOT / ".github" / "workflows" / "anki-gpt-ci.yml").read_text(encoding="utf-8")
    for command in (
        "py_compile",
        "pytest -q",
        "validate_openapi.py",
        "bash -n",
        "ruff check",
        "mypy",
        "bandit",
        "check_no_secrets.py",
    ):
        assert command in workflow
    for forbidden in ("tagging_token.txt", "collection.anki2", "anki gui", "git push"):
        assert forbidden not in workflow.casefold()


def test_concurrent_readers_observe_only_complete_generations(query_api, tmp_path):
    state_store, cache = generation_cache(query_api, tmp_path)
    state_store.publish_generation(tmp_path, generation_objects("g0"))
    observed = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            objects, _ = cache.snapshot()
            observed.append(objects["snapshot_status.json"]["generated_at"])

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(1, 5):
            state_store.publish_generation(tmp_path, generation_objects(f"g{index}"))
    finally:
        stop.set()
        thread.join(timeout=5)
    assert observed
    assert set(observed) <= {f"g{index}" for index in range(5)}


def test_derived_request_cache_reuses_and_invalidates_by_generation(query_api, monkeypatch):
    first_objects = generation_objects("derived-a")
    second_objects = generation_objects("derived-b")

    class FakeStateCache:
        current = (first_objects, {"generation_id": "gen-a"})

        def snapshot(self):
            return self.current

        def metrics(self):
            return {"generation_id": self.current[1]["generation_id"]}

    fake = FakeStateCache()
    monkeypatch.setattr(query_api, "STATE_CACHE", fake)
    monkeypatch.setitem(query_api.DERIVED_STATE_CACHE, "key", None)
    monkeypatch.setitem(query_api.DERIVED_STATE_CACHE, "value", None)
    monkeypatch.setitem(query_api.DERIVED_STATE_CACHE, "hits", 0)
    monkeypatch.setitem(query_api.DERIVED_STATE_CACHE, "misses", 0)

    first = query_api.load_derived_request_state()
    repeated = query_api.load_derived_request_state()
    assert repeated is first
    assert query_api.DERIVED_STATE_CACHE["hits"] == 1
    assert query_api.DERIVED_STATE_CACHE["misses"] == 1

    fake.current = (second_objects, {"generation_id": "gen-b"})
    second = query_api.load_derived_request_state()
    assert second is not first
    assert second["generation_id"] == "gen-b"
    assert query_api.DERIVED_STATE_CACHE["misses"] == 2


def test_operational_diagnostics_is_authenticated_shape_without_paths_or_tokens(query_api, monkeypatch, tmp_path):
    import sqlite3

    fts = tmp_path / "fts.sqlite"
    connection = sqlite3.connect(fts)
    connection.execute("create table documents(path text primary key)")
    connection.execute("insert into documents values ('artificial.md')")
    connection.commit()
    connection.close()
    tagging = tmp_path / "tagging"
    organization_dir = tmp_path / "organization"
    tagging.mkdir()
    organization_dir.mkdir()
    (tagging / "one.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(query_api, "FO_SEARCH_INDEX_PATH", fts)
    monkeypatch.setattr(query_api, "STATE_DIR", tmp_path)
    monkeypatch.setattr(query_api, "TAGGING_OPERATIONS_DIR", tagging)
    monkeypatch.setattr(query_api, "ORGANIZATION_OPERATIONS_DIR", organization_dir)
    monkeypatch.setattr(
        query_api,
        "load_derived_request_state",
        lambda: {
            "generation_id": "gen-artificial",
            "snapshot_status": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "total_decks": 2,
                "total_cards": 3,
                "total_notes": 1,
            },
        },
    )
    monkeypatch.setattr(
        query_api,
        "STATE_CACHE",
        types.SimpleNamespace(metrics=lambda: {"hits": 1, "misses": 1, "reload_failures": 0}),
    )
    diagnostics = query_api.operational_diagnostics()
    serialized = json.dumps(diagnostics, sort_keys=True).casefold()
    assert diagnostics["ok"] is True
    assert diagnostics["fts_documents"] == 1
    assert diagnostics["generation_id"] == "gen-artificial"
    assert "token" not in serialized
    assert str(tmp_path).casefold() not in serialized


def post_to_test_server(query_api, path, headers=None, body=b"{}"):
    server = query_api.ThreadingHTTPServer(("127.0.0.1", 0), query_api.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request("POST", path, body=body, headers=request_headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        return response.status, payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def post_to_test_server_with_headers(query_api, path, headers=None, body=b"{}"):
    server = query_api.ThreadingHTTPServer(("127.0.0.1", 0), query_api.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request("POST", path, body=body, headers=request_headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        return response.status, payload, dict(response.getheaders())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_sync_write_requires_valid_authentication(query_api, monkeypatch, tmp_path):
    token_file = tmp_path / "sync-token"
    token_file.write_text("fixture-token", encoding="utf-8")
    monkeypatch.setattr(query_api, "TAGGING_TOKEN_FILE", token_file)
    monkeypatch.delenv(query_api.TAGGING_TOKEN_ENV, raising=False)
    monkeypatch.setattr(
        query_api,
        "publish_full_snapshot_payload",
        lambda payload, request_path: {"ok": True, "received": payload, "path": request_path},
    )

    status, payload = post_to_test_server(query_api, "/sync/full")
    assert status == 401
    assert payload == {"error": "unauthorized"}

    status, payload = post_to_test_server(
        query_api,
        "/sync/full",
        headers={"X-Tagging-Token": "wrong-token"},
    )
    assert status == 401
    assert payload == {"error": "unauthorized"}

    status, payload = post_to_test_server(
        query_api,
        "/sync/full",
        headers={"X-Tagging-Token": "fixture-token"},
        body=b'{"decks": []}',
    )
    assert status == 200
    assert payload["ok"] is True


def test_duplicate_auth_headers_fail_closed(query_api, monkeypatch, tmp_path):
    token_file = tmp_path / "sync-token"
    token_file.write_text("fixture-token", encoding="utf-8")
    monkeypatch.setattr(query_api, "TAGGING_TOKEN_FILE", token_file)
    monkeypatch.delenv(query_api.TAGGING_TOKEN_ENV, raising=False)
    monkeypatch.setattr(query_api, "REQUIRE_READ_AUTH", True)
    server = query_api.ThreadingHTTPServer(("127.0.0.1", 0), query_api.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for first, second in (
            ("wrong-token", "fixture-token"),
            ("fixture-token", "wrong-token"),
        ):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.putrequest("GET", "/decks")
            connection.putheader("X-Tagging-Token", first)
            connection.putheader("X-Tagging-Token", second)
            connection.endheaders()
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
            assert response.status == 401
            assert payload == {"error": "unauthorized"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_sync_write_fails_closed_when_server_token_missing(query_api, monkeypatch, tmp_path):
    monkeypatch.setattr(query_api, "TAGGING_TOKEN_FILE", tmp_path / "missing-token")
    monkeypatch.delenv(query_api.TAGGING_TOKEN_ENV, raising=False)
    status, payload = post_to_test_server(query_api, "/sync/full")
    assert status == 503
    assert payload["error"] == "tagging_token_not_configured"


def test_sync_body_limit_is_checked_before_read(query_api):
    handler = types.SimpleNamespace(
        headers={
            "Content-Length": str(query_api.MAX_SYNC_BODY_BYTES + 1),
            "Content-Type": "application/json",
        },
        rfile=types.SimpleNamespace(read=lambda _length: pytest.fail("body must not be read")),
    )
    with pytest.raises(ValueError, match="request_body_too_large"):
        query_api.read_json_body(handler, max_bytes=query_api.MAX_SYNC_BODY_BYTES)


def test_json_body_requires_content_type_and_has_default_limit(query_api):
    wrong_type = types.SimpleNamespace(
        headers={"Content-Length": "2", "Content-Type": "text/plain"},
        rfile=types.SimpleNamespace(read=lambda _length: pytest.fail("wrong type must not be read")),
    )
    with pytest.raises(ValueError, match="unsupported_content_type"):
        query_api.read_json_body(wrong_type)

    oversized = types.SimpleNamespace(
        headers={
            "Content-Length": str(query_api.MAX_JSON_BODY_BYTES + 1),
            "Content-Type": "application/json",
        },
        rfile=types.SimpleNamespace(read=lambda _length: pytest.fail("oversized body must not be read")),
    )
    with pytest.raises(ValueError, match="request_body_too_large"):
        query_api.read_json_body(oversized)


def test_action_body_reader_uses_central_content_type_and_size_guards(query_api, monkeypatch, tmp_path):
    monkeypatch.setattr(query_api, "ACTION_LOG_PATH", tmp_path / "action.jsonl")
    wrong_type = types.SimpleNamespace(
        headers={"Content-Length": "2", "Content-Type": "text/plain"},
        rfile=types.SimpleNamespace(read=lambda _length: pytest.fail("wrong type must not be read")),
        command="POST",
    )
    with pytest.raises(ValueError, match="unsupported_content_type"):
        query_api.read_json_body_for_action(wrong_type, "/organization/operations")

    oversized = types.SimpleNamespace(
        headers={
            "Content-Length": str(query_api.MAX_JSON_BODY_BYTES + 1),
            "Content-Type": "application/json",
        },
        rfile=types.SimpleNamespace(read=lambda _length: pytest.fail("oversized body must not be read")),
        command="POST",
    )
    with pytest.raises(ValueError, match="request_body_too_large"):
        query_api.read_json_body_for_action(oversized, "/organization/operations")


def test_public_health_remains_readable(query_api):
    server = query_api.ThreadingHTTPServer(("127.0.0.1", 0), query_api.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/health")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert payload["ok"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get_from_test_server(query_api, path, headers=None):
    server = query_api.ThreadingHTTPServer(("127.0.0.1", 0), query_api.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return response.status, body, dict(response.getheaders())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_sensitive_reads_require_auth_but_version_is_public(query_api, monkeypatch, tmp_path):
    token_file = tmp_path / "read-token"
    token_file.write_text("fixture-token", encoding="utf-8")
    monkeypatch.setattr(query_api, "TAGGING_TOKEN_FILE", token_file)
    monkeypatch.delenv(query_api.TAGGING_TOKEN_ENV, raising=False)
    monkeypatch.setattr(query_api, "ACTION_LOG_PATH", tmp_path / "action.jsonl")
    monkeypatch.setattr(query_api, "REQUIRE_READ_AUTH", True)

    status, body, _ = get_from_test_server(query_api, "/decks")
    assert status == 401
    assert json.loads(body) == {"error": "unauthorized"}

    status, body, _ = get_from_test_server(query_api, "/version")
    assert status == 200
    assert json.loads(body)["api_version"] == query_api.API_VERSION

    status, body, _ = get_from_test_server(
        query_api,
        "/debug/action-log?limit=1",
        headers={"X-Tagging-Token": "fixture-token"},
    )
    assert status == 200
    assert json.loads(body)["events"] == []


def test_compact_openapi_route_is_public_read_only_and_not_cached(
    query_api, monkeypatch, tmp_path
):
    compact_path = REPO_ROOT / "contracts" / "openapi" / "gpt-action-compact.openapi.json"
    token_file = tmp_path / "route-test-token"
    token_file.write_text("fixture-token", encoding="utf-8")
    monkeypatch.setattr(query_api, "GPT_COMPACT_SCHEMA_PATH", compact_path)
    monkeypatch.setattr(query_api, "TAGGING_TOKEN_FILE", token_file)
    monkeypatch.delenv(query_api.TAGGING_TOKEN_ENV, raising=False)
    monkeypatch.setattr(query_api, "REQUIRE_READ_AUTH", True)

    status, body, headers = get_from_test_server(query_api, "/openapi/gpt.json")
    normalized_headers = {name.casefold(): value for name, value in headers.items()}
    assert status == 200
    assert body == compact_path.read_bytes()
    assert normalized_headers["content-type"] == "application/json; charset=utf-8"
    assert normalized_headers["cache-control"] == "no-store, max-age=0"
    assert normalized_headers["pragma"] == "no-cache"
    assert "set-cookie" not in normalized_headers

    status, body, _ = get_from_test_server(query_api, "/decks")
    assert status == 401
    assert json.loads(body) == {"error": "unauthorized"}


def test_mutating_get_wrappers_are_deprecated_with_post_successor_headers(query_api, monkeypatch, tmp_path):
    token_file = tmp_path / "read-token"
    token_file.write_text("fixture-token", encoding="utf-8")
    monkeypatch.setattr(query_api, "TAGGING_TOKEN_FILE", token_file)
    monkeypatch.delenv(query_api.TAGGING_TOKEN_ENV, raising=False)
    monkeypatch.setattr(query_api, "ACTION_LOG_PATH", tmp_path / "action.jsonl")
    monkeypatch.setattr(query_api, "REQUIRE_READ_AUTH", True)

    status, body, headers = get_from_test_server(
        query_api,
        "/organization/reorder-order-create",
        headers={"X-Tagging-Token": "fixture-token"},
    )
    assert status == 400
    assert json.loads(body)["error"] == "deck_empty"
    assert headers["Deprecation"] == "true"
    assert headers["Sunset"] == "Thu, 01 Oct 2026 00:00:00 GMT"
    assert headers["Link"] == '</organization/reorder-order>; rel="successor-version"'


def test_post_aliases_materialize_only_artificial_fixture_state(query_api, monkeypatch, tmp_path):
    token_file = tmp_path / "write-token"
    token_file.write_text("fixture-token", encoding="utf-8")
    updates_dir = tmp_path / "note-field-updates"
    operations_dir = tmp_path / "operations"
    orders_dir = tmp_path / "orders"
    for path in (updates_dir, operations_dir, orders_dir):
        path.mkdir()
    monkeypatch.setattr(query_api, "TAGGING_TOKEN_FILE", token_file)
    monkeypatch.delenv(query_api.TAGGING_TOKEN_ENV, raising=False)
    monkeypatch.setattr(query_api, "ACTION_LOG_PATH", tmp_path / "action.jsonl")
    monkeypatch.setattr(query_api, "ORGANIZATION_NOTE_FIELD_UPDATES_DIR", updates_dir)
    monkeypatch.setattr(query_api, "ORGANIZATION_OPERATIONS_DIR", operations_dir)
    monkeypatch.setattr(query_api, "ORGANIZATION_REORDER_ORDERS_DIR", orders_dir)

    headers = {"X-Tagging-Token": "fixture-token"}
    note_updates_json = json.dumps([
        {"note_id": 1, "fields": {"Text": "fixture only"}},
    ])
    status, materialized, _ = post_to_test_server_with_headers(
        query_api,
        "/organization/note-field-updates-create",
        headers=headers,
        body=json.dumps({"note_updates_json": note_updates_json}).encode(),
    )
    assert status == 200
    assert materialized["note_updates_count"] == 1
    assert len(list(updates_dir.glob("*.json"))) == 1

    status, created, _ = post_to_test_server_with_headers(
        query_api,
        "/organization/update-note-fields-create",
        headers=headers,
        body=json.dumps({
            "updates_id": materialized["updates_id"],
            "dry_run": True,
            "confirmed_by_user": True,
        }).encode(),
    )
    assert status == 200
    assert created["operation"]["operation_type"] == "update_note_fields"
    assert created["operation"]["status"] == "pending"
    assert len(list(operations_dir.glob("*.json"))) == 1

    status, order, _ = post_to_test_server_with_headers(
        query_api,
        "/organization/reorder-order-create",
        headers=headers,
        body=json.dumps({
            "deck": "Fixture Deck",
            "ordered_note_ids": [1, 2],
            "expected_eligible_card_ids": [10, 20],
            "target_created_column": "note",
        }).encode(),
    )
    assert status == 200
    assert order["ordered_note_ids_count"] == 2
    assert len(list(orders_dir.glob("*.json"))) == 1

    monkeypatch.setattr(query_api, "validate_global_note_reorder_payload", lambda _payload: None)
    status, reorder_operation, _ = post_to_test_server_with_headers(
        query_api,
        "/organization/reorder-cards-by-material-create",
        headers=headers,
        body=json.dumps({
            "deck": "Fixture Deck",
            "order_id": order["order_id"],
            "dry_run": True,
            "confirmed_by_user": True,
        }).encode(),
    )
    assert status == 200
    assert reorder_operation["operation"]["operation_type"] == "reorder_cards_by_material"
    assert len(list(operations_dir.glob("*.json"))) == 2


def test_http_creation_without_mode_enters_pending_queue_as_direct(
    query_api, monkeypatch, tmp_path
):
    token_file = tmp_path / "write-token"
    token_file.write_text("fixture-token", encoding="utf-8")
    operations_dir = tmp_path / "operations"
    operations_dir.mkdir()
    monkeypatch.setattr(query_api, "TAGGING_TOKEN_FILE", token_file)
    monkeypatch.delenv(query_api.TAGGING_TOKEN_ENV, raising=False)
    monkeypatch.setattr(query_api, "ACTION_LOG_PATH", tmp_path / "action.jsonl")
    monkeypatch.setattr(query_api, "ORGANIZATION_OPERATIONS_DIR", operations_dir)
    headers = {"X-Tagging-Token": "fixture-token"}

    status, created, _ = post_to_test_server_with_headers(
        query_api,
        "/organization/update-note-fields",
        headers=headers,
        body=json.dumps({
            "note_updates": [{
                "note_id": 1,
                "fields": {"Text": "artificial"},
                "expected_content_hash": "a" * 64,
            }],
        }).encode(),
    )
    assert status == 200
    operation = created["operation"]
    assert operation["status"] == "pending"
    assert operation["execution_mode"] == "direct"
    assert operation["payload"]["dry_run"] is False
    assert operation["confirmed_by_user"] is False
    assert created["default_execution_mode"] == "direct"

    get_status, body, _ = get_from_test_server(
        query_api,
        "/organization/operations?status=pending",
        headers=headers,
    )
    assert get_status == 200
    list_response = json.loads(body)
    assert list_response["default_execution_mode"] == "direct"
    queued = list_response["operations"]
    assert [item["operation_id"] for item in queued] == [operation["operation_id"]]
    assert queued[0]["execution_mode"] == "direct"


def test_apply_v2_full_updates_id_flow_uses_artificial_notes_only(
    query_api, organization, monkeypatch, tmp_path
):
    token_file = tmp_path / "write-token"
    token_file.write_text("fixture-token", encoding="utf-8")
    updates_dir = tmp_path / "note-field-updates"
    operations_dir = tmp_path / "operations"
    updates_dir.mkdir()
    operations_dir.mkdir()
    monkeypatch.setattr(query_api, "TAGGING_TOKEN_FILE", token_file)
    monkeypatch.delenv(query_api.TAGGING_TOKEN_ENV, raising=False)
    monkeypatch.setattr(query_api, "ACTION_LOG_PATH", tmp_path / "action.jsonl")
    monkeypatch.setattr(query_api, "ORGANIZATION_NOTE_FIELD_UPDATES_DIR", updates_dir)
    monkeypatch.setattr(query_api, "ORGANIZATION_OPERATIONS_DIR", operations_dir)
    headers = {"X-Tagging-Token": "fixture-token"}

    notes = [
        FakeAnkiNote(101, {"Text": "before one", "Back Extra": "extra one"}),
        FakeAnkiNote(102, {"Text": "before two", "Back Extra": "extra two"}),
        FakeAnkiNote(103, {"Text": "before three", "Back Extra": "extra three"}),
    ]
    configure_fake_notes(organization, monkeypatch, notes)
    organization.mw.pm = types.SimpleNamespace(name="artificial-fixture")
    monkeypatch.setattr(
        organization,
        "load_remote_note_field_updates",
        lambda updates_id: query_api.load_note_field_updates(updates_id),
    )

    requested_updates = [
        {"note_id": note.id, "fields": {"Text": f"after {note.id}"}}
        for note in notes
    ]
    status, source_batch, _ = post_to_test_server_with_headers(
        query_api,
        "/organization/note-field-updates-create",
        headers=headers,
        body=json.dumps({"note_updates_json": json.dumps(requested_updates)}).encode(),
    )
    assert status == 200

    status, dry_run_created, _ = post_to_test_server_with_headers(
        query_api,
        "/organization/update-note-fields-create",
        headers=headers,
        body=json.dumps({
            "updates_id": source_batch["updates_id"],
            "dry_run": True,
            "confirmed_by_user": True,
        }).encode(),
    )
    assert status == 200
    dry_run_operation = dry_run_created["operation"]
    dry_run_execution = organization.execute_organization_operation(dry_run_operation)
    assert dry_run_execution["ok"] is True
    assert dry_run_execution["result"]["changed_count"] == 3
    assert len(dry_run_execution["result"]["apply_preconditions"]) == 3
    assert [note["Text"] for note in notes] == [
        "before one",
        "before two",
        "before three",
    ]

    compact_confirmation = organization.compact_organization_result_for_storage(
        dry_run_execution
    )
    status, confirmed = post_to_test_server(
        query_api,
        "/organization/operations/confirm",
        headers=headers,
        body=json.dumps(compact_confirmation).encode(),
    )
    assert status == 200
    assert confirmed["operation"]["result"]["apply_preconditions"] == compact_confirmation["result"]["apply_preconditions"]

    status, body, _ = get_from_test_server(
        query_api,
        "/organization/operations?status=all",
        headers=headers,
    )
    assert status == 200
    public_operations = json.loads(body)["operations"]
    public_dry_run = next(
        item for item in public_operations
        if item["operation_id"] == dry_run_operation["operation_id"]
    )
    assert len(public_dry_run["result"]["apply_preconditions"]) == 3

    status, apply_batch, _ = post_to_test_server_with_headers(
        query_api,
        "/organization/note-field-updates-create",
        headers=headers,
        body=json.dumps({
            "dry_run_operation_id": dry_run_operation["operation_id"],
        }).encode(),
    )
    assert status == 200
    assert apply_batch["ready_for_apply_v2"] is True
    assert apply_batch["apply_updates_id"] == apply_batch["updates_id"]
    assert apply_batch["source_updates_id"] == source_batch["updates_id"]
    conditioned_updates = query_api.load_note_field_updates(
        apply_batch["updates_id"]
    )["note_updates"]
    assert [item["fields"] for item in conditioned_updates] == [
        item["fields"] for item in requested_updates
    ]
    assert all(item.get("expected_content_hash") for item in conditioned_updates)

    status, apply_created, _ = post_to_test_server_with_headers(
        query_api,
        "/organization/update-note-fields-create",
        headers=headers,
        body=json.dumps({
            "updates_id": apply_batch["updates_id"],
            "dry_run": False,
            "confirmed_by_user": True,
        }).encode(),
    )
    assert status == 200
    apply_execution = organization.execute_organization_operation(
        apply_created["operation"]
    )
    assert apply_execution["ok"] is True
    assert apply_execution["result"]["changed_count"] == 3
    assert apply_execution["result"]["errors"] == []
    assert [note["Text"] for note in notes] == [
        "after 101",
        "after 102",
        "after 103",
    ]


def addon_function_namespace(function_names, **values):
    source_path = ROOT / "addon-local" / "__init__.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in set(function_names)
    ]
    assert {node.name for node in functions} == set(function_names)
    namespace = dict(values)
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace


def test_auto_publish_policy_defaults_conservatively_and_respects_pause(tmp_path):
    policy_file = tmp_path / "auto_publish_policy.json"
    pause_file = tmp_path / "pause_auto_publish"
    events = []
    namespace = addon_function_namespace(
        {"read_json_file", "load_auto_publish_policy", "publication_decision"},
        Path=Path,
        json=json,
        log=events.append,
        AUTO_PUBLISH_POLICY_FILE=policy_file,
        AUTO_PUBLISH_PAUSE_FILE=pause_file,
        AUTO_PUBLISH_MODES={"disabled", "manual", "after_anki_sync", "always"},
        DEFAULT_AUTO_PUBLISH_MODE="manual",
    )
    decide = namespace["publication_decision"]
    assert decide("initialization") == {
        "mode": "manual",
        "configured": False,
        "allowed": False,
        "reason": "policy_not_enabled_for_trigger",
    }
    assert decide("manual")["allowed"] is True
    assert decide("anki_sync_did_finish")["allowed"] is False

    policy_file.write_text('{"mode":"after_anki_sync"}', encoding="utf-8")
    assert decide("anki_sync_did_finish")["allowed"] is True
    pause_file.touch()
    assert decide("manual")["reason"] == "pause_auto_publish"
    assert decide("anki_sync_did_finish")["allowed"] is False

    pause_file.unlink()
    policy_file.write_text('{"mode":"disabled"}', encoding="utf-8")
    assert decide("manual")["reason"] == "policy_disabled"
    policy_file.write_text('{"mode":"always"}', encoding="utf-8")
    assert decide("initialization")["reason"] == "explicit_always"


def test_runtime_diagnostics_reflects_pause_filesystem_and_is_resilient(tmp_path):
    policy_file = tmp_path / "auto_publish_policy.json"
    pause_file = tmp_path / "pause_auto_publish"
    runtime_file = tmp_path / "addon_runtime.json"
    module_file = tmp_path / "addon.py"
    organization_file = tmp_path / "organization.py"
    module_file.write_text("addon fixture", encoding="utf-8")
    organization_file.write_text("organization fixture", encoding="utf-8")
    events = []
    namespace = addon_function_namespace(
        {
            "read_json_file",
            "atomic_write_json",
            "load_auto_publish_policy",
            "save_auto_publish_policy",
            "auto_publish_runtime_state",
            "set_auto_publish_pause",
            "file_sha256",
            "write_addon_runtime_diagnostics",
        },
        __file__=str(module_file),
        Path=Path,
        json=json,
        hashlib=hashlib,
        log=events.append,
        AUTO_PUBLISH_POLICY_FILE=policy_file,
        AUTO_PUBLISH_PAUSE_FILE=pause_file,
        ADDON_RUNTIME_DIAGNOSTICS_FILE=runtime_file,
        AUTO_PUBLISH_MODES={"disabled", "manual", "after_anki_sync", "always"},
        DEFAULT_AUTO_PUBLISH_MODE="manual",
        now_iso=lambda: "2026-07-12T00:01:00+00:00",
        ADDON_VERSION="3.0.0",
        ADDON_LOADED_AT="2026-07-12T00:00:00+00:00",
        MENU_REGISTERED=True,
        organization_module=types.SimpleNamespace(__file__=str(organization_file)),
        LAST_CONFIRMED_GENERATION_ID="gen-fixture",
    )

    write_diag = namespace["write_addon_runtime_diagnostics"]
    set_pause = namespace["set_auto_publish_pause"]

    policy_file.write_text('{"version":1,"mode":"manual"}', encoding="utf-8")
    write_diag("initial")
    payload = json.loads(runtime_file.read_text(encoding="utf-8"))
    assert payload["auto_publish_mode"] == "manual"
    assert payload["auto_publish_configured"] is True
    assert payload["auto_publish_policy"] == "manual"
    assert payload["auto_publish_policy_configured"] is True
    assert payload["auto_publish_paused"] is False
    assert payload["last_confirmed_generation_id"] == "gen-fixture"
    assert payload["loaded_at"] == "2026-07-12T00:00:00+00:00"
    assert payload["module_hash"] == hashlib.sha256(module_file.read_bytes()).hexdigest()
    assert payload["organization_module_hash"] == hashlib.sha256(organization_file.read_bytes()).hexdigest()

    assert set_pause(True, "pause_created") is True
    payload = json.loads(runtime_file.read_text(encoding="utf-8"))
    assert payload["auto_publish_mode"] == "manual"
    assert payload["auto_publish_paused"] is True
    assert runtime_file.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("addon_runtime.json.tmp*"))

    assert set_pause(False, "pause_removed") is True
    payload = json.loads(runtime_file.read_text(encoding="utf-8"))
    assert payload["auto_publish_mode"] == "manual"
    assert payload["auto_publish_paused"] is False
    json.loads(runtime_file.read_text(encoding="utf-8"))

    namespace["save_auto_publish_policy"]("manual")
    payload = json.loads(runtime_file.read_text(encoding="utf-8"))
    assert payload["auto_publish_mode"] == "manual"
    assert payload["auto_publish_configured"] is True

    def fail_write(_path, _payload):
        raise OSError("fixture write failure")

    namespace["atomic_write_json"] = fail_write
    write_diag("write_failure")
    assert any("addon runtime diagnostics write failed" in event for event in events)


def test_snapshot_publish_requires_generation_confirmation_and_pause_precedes_network(tmp_path):
    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.body

    calls = []
    pause_file = tmp_path / "pause_auto_publish"
    namespace = addon_function_namespace(
        {"post_snapshot_payload_result", "post_snapshot_payload"},
        AUTO_PUBLISH_PAUSE_FILE=pause_file,
        LAST_POSTED_SNAPSHOT_HASH="",
        LAST_CONFIRMED_GENERATION_ID="",
        load_tagging_token=lambda: "fixture-token",
        log=lambda message: calls.append(("log", message)),
        snapshot_content_hash=lambda _payload: "fixture-hash",
        json=json,
        Request=lambda *args, **kwargs: (args, kwargs),
        SYNC_URL="https://example.invalid/sync/full",
        time=types.SimpleNamespace(perf_counter=lambda: 1.0),
        duration_ms=lambda _started: 1,
        HTTPError=RuntimeError,
        URLError=RuntimeError,
        urlopen=lambda *_args, **_kwargs: Response(b'{"generation_id":"gen-fixture"}'),
    )
    result = namespace["post_snapshot_payload_result"]({"fixture": True}, "fixture")
    assert result == {
        "ok": True,
        "cause": "",
        "status": 200,
        "generation_id": "gen-fixture",
    }
    assert namespace["post_snapshot_payload"]({"fixture": True}, "fixture") is True
    assert namespace["LAST_CONFIRMED_GENERATION_ID"] == "gen-fixture"

    namespace["urlopen"] = lambda *_args, **_kwargs: Response(b'{"ok":true}')
    result = namespace["post_snapshot_payload_result"]({"fixture": True}, "fixture")
    assert result["ok"] is False
    assert result["cause"] == "invalid_response"

    pause_file.touch()
    namespace["urlopen"] = lambda *_args, **_kwargs: pytest.fail("network must not run while paused")
    result = namespace["post_snapshot_payload_result"]({"fixture": True}, "fixture")
    assert result == {"ok": False, "cause": "auto_publish_paused"}


def test_addon_token_loader_uses_canonical_file_and_environment_precedence(
    tmp_path, monkeypatch
):
    token_file = tmp_path / "tagging_token.txt"
    token_file.write_text("fixture-file-token\n", encoding="utf-8")
    events = []
    namespace = addon_function_namespace(
        {"load_tagging_token"},
        os=os,
        TAGGING_TOKEN_ENV="ANKI_GPT_TAGGING_TOKEN_FIXTURE",
        TAGGING_TOKEN_FILE=token_file,
        log=events.append,
    )

    monkeypatch.delenv("ANKI_GPT_TAGGING_TOKEN_FIXTURE", raising=False)
    assert namespace["load_tagging_token"]() == "fixture-file-token"

    monkeypatch.setenv("ANKI_GPT_TAGGING_TOKEN_FIXTURE", "fixture-env-token")
    assert namespace["load_tagging_token"]() == "fixture-env-token"
    assert events == []


def test_addon_token_loader_rejects_missing_empty_and_multiline_files(
    tmp_path, monkeypatch
):
    token_file = tmp_path / "tagging_token.txt"
    events = []
    namespace = addon_function_namespace(
        {"load_tagging_token"},
        os=os,
        TAGGING_TOKEN_ENV="ANKI_GPT_TAGGING_TOKEN_FIXTURE",
        TAGGING_TOKEN_FILE=token_file,
        log=events.append,
    )
    monkeypatch.delenv("ANKI_GPT_TAGGING_TOKEN_FIXTURE", raising=False)

    assert namespace["load_tagging_token"]() == ""
    token_file.write_text("", encoding="utf-8")
    assert namespace["load_tagging_token"]() == ""
    token_file.write_text("first-line\nsecond-line\n", encoding="utf-8")
    assert namespace["load_tagging_token"]() == ""
    assert len([event for event in events if "token file invalid" in event]) == 2


def test_snapshot_authentication_rejection_preserves_cause_without_logging_secret(tmp_path):
    class RejectedError(Exception):
        code = 401
        reason = "Unauthorized"

        @staticmethod
        def read():
            return b'{"error":"unauthorized"}'

    class NetworkError(Exception):
        pass

    secret = "fixture-secret-that-must-never-be-logged"
    events = []
    namespace = addon_function_namespace(
        {"post_snapshot_payload_result"},
        AUTO_PUBLISH_PAUSE_FILE=tmp_path / "pause_auto_publish",
        LAST_POSTED_SNAPSHOT_HASH="",
        LAST_CONFIRMED_GENERATION_ID="",
        load_tagging_token=lambda: secret,
        log=events.append,
        snapshot_content_hash=lambda _payload: "fixture-hash",
        json=json,
        Request=lambda *args, **kwargs: (args, kwargs),
        SYNC_URL="https://example.invalid/sync/full",
        time=types.SimpleNamespace(perf_counter=lambda: 1.0),
        duration_ms=lambda _started: 1,
        HTTPError=RejectedError,
        URLError=NetworkError,
        urlopen=lambda *_args, **_kwargs: (_ for _ in ()).throw(RejectedError()),
    )

    result = namespace["post_snapshot_payload_result"]({"fixture": True}, "fixture")

    assert result == {"ok": False, "cause": "authentication_rejected", "status": 401}
    assert secret not in "\n".join(events)
    assert any("cause=authentication_rejected" in event for event in events)


def test_manual_missing_authentication_message_is_clear_and_canonical():
    canonical_token = (
        Path.home()
        / "Library"
        / "Application Support"
        / "Anki2"
        / "addon-data"
        / "anki_gpt_sync"
        / "tagging_token.txt"
    )
    namespace = addon_function_namespace(
        {"authentication_required_message"},
        TAGGING_TOKEN_ENV="ANKI_GPT_TAGGING_TOKEN",
        TAGGING_TOKEN_FILE=canonical_token,
    )

    message = namespace["authentication_required_message"]()

    assert message.startswith("Autenticação ausente.")
    assert "ANKI_GPT_TAGGING_TOKEN" in message
    assert str(canonical_token) in message
    assert "anki-gpt-files" not in message


def test_manual_combined_sync_missing_token_never_reads_collection_or_uses_generic_error():
    messages = []
    events = []
    canonical_token = (
        Path.home()
        / "Library"
        / "Application Support"
        / "Anki2"
        / "addon-data"
        / "anki_gpt_sync"
        / "tagging_token.txt"
    )
    namespace = addon_function_namespace(
        {"authentication_required_message", "sync_everything_now"},
        COMBINED_SYNC_IN_FLIGHT=False,
        publication_decision=lambda _trigger: {
            "allowed": True,
            "mode": "manual",
            "reason": "manual_request",
        },
        load_tagging_token=lambda: "",
        log=events.append,
        showInfo=messages.append,
        TAGGING_TOKEN_ENV="ANKI_GPT_TAGGING_TOKEN",
        TAGGING_TOKEN_FILE=canonical_token,
    )

    namespace["sync_everything_now"]()

    assert namespace["COMBINED_SYNC_IN_FLIGHT"] is False
    assert messages == [namespace["authentication_required_message"]()]
    assert "Erro inesperado" not in messages[0]
    assert "initial_snapshot_upload_failed" not in messages[0]
    assert any("cause=missing_authentication_token" in event for event in events)


def test_automatic_sync_missing_token_logs_without_popup_or_collection_access():
    events = []
    namespace = addon_function_namespace(
        {"on_sync_did_finish"},
        log=events.append,
        publication_decision=lambda _trigger: {
            "allowed": True,
            "mode": "after_anki_sync",
            "configured": True,
            "reason": "after_anki_sync",
        },
        post_full_snapshot=lambda _reason: False,
    )

    namespace["on_sync_did_finish"]()

    assert any("hook sync_did_finish" in event for event in events)
    assert "showInfo" not in namespace


def test_combined_snapshot_failure_preserves_real_cause_and_canonical_log_path():
    canonical_log = (
        Path.home()
        / "Library"
        / "Application Support"
        / "Anki2"
        / "addon-data"
        / "anki_gpt_sync"
        / "logs"
        / "anki_gpt_sync.log"
    )
    namespace = addon_function_namespace(
        {
            "authentication_required_message",
            "snapshot_failure_message",
            "combined_snapshot_failure",
        },
        TAGGING_TOKEN_ENV="ANKI_GPT_TAGGING_TOKEN",
        TAGGING_TOKEN_FILE=canonical_log.parent.parent / "tagging_token.txt",
        LOG_FILE=canonical_log,
    )

    failure = namespace["combined_snapshot_failure"](
        "initial",
        {"ok": False, "cause": "authentication_rejected", "status": 401},
    )

    assert failure["error"] == "initial_snapshot_upload_failed"
    assert failure["failure_cause"] == "authentication_rejected"
    assert failure["upload_result"]["status"] == 401
    assert str(canonical_log.parent.parent / "tagging_token.txt") in failure["lines"][0]
    assert "anki-gpt-files" not in json.dumps(failure)

    network_failure = namespace["combined_snapshot_failure"](
        "initial",
        {"ok": False, "cause": "network_error"},
    )
    assert str(canonical_log) in network_failure["lines"][0]
    assert "anki-gpt-files" not in network_failure["lines"][0]


def test_active_addon_sources_have_no_legacy_runtime_dependency():
    active_paths = (
        ROOT / "addon-local" / "__init__.py",
        ROOT / "addon-local" / "organization.py",
        ROOT / "local-tools" / "anki_publish.sh",
    )
    for path in active_paths:
        source = path.read_text(encoding="utf-8")
        assert "anki-gpt-files" not in source
        assert "/Users/gatsby/anki-gpt-files" not in source


def test_media_publish_smoke_test_uses_canonical_token_without_command_argument():
    script = (ROOT / "local-tools" / "anki_publish.sh").read_text(encoding="utf-8")
    rebuild_script = (ROOT.parents[1] / "services" / "anki-api" / "rebuild_state.sh").read_text(
        encoding="utf-8"
    )

    assert 'ANKI_GPT_TOKEN_FILE="${ANKI_GPT_TOKEN_FILE:-$LOCAL_BASE/tagging_token.txt}"' in script
    assert 'os.environ.get("ANKI_GPT_TAGGING_TOKEN", "").strip()' in script
    assert 'headers={"X-Tagging-Token": token}' in script
    assert "/decks?limit=1" in script
    assert "curl -fsS https://gatsby-anki.137.131.191.66.nip.io/roots" not in script
    assert 'Path("$BASE/tagging_token.txt")' in rebuild_script
    assert 'headers={"X-Tagging-Token": token}' in rebuild_script
    assert "curl -fsS http://127.0.0.1:8767" not in rebuild_script

    authentication_docs = (ROOT / "docs" / "AUTHENTICATION.md").read_text(encoding="utf-8")
    assert "ambiente tem precedência" in authentication_docs


def test_auto_publish_guards_cover_hook_manual_snapshot_and_media():
    tree = ast.parse((ROOT / "addon-local" / "__init__.py").read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    hook = functions["on_sync_did_finish"]
    hook_calls = [
        node.func.id
        for node in ast.walk(hook)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "publication_decision" in hook_calls
    assert hook_calls.index("publication_decision") < hook_calls.index("post_full_snapshot")

    for name in ("sync_full_snapshot_now", "sync_everything_now"):
        decisions = [
            node for node in ast.walk(functions[name])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "publication_decision"
        ]
        assert len(decisions) == 1
        assert isinstance(decisions[0].args[0], ast.Constant)
        assert decisions[0].args[0].value == "manual"
    assert "AUTO_PUBLISH_PAUSE_FILE.exists()" in ast.unparse(functions["run_media_publish_script"])
    assert "AUTO_PUBLISH_PAUSE_FILE.exists()" in ast.unparse(
        functions["post_snapshot_payload_result"]
    )


def test_normal_search_cache_is_equivalent_reused_and_generation_scoped(query_api, monkeypatch):
    notes = [
        {
            "note_id": 1,
            "compare_text": "Ação constitucional",
            "fields": {"Text": "Água e cidadania", "Back Extra": "Fixture"},
            "tags": ["fixture"],
            "cards": [{"card_id": 10, "note_id": 1, "deck_id": 100, "deck_name": "Fixture"}],
        },
        {
            "note_id": 2,
            "compare_text": "Biologia",
            "fields": {"Text": "Célula procarionte"},
            "tags": [],
            "cards": [
                {"card_id": 20, "note_id": 2, "deck_id": 100, "deck_name": "Fixture"},
                {"card_id": 21, "note_id": 2, "deck_id": 100, "deck_name": "Fixture"},
            ],
        },
    ]
    monkeypatch.setattr(query_api, "NORMAL_SEARCH_CACHE", {
        "key": None,
        "value": None,
        "hits": 0,
        "misses": 0,
    })
    monkeypatch.setattr(query_api, "NORMAL_SEARCH_CACHE_LOCK", threading.RLock())
    cache = query_api.load_normal_search_cache(notes, "gen-a")
    assert query_api.load_normal_search_cache(notes, "gen-a") is cache
    assert query_api.NORMAL_SEARCH_CACHE["hits"] == 1
    assert query_api.NORMAL_SEARCH_CACHE["misses"] == 1

    for query in ("acao", "água", "procar", "inexistente"):
        params = {"q": [query]}
        fallback, _ = query_api.find_matching_cards_from_snapshot(notes, params)
        cached, _ = query_api.find_matching_cards_from_snapshot(
            notes,
            params,
            normalized_note_search=cache,
        )
        assert [card["card_id"] for card, _note in fallback] == [
            card["card_id"] for card, _note in cached
        ]

    next_notes = [dict(note) for note in notes]
    next_cache = query_api.load_normal_search_cache(next_notes, "gen-b")
    assert next_cache is not cache
    assert query_api.NORMAL_SEARCH_CACHE["misses"] == 2
    assert len(next_cache) == len(next_notes)


def test_media_rejects_traversal_and_symlink(query_api, monkeypatch, tmp_path):
    token_file = tmp_path / "read-token"
    token_file.write_text("fixture-token", encoding="utf-8")
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "ok.txt").write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    (media_dir / "link.txt").symlink_to(outside)
    monkeypatch.setattr(query_api, "TAGGING_TOKEN_FILE", token_file)
    monkeypatch.setattr(query_api, "MEDIA_DIR", media_dir)
    monkeypatch.setattr(query_api, "REQUIRE_READ_AUTH", True)
    monkeypatch.delenv(query_api.TAGGING_TOKEN_ENV, raising=False)
    headers = {"X-Tagging-Token": "fixture-token"}

    status, body, _ = get_from_test_server(query_api, "/media/ok.txt", headers=headers)
    assert status == 200
    assert body == b"safe"
    status, body, _ = get_from_test_server(query_api, "/media/link.txt", headers=headers)
    assert status == 404
    status, body, _ = get_from_test_server(query_api, "/media/..%2Foutside.txt", headers=headers)
    assert status == 400
    monkeypatch.setattr(query_api, "REQUIRE_READ_AUTH", False)
    status, body, _ = get_from_test_server(query_api, "/media/ok.txt")
    assert status == 200


def test_logs_redact_content_and_rotate(query_api, organization, monkeypatch, tmp_path):
    shape = query_api.redact_json_shape({"Text": "sensitive card content", "count": 2})
    assert shape == {"type": "object", "length": 2}
    assert "sensitive" not in json.dumps(shape)
    assert "Text" not in json.dumps(shape)

    log_path = tmp_path / "action.jsonl"
    monkeypatch.setattr(query_api, "ACTION_LOG_PATH", log_path)
    monkeypatch.setattr(query_api, "ACTION_LOG_MAX_BYTES", 1)
    monkeypatch.setattr(query_api, "ACTION_LOG_BACKUP_COUNT", 2)
    query_api.append_action_log({"body": shape})
    query_api.append_action_log({"body": shape})
    assert log_path.exists()
    assert (tmp_path / "action.jsonl.1").exists()
    combined = log_path.read_text() + (tmp_path / "action.jsonl.1").read_text()
    assert "sensitive card content" not in combined

    summary = organization.summarize_field_change("private before", "private after")
    assert summary == {
        "changed": True,
        "before_length": 14,
        "after_length": 13,
        "before_sha256": hashlib.sha256(b"private before").hexdigest(),
        "after_sha256": hashlib.sha256(b"private after").hexdigest(),
    }


def test_observability_ids_and_last_error_are_sanitized(query_api):
    status, _body, headers = get_from_test_server(
        query_api,
        "/health",
        headers={"X-Correlation-ID": "fixture-correlation-01"},
    )
    assert status == 200
    assert headers["X-Correlation-ID"] == "fixture-correlation-01"
    assert len(headers["X-Request-ID"]) == 32

    recorded = query_api.record_sanitized_error(
        "fixture",
        RuntimeError("Bearer private Text Back Extra <html>"),
    )
    serialized = json.dumps(recorded)
    assert recorded["error_type"] == "RuntimeError"
    for forbidden in ("Bearer", "Text", "Back Extra", "html", "private"):
        assert forbidden not in serialized


def test_combined_sync_worker_receives_detached_payload_only():
    source = (ROOT / "addon-local" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    uploader = functions["post_snapshot_payload"]
    uploader_names = {node.id for node in ast.walk(uploader) if isinstance(node, ast.Name)}
    assert "build_payload" not in uploader_names
    assert "mw" not in uploader_names

    combined = functions["sync_everything_now"]
    worker_calls = [
        node
        for node in ast.walk(combined)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_in_background"
    ]
    assert worker_calls
    for call in worker_calls:
        worker = call.args[0]
        if isinstance(worker, ast.Lambda):
            called = {node.id for node in ast.walk(worker) if isinstance(node, ast.Name)}
            assert "build_payload" not in called
            assert "process_organization_queue_for_sync" not in called


def test_addon_checks_sync_token_before_reading_collection():
    tree = ast.parse((ROOT / "addon-local" / "__init__.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "post_full_snapshot_result"
    )
    calls = {
        node.func.id: node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in {"load_tagging_token", "build_payload"}
    }
    assert calls["load_tagging_token"] < calls["build_payload"]


def test_combined_sync_harness_keeps_collection_work_on_caller_thread():
    source = (ROOT / "addon-local" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "sync_everything_now")
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    main_thread = threading.get_ident()
    collection_threads = []
    upload_threads = []
    callbacks = []

    class Future:
        def __init__(self, value=None, error=None):
            self.value = value
            self.error = error

        def result(self):
            if self.error:
                raise self.error
            return self.value

    class Taskman:
        def run_in_background(self, worker, done):
            holder = {}

            def run():
                try:
                    holder["future"] = Future(worker())
                except Exception as exc:
                    holder["future"] = Future(error=exc)

            thread = threading.Thread(target=run)
            thread.start()
            thread.join()
            callbacks.append(lambda: done(holder["future"]))

    def build_payload():
        collection_threads.append(threading.get_ident())
        return {"note_count": 0, "notes_with_images": 0, "notes": [], "decks": []}

    def process_queue():
        collection_threads.append(threading.get_ident())
        return {"processed": 0, "changed": False, "errors": []}

    def upload(_payload, _reason):
        upload_threads.append(threading.get_ident())
        return {
            "ok": True,
            "cause": "",
            "status": 200,
            "generation_id": "gen-fixture",
        }

    namespace = {
        "COMBINED_SYNC_IN_FLIGHT": False,
        "COMBINED_SYNC_PROGRESS_MAX": 5,
        "LOG_FILE": Path("fixture.log"),
        "mw": types.SimpleNamespace(taskman=Taskman()),
        "showInfo": lambda _message: None,
        "log": lambda _message: None,
        "publication_decision": lambda _trigger: {"allowed": True, "mode": "manual", "reason": "manual_allowed"},
        "load_tagging_token": lambda: "fixture-token",
        "authentication_required_message": lambda: "fixture auth required",
        "progress_start": lambda *_args, **_kwargs: False,
        "progress_update": lambda *_args, **_kwargs: None,
        "progress_finish": lambda: None,
        "run_on_main_thread": lambda callback: callback(),
        "build_payload": build_payload,
        "post_snapshot_payload_result": upload,
        "process_organization_queue_for_sync": process_queue,
        "organization_summary_changed": lambda summary: bool(summary.get("changed")),
        "organization_summary_failed": lambda summary: bool(summary.get("errors")),
        "duration_ms": lambda _started: 0,
        "time": types.SimpleNamespace(perf_counter=lambda: 0),
        "schedule_media_publish_background": lambda **_kwargs: {"status": "queued"},
    }
    exec(compile(module, "<sync-harness>", "exec"), namespace)
    namespace["sync_everything_now"]()
    while callbacks:
        callbacks.pop(0)()
    assert collection_threads
    assert all(thread_id == main_thread for thread_id in collection_threads)
    assert upload_threads
    assert all(thread_id != main_thread for thread_id in upload_threads)
    assert namespace["COMBINED_SYNC_IN_FLIGHT"] is False


def test_explicit_background_worker_call_graph_never_reaches_mw_col():
    """Fail if a current explicit worker can transitively touch the live collection."""
    source = (ROOT / "addon-local" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def touches_collection(node):
        return any(
            isinstance(item, ast.Attribute)
            and item.attr == "col"
            and isinstance(item.value, ast.Name)
            and item.value.id == "mw"
            for item in ast.walk(node)
        )

    call_graph = {
        name: {
            item.func.id
            for item in ast.walk(node)
            if isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id in functions
        }
        for name, node in functions.items()
    }

    def reachable(root):
        pending = [root]
        seen = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            pending.extend(call_graph.get(current, set()) - seen)
        return seen

    # post_snapshot_payload is submitted through taskman; thread_main is the
    # explicit media worker target. Both may do network/filesystem work only.
    for worker_root in ("post_snapshot_payload", "thread_main"):
        reached = reachable(worker_root)
        offenders = sorted(name for name in reached if touches_collection(functions[name]))
        assert not offenders, f"{worker_root} reaches live collection APIs via {offenders}"

    # The generic helper is currently unused. If adopted, its caller must get
    # an equivalent worker-root assertion above.
    helper_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_background_task_with_progress"
    ]
    assert helper_calls == []


@pytest.mark.parametrize(
    "payload,error",
    [
        (None, "invalid_note_updates"),
        ([], "invalid_note_updates"),
        ([{"note_id": 1, "fields": {}}], "invalid_fields"),
        ([{"note_id": 1, "fields": {"Text": None}}], "invalid_field_value"),
        ([{"note_id": True, "fields": {"Text": "x"}}], "invalid_note_id"),
        (
            [
                {"note_id": 1, "fields": {"Text": "a"}},
                {"note_id": 1, "fields": {"Text": "b"}},
            ],
            "duplicate_note_id",
        ),
    ],
)
def test_update_note_fields_rejects_malformed_batches_before_collection_access(
    organization, payload, error
):
    organization.mw = types.SimpleNamespace(col=types.SimpleNamespace())
    with pytest.raises(ValueError, match=error):
        organization.update_note_fields(payload, dry_run=False)


def test_update_note_fields_accepts_empty_string_and_large_artificial_html(organization, monkeypatch):
    note = FakeAnkiNote(1, {"Text": "before", "Back Extra": "stable"})
    configure_fake_notes(organization, monkeypatch, [note])
    large_html = "<div>" + ("<span class=\"kw\">artificial</span>" * 4000) + "</div>"
    preview = organization.update_note_fields(
        [{"note_id": 1, "fields": {"Text": "", "Back Extra": large_html}}],
        dry_run=True,
    )
    assert preview["errors"] == []
    assert preview["changed_count"] == 1
    assert note["Text"] == "before"
    assert note["Back Extra"] == "stable"


def test_operation_dispatch_does_not_require_legacy_confirmation_and_skips_replay(
    organization
):
    organization.mw = types.SimpleNamespace(pm=types.SimpleNamespace(name="fixture"))
    base = {
        "operation_id": "artificial-op",
        "operation_type": "update_note_fields",
        "operation_schema_version": 3,
        "execution_mode": "preview",
        "payload": {"dry_run": True, "note_updates": []},
    }
    unconfirmed = organization.execute_organization_operation({**base, "status": "pending"})
    replayed = organization.execute_organization_operation(
        {**base, "status": "done", "confirmed_by_user": True}
    )
    assert unconfirmed["status"] == "failed"
    assert all("missing_explicit_confirmation" not in error for error in unconfirmed["errors"])
    assert replayed["status"] == "skipped"
    assert "operation_not_pending" in replayed["errors"][0]


@pytest.mark.parametrize(
    "operation,mode,state,result",
    [
        (
            {"status": "pending", "payload": {"dry_run": True}},
            "Prévia",
            "Pendente",
            "",
        ),
        (
            {"status": "done", "payload": {"dry_run": True}, "result": {"changed_count": 15}},
            "Prévia",
            "Concluída",
            "15 alterações previstas",
        ),
        (
            {"status": "done", "payload": {"dry_run": False}, "result": {"changed_count": 15}},
            "Aplicação real",
            "Concluída",
            "15 notes alteradas",
        ),
    ],
)
def test_operation_visual_model_separates_mode_state_and_result(
    organization, operation, mode, state, result
):
    assert organization.operation_mode_label(operation) == mode
    assert organization.operation_state_label(operation) == state
    assert organization.operation_result_label(operation) == result


def test_operations_panel_separates_mode_state_and_has_no_direct_confirmation_action():
    source = (ROOT / "addon-local" / "__init__.py").read_text(encoding="utf-8")
    assert '"Modo",' in source
    assert '"Estado",' in source
    assert "Aplicar esta pr" not in source
    assert 'menu.addAction("Confirmar aplica' not in source
    assert "Aplicacao real" in source or "Aplica\\u00e7\\u00e3o real" in source


def test_operation_visual_model_exposes_failure_partial_and_unknown_states(organization):
    failed = {
        "status": "failed",
        "payload": {"dry_run": False},
        "execution_confirmation": {"errors": ["fixture failure"]},
    }
    partial = {
        "status": "partially_applied",
        "payload": {"dry_run": False},
        "result": {"changed_count": 2, "errors": ["rollback incomplete"]},
    }
    unknown = {"status": "waiting_for_fixture", "payload": {"dry_run": True}}
    assert organization.operation_state_label(failed) == "Falhou"
    assert organization.operation_error_label(failed) == "fixture failure"
    assert organization.operation_state_label(partial) == "Parcialmente aplicada"
    assert organization.operation_result_label(partial) == "2 notes alteradas"
    assert organization.operation_error_label(partial) == "rollback incomplete"
    assert organization.operation_state_label(unknown) == "waiting_for_fixture"


def test_legacy_dry_run_index_status_is_interpreted_as_completed_state(organization):
    legacy = {
        "status": "dry_run",
        "dry_run": True,
        "last_result": {
            "status": "done",
            "result": {"dry_run": True, "changed_count": 15},
        },
    }
    assert organization.operation_status(legacy) == "done"
    assert organization.operation_mode_label(legacy) == "Prévia"
    assert organization.operation_state_label(legacy) == "Concluída"


def test_addon_infers_new_direct_and_legacy_missing_mode_safely(organization):
    new_operation = {
        "operation_schema_version": 3,
        "operation_type": "update_note_fields",
        "status": "pending",
        "payload": {},
    }
    legacy_operation = {
        "operation_schema_version": 2,
        "operation_type": "update_note_fields",
        "status": "pending",
        "payload": {},
    }
    assert organization.operation_execution_mode(new_operation) == "direct"
    assert organization.operation_execution_mode(legacy_operation) == "preview"


def test_operation_index_pending_to_done_updates_same_entry(organization, monkeypatch, tmp_path):
    index_file = tmp_path / "operations_index.json"
    monkeypatch.setattr(organization, "OPERATIONS_INDEX_FILE", index_file)
    monkeypatch.setattr(organization, "LOG_FILE", tmp_path / "organization.log")
    pending = {
        "operation_id": "orgop-transition-fixture",
        "operation_type": "update_note_fields",
        "status": "pending",
        "payload": {"dry_run": True, "note_ids": [1]},
    }
    done = {
        **pending,
        "status": "done",
        "result": {"dry_run": True, "changed_count": 1},
        "execution_confirmation": {"ok": True, "status": "done", "errors": []},
    }
    organization.upsert_operation_index(pending, phase="fetched")
    organization.upsert_operation_index(done, phase="fetched")
    stored = organization.load_operations_index()["operations"]
    assert list(stored) == ["orgop-transition-fixture"]
    assert stored["orgop-transition-fixture"]["status"] == "done"
    assert stored["orgop-transition-fixture"]["dry_run"] is True


def test_remote_operation_fetch_uses_explicit_status_filters(organization, monkeypatch):
    paths = []

    def request(path, method="GET", payload=None):
        paths.append((path, method, payload))
        return {"operations": []}

    monkeypatch.setattr(organization, "organization_api_request", request)
    organization.fetch_remote_organization_operations(limit=7)
    organization.fetch_remote_organization_operations(limit=9, status_filter="all")
    assert paths == [
        ("/organization/operations?status=pending&limit=7", "GET", None),
        ("/organization/operations?status=all&limit=9", "GET", None),
    ]


def test_update_note_fields_should_be_atomic_on_item_error(organization, monkeypatch):
    class FakeNote(dict):
        def __init__(self, **fields):
            super().__init__(fields)
            self.fields = list(fields.values())

    note = FakeNote(Text="antes")
    persisted = []

    def get_note(note_id):
        if note_id == 1:
            return note
        raise KeyError(note_id)

    monkeypatch.setattr(organization, "get_note", get_note)
    monkeypatch.setattr(organization, "note_field_names", lambda value: list(value.keys()))
    monkeypatch.setattr(organization, "persist_note", lambda value: persisted.append(dict(value)))
    monkeypatch.setattr(organization, "save_collection", lambda: None)
    organization.mw = types.SimpleNamespace(
        col=types.SimpleNamespace(db=types.SimpleNamespace(list=lambda *_args: []))
    )

    result = organization.update_note_fields(
        [
            {"note_id": 1, "fields": {"Text": "depois"}},
            {"note_id": 2, "fields": {"Text": "inválido"}},
        ],
        dry_run=False,
    )
    assert result["errors"]
    assert persisted == []


class FakeAnkiNote(dict):
    def __init__(self, note_id, fields, *, mid=10, mod=20, usn=30, tags=None):
        super().__init__(fields)
        self.id = note_id
        self.mid = mid
        self.mod = mod
        self.usn = usn
        self.tags = list(tags or [])
        self.fields = list(fields.values())


def configure_fake_notes(organization, monkeypatch, notes, persist=None, save=None):
    by_id = {note.id: note for note in notes}
    monkeypatch.setattr(
        organization,
        "get_note",
        lambda note_id: by_id[note_id] if note_id in by_id else (_ for _ in ()).throw(ValueError("note_not_found")),
    )
    monkeypatch.setattr(organization, "note_field_names", lambda note: list(note.keys()))
    monkeypatch.setattr(organization, "persist_note", persist or (lambda _note: None))
    monkeypatch.setattr(organization, "save_collection", save or (lambda strict=False: None))
    organization.mw = types.SimpleNamespace(
        col=types.SimpleNamespace(db=types.SimpleNamespace(list=lambda *_args: []))
    )


def test_update_note_fields_valid_batch_and_dry_run(organization, monkeypatch):
    notes = [
        FakeAnkiNote(1, {"Text": "a", "Back Extra": "x"}),
        FakeAnkiNote(2, {"Text": "b", "Back Extra": "y"}),
    ]
    persisted = []
    configure_fake_notes(organization, monkeypatch, notes, persist=lambda note: persisted.append(note.id))
    updates = [
        {"note_id": 1, "fields": {"Text": "A"}},
        {"note_id": 2, "fields": {"Text": "B"}},
    ]
    preview = organization.update_note_fields(updates, dry_run=True, require_preconditions=True)
    assert persisted == []
    assert [note["Text"] for note in notes] == ["a", "b"]
    assert len(preview["preconditions"]) == 2
    assert preview["apply_preconditions"] == preview["preconditions"]

    compact = organization.compact_update_note_fields_result_payload(preview)
    assert compact["apply_preconditions"] == preview["apply_preconditions"]

    conditioned = []
    for update, precondition in zip(updates, preview["preconditions"]):
        conditioned.append({**update, **{k: v for k, v in precondition.items() if k != "note_id"}})
    result = organization.update_note_fields(
        conditioned,
        dry_run=False,
        require_preconditions=True,
    )
    assert result["errors"] == []
    assert result["affected_note_ids"] == [1, 2]
    assert [note["Text"] for note in notes] == ["A", "B"]


@pytest.mark.parametrize("invalid_index", [0, 1, 2])
def test_invalid_note_anywhere_prevents_all_writes(organization, monkeypatch, invalid_index):
    notes = [FakeAnkiNote(1, {"Text": "a"}), FakeAnkiNote(2, {"Text": "b"})]
    persisted = []
    configure_fake_notes(organization, monkeypatch, notes, persist=lambda note: persisted.append(note.id))
    updates = [
        {"note_id": 1, "fields": {"Text": "A"}},
        {"note_id": 2, "fields": {"Text": "B"}},
    ]
    updates.insert(invalid_index, {"note_id": 999, "fields": {"Text": "bad"}})
    result = organization.update_note_fields(updates, dry_run=False)
    assert result["errors"]
    assert persisted == []
    assert [note["Text"] for note in notes] == ["a", "b"]


def test_write_failure_rolls_back_and_can_retry(organization, monkeypatch):
    notes = [FakeAnkiNote(1, {"Text": "a"}), FakeAnkiNote(2, {"Text": "b"})]
    calls = []
    fail_once = {2}

    def persist(note):
        calls.append((note.id, note["Text"]))
        if note.id in fail_once:
            fail_once.remove(note.id)
            raise RuntimeError("fixture_write_failure")

    configure_fake_notes(organization, monkeypatch, notes, persist=persist)
    updates = [
        {"note_id": 1, "fields": {"Text": "A"}},
        {"note_id": 2, "fields": {"Text": "B"}},
    ]
    failed = organization.update_note_fields(updates, dry_run=False)
    assert failed["rolled_back"] is True
    assert [note["Text"] for note in notes] == ["a", "b"]
    retry = organization.update_note_fields(updates, dry_run=False)
    assert retry["errors"] == []
    assert [note["Text"] for note in notes] == ["A", "B"]


def test_commit_failure_rolls_back(organization, monkeypatch):
    note = FakeAnkiNote(1, {"Text": "a"})
    save_calls = []

    def save(strict=False):
        save_calls.append(strict)
        if len(save_calls) == 1:
            raise RuntimeError("fixture_commit_failure")

    configure_fake_notes(organization, monkeypatch, [note], save=save)
    result = organization.update_note_fields(
        [{"note_id": 1, "fields": {"Text": "A"}}],
        dry_run=False,
    )
    assert result["errors"]
    assert result["rolled_back"] is True
    assert note["Text"] == "a"
    assert save_calls == [True, True]


def test_precondition_detects_content_tag_and_model_changes_not_scheduling(organization, monkeypatch):
    note = FakeAnkiNote(1, {"Text": "a", "Other": "stable"}, tags=["one"])
    configure_fake_notes(organization, monkeypatch, [note])
    update = {"note_id": 1, "fields": {"Text": "A"}}
    preview = organization.update_note_fields([update], dry_run=True, require_preconditions=True)
    expected = {k: v for k, v in preview["preconditions"][0].items() if k != "note_id"}

    # Scheduling is read from cards and intentionally excluded from the note hash.
    organization.mw.col.db.list = lambda *_args: [111]
    unchanged = organization.update_note_fields([{**update, **expected}], dry_run=False, require_preconditions=True)
    assert unchanged["errors"] == []

    for mutate in (
        lambda: note.__setitem__("Other", "changed"),
        lambda: note.tags.append("two"),
        lambda: setattr(note, "mid", 99),
    ):
        note["Text"] = "a"
        note["Other"] = "stable"
        note.tags = ["one"]
        note.mid = 10
        mutate()
        conflict = organization.update_note_fields([{**update, **expected}], dry_run=False, require_preconditions=True)
        assert conflict["errors"]
        assert note["Text"] == "a"


def test_replay_with_old_precondition_is_rejected(organization, monkeypatch):
    note = FakeAnkiNote(1, {"Text": "a"})
    configure_fake_notes(organization, monkeypatch, [note])
    update = {"note_id": 1, "fields": {"Text": "A"}}
    preview = organization.update_note_fields([update], dry_run=True, require_preconditions=True)
    conditioned = {**update, **{k: v for k, v in preview["preconditions"][0].items() if k != "note_id"}}
    first = organization.update_note_fields([conditioned], dry_run=False, require_preconditions=True)
    second = organization.update_note_fields([conditioned], dry_run=False, require_preconditions=True)
    assert first["errors"] == []
    assert second["errors"]
    assert note["Text"] == "A"


def test_new_schema_apply_without_precondition_finishes_failed(organization, monkeypatch):
    note = FakeAnkiNote(1, {"Text": "a"})
    configure_fake_notes(organization, monkeypatch, [note])
    organization.mw.pm = types.SimpleNamespace(name="fixture")
    result = organization.execute_organization_operation({
        "operation_id": "orgop-fixture",
        "operation_type": "update_note_fields",
        "operation_schema_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "confirmed_by_user": True,
        "payload": {
            "dry_run": False,
            "note_updates": [{"note_id": 1, "fields": {"Text": "A"}}],
        },
    })
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "expected top-level key expected_content_hash" in result["result"]["errors"][0]["error"]
    assert note["Text"] == "a"


def test_direct_operation_executes_without_preview_or_legacy_confirmation(
    organization, monkeypatch
):
    note = FakeAnkiNote(1, {"Text": "before", "Back Extra": "stable"})
    configure_fake_notes(organization, monkeypatch, [note])
    organization.mw.pm = types.SimpleNamespace(name="artificial-fixture")
    precondition = organization.note_precondition(note, ["Text", "Back Extra"])
    operation = {
        "operation_id": "orgop-direct-artificial",
        "operation_type": "update_note_fields",
        "operation_schema_version": 3,
        "execution_mode": "direct",
        "dry_run": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "payload": {
            "execution_mode": "direct",
            "dry_run": False,
            "note_updates": [{
                "note_id": 1,
                "fields": {"Text": "after"},
                **precondition,
            }],
        },
    }

    result = organization.execute_organization_operation(operation)
    assert result["ok"] is True
    assert result["status"] == "done"
    assert result["execution_mode"] == "direct"
    assert result["result"]["affected_note_ids"] == [1]
    assert result["result"]["notes"][0]["changed_fields"] == ["Text"]
    assert result["result"]["notes"][0]["fields"]["Text"]["before_sha256"]
    assert result["result"]["notes"][0]["fields"]["Text"]["after_sha256"]
    assert note["Text"] == "after"


def test_update_note_fields_compaction_preserves_localized_audit_data(organization):
    payload = {
        "operation": "update_note_fields",
        "execution_mode": "direct",
        "dry_run": False,
        "changed_count": 1,
        "planned_note_ids": [1],
        "affected_note_ids": [1],
        "notes": [{
            "note_id": 1,
            "card_ids": [10],
            "changed_fields": ["Text"],
            "fields": {
                "Text": {
                    "before_sha256": "a" * 64,
                    "after_sha256": "b" * 64,
                },
            },
        }],
        "errors": [],
        "warnings": [],
    }
    compact = organization.compact_update_note_fields_result_payload(payload)
    assert compact["execution_mode"] == "direct"
    assert compact["affected_note_ids"] == [1]
    assert compact["notes"][0]["card_ids"] == [10]
    assert compact["notes"][0]["changed_fields"] == ["Text"]


def test_reorder_compaction_preserves_ids_needed_for_localized_correction(organization):
    payload = {
        "operation": "reorder_cards_by_material",
        "execution_mode": "direct",
        "dry_run": False,
        "order_id": "rord-fixture",
        "ordered_note_ids": [1, 2],
        "ordered_card_ids": [10, 20],
        "eligible_card_ids": [10],
        "expected_eligible_card_ids": [10],
        "proposed_order": [{
            "position": 1,
            "old_nid": 1,
            "new_nid": 3,
            "old_cid": 10,
            "new_cid": 30,
            "eligible": True,
            "warnings": [],
        }],
        "errors": [],
        "warnings": [],
    }
    compact = organization.compact_reorder_result_payload(payload)
    assert compact["execution_mode"] == "direct"
    assert compact["ordered_note_ids"] == [1, 2]
    assert compact["ordered_card_ids"] == [10, 20]
    assert compact["proposed_order_audit"][0]["new_cid"] == 30


@pytest.mark.parametrize(
    "mutate",
    [
        lambda note: note.__setitem__("Text", "changed after dry-run"),
        lambda note: note.__delitem__("Text"),
        lambda note: setattr(note, "mid", 999),
    ],
    ids=["previous-value-changed", "field-removed", "note-type-changed"],
)
def test_apply_v2_rejects_state_changes_after_dry_run_without_writes(
    organization, monkeypatch, mutate
):
    notes = [
        FakeAnkiNote(1, {"Text": "a"}),
        FakeAnkiNote(2, {"Text": "b"}),
        FakeAnkiNote(3, {"Text": "c"}),
    ]
    persisted = []
    configure_fake_notes(
        organization,
        monkeypatch,
        notes,
        persist=lambda note: persisted.append(note.id),
    )
    updates = [
        {"note_id": note.id, "fields": {"Text": note["Text"].upper()}}
        for note in notes
    ]
    preview = organization.update_note_fields(
        updates,
        dry_run=True,
        require_preconditions=True,
    )
    conditioned = [
        {**update, **{key: value for key, value in precondition.items() if key != "note_id"}}
        for update, precondition in zip(updates, preview["apply_preconditions"])
    ]

    mutate(notes[1])
    state_before_apply = [dict(note) for note in notes]
    result = organization.update_note_fields(
        conditioned,
        dry_run=False,
        require_preconditions=True,
    )

    assert result["errors"]
    assert result["changed_count"] == 0
    assert result["affected_note_ids"] == []
    assert persisted == []
    assert [dict(note) for note in notes] == state_before_apply


def test_expired_new_schema_operation_is_rejected(organization, monkeypatch):
    note = FakeAnkiNote(1, {"Text": "a"})
    configure_fake_notes(organization, monkeypatch, [note])
    organization.mw.pm = types.SimpleNamespace(name="fixture")
    precondition = organization.note_precondition(note, ["Text"])
    result = organization.execute_organization_operation({
        "operation_id": "orgop-expired",
        "operation_type": "update_note_fields",
        "operation_schema_version": 2,
        "created_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        "status": "pending",
        "confirmed_by_user": True,
        "payload": {
            "dry_run": False,
            "note_updates": [{"note_id": 1, "fields": {"Text": "A"}, **precondition}],
        },
    })
    assert result["status"] == "failed"
    assert any("operation_expired" in error for error in result["errors"])
    assert note["Text"] == "a"


def test_rollback_failure_is_partially_applied_not_done(organization, monkeypatch):
    notes = [FakeAnkiNote(1, {"Text": "a"}), FakeAnkiNote(2, {"Text": "b"})]

    def persist(note):
        if note.id == 2:
            raise RuntimeError("fixture_persistent_failure")

    configure_fake_notes(organization, monkeypatch, notes, persist=persist)
    organization.mw.pm = types.SimpleNamespace(name="fixture")
    result = organization.execute_organization_operation({
        "operation_id": "orgop-partial",
        "operation_type": "update_note_fields",
        "operation_schema_version": 1,
        "status": "pending",
        "confirmed_by_user": True,
        "payload": {
            "dry_run": False,
            "note_updates": [
                {"note_id": 1, "fields": {"Text": "A"}},
                {"note_id": 2, "fields": {"Text": "B"}},
            ],
        },
    })
    assert result["ok"] is False
    assert result["status"] == "partially_applied"


def artificial_applied_operation_receipt(organization):
    operation = {
        "operation_id": "orgop-receipt-fixture",
        "operation_type": "update_note_fields",
        "operation_schema_version": 2,
        "status": "pending",
        "confirmed_by_user": True,
        "payload": {
            "dry_run": False,
            "updates_id": "updates-fixture",
            "updates_sha256": "a" * 64,
            "note_updates": [{"note_id": 1, "expected_content_hash": "b" * 64}],
        },
    }
    result = {
        "ok": True,
        "operation_id": operation["operation_id"],
        "operation_type": operation["operation_type"],
        "status": "done",
        "addon_profile": "fixture",
        "result": {"dry_run": False, "applied_count": 1},
        "errors": [],
    }
    now = datetime.now(timezone.utc)
    receipt = organization.build_operation_receipt(operation, result, now_value=now)
    return operation, result, receipt, now


def test_operation_receipt_is_deterministic_detects_collision_and_expiry(organization):
    operation, result, receipt, now = artificial_applied_operation_receipt(organization)
    repeated = organization.build_operation_receipt(operation, result, now_value=now)
    assert receipt == repeated
    assert organization.validate_operation_receipt(operation, receipt, now_value=now)["state"] == "valid"

    changed = json.loads(json.dumps(operation))
    changed["payload"]["updates_sha256"] = "c" * 64
    assert organization.validate_operation_receipt(changed, receipt, now_value=now)["state"] == "collision"
    assert organization.validate_operation_receipt(
        operation,
        receipt,
        now_value=now + timedelta(days=91),
    )["state"] == "expired"

    partial = {**result, "ok": False, "status": "partially_applied"}
    assert organization.build_operation_receipt(operation, partial, now_value=now) is not None


def test_lost_confirmation_retry_replays_receipt_without_apply(organization, monkeypatch, tmp_path):
    operation, result, receipt, _now = artificial_applied_operation_receipt(organization)
    monkeypatch.setattr(organization, "OPERATIONS_INDEX_FILE", tmp_path / "operations_index.json")
    monkeypatch.setattr(organization, "LOG_FILE", tmp_path / "organization.log")
    organization.mw.pm = types.SimpleNamespace(name="fixture")
    organization.upsert_operation_index(
        operation,
        result=result,
        phase="processing_finished",
        receipt=receipt,
    )
    monkeypatch.setattr(organization, "fetch_remote_organization_operations", lambda _limit: [operation])
    monkeypatch.setattr(
        organization,
        "execute_organization_operation",
        lambda _operation: pytest.fail("receipt replay must not execute apply"),
    )
    confirmations = []
    monkeypatch.setattr(
        organization,
        "report_organization_operation_result",
        lambda replayed_result, replayed_receipt=None: confirmations.append(
            (replayed_result, replayed_receipt)
        ) or True,
    )

    summary = organization.process_organization_queue()
    assert summary["receipt_replays"] == 1
    assert summary["confirmed"] == 1
    assert confirmations == [(result, receipt)]


def test_backend_result_receipt_is_idempotent(query_api, monkeypatch, tmp_path):
    token_file = tmp_path / "tagging-token"
    token_file.write_text("fixture-token", encoding="utf-8")
    operations_dir = tmp_path / "organization" / "operations"
    operations_dir.mkdir(parents=True)
    monkeypatch.setattr(query_api, "TAGGING_TOKEN_FILE", token_file)
    monkeypatch.delenv(query_api.TAGGING_TOKEN_ENV, raising=False)
    monkeypatch.setattr(query_api, "ORGANIZATION_OPERATIONS_DIR", operations_dir)

    operation = {
        "operation_id": "orgop-backend-receipt",
        "operation_type": "update_note_fields",
        "operation_schema_version": 2,
        "status": "pending",
        "confirmed_by_user": True,
        "payload": {"dry_run": False, "note_updates": []},
    }
    query_api.atomic_write_json(operations_dir / "orgop-backend-receipt.json", operation)
    payload = {
        "ok": True,
        "operation_id": operation["operation_id"],
        "operation_type": operation["operation_type"],
        "status": "done",
        "addon_profile": "fixture",
        "result": {"dry_run": False, "applied_count": 0},
        "errors": [],
    }
    operation_hash = query_api.canonical_sha256(query_api.organization_operation_receipt_payload(operation))
    result_hash = query_api.canonical_sha256(payload)
    receipt_id = query_api.canonical_sha256({
        "operation_id": operation["operation_id"],
        "operation_hash": operation_hash,
        "result_hash": result_hash,
    })
    now = datetime.now(timezone.utc)
    payload["receipt"] = {
        "schema_version": 1,
        "receipt_id": receipt_id,
        "operation_id": operation["operation_id"],
        "operation_type": operation["operation_type"],
        "updates_id": None,
        "applied_at": now.isoformat(),
        "expires_at": (now + timedelta(days=90)).isoformat(),
        "operation_hash": operation_hash,
        "result_hash": result_hash,
        "preconditions_hash": "d" * 64,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"X-Tagging-Token": "fixture-token"}

    legacy_authorization_status, legacy_authorization = post_to_test_server(
        query_api,
        "/organization/operations/confirm",
        headers,
        json.dumps({
            "operation_id": operation["operation_id"],
            "confirmed_by_user": True,
        }).encode("utf-8"),
    )
    path_confirmation_status, path_confirmation = post_to_test_server(
        query_api,
        f"/organization/operations/{operation['operation_id']}/confirm",
        headers,
        json.dumps({"confirmed_by_user": True}).encode("utf-8"),
    )

    assert legacy_authorization_status == 400
    assert legacy_authorization == {"error": "invalid_execution_status"}
    assert path_confirmation_status == 404
    assert path_confirmation["error"] == "not_found"
    persisted_after_rejected_confirmation = json.loads(
        (operations_dir / "orgop-backend-receipt.json").read_text(encoding="utf-8")
    )
    assert persisted_after_rejected_confirmation["status"] == "pending"

    first_status, first = post_to_test_server(query_api, "/organization/operations/result", headers, body)
    retry_status, retry = post_to_test_server(query_api, "/organization/operations/result", headers, body)

    assert first_status == 200 and first["ok"] is True
    assert first["operation"]["execution_result"]["status"] == "done"
    assert first["operation"]["execution_confirmation"]["status"] == "done"
    assert retry_status == 200 and retry == {
        "ok": True,
        "replayed": True,
        "operation_id": operation["operation_id"],
        "status": "done",
    }


def test_backend_pending_filter_never_returns_persisted_done_operation(
    query_api, monkeypatch, tmp_path
):
    token_file = tmp_path / "tagging-token"
    token_file.write_text("fixture-token", encoding="utf-8")
    operations_dir = tmp_path / "operations"
    operations_dir.mkdir()
    monkeypatch.setattr(query_api, "TAGGING_TOKEN_FILE", token_file)
    monkeypatch.delenv(query_api.TAGGING_TOKEN_ENV, raising=False)
    monkeypatch.setattr(query_api, "ORGANIZATION_OPERATIONS_DIR", operations_dir)

    pending = {
        "operation_id": "orgop-pending-filter-fixture",
        "operation_type": "update_note_fields",
        "status": "pending",
        "payload": {"dry_run": True},
    }
    done = {
        "operation_id": "orgop-done-filter-fixture",
        "operation_type": "update_note_fields",
        "status": "done",
        "payload": {"dry_run": True},
        "execution_confirmation": {"ok": True, "status": "done"},
    }
    query_api.atomic_write_json(operations_dir / f"{pending['operation_id']}.json", pending)
    query_api.atomic_write_json(operations_dir / f"{done['operation_id']}.json", done)
    headers = {"X-Tagging-Token": "fixture-token"}

    pending_status, pending_body, _ = get_from_test_server(
        query_api, "/organization/operations?status=pending", headers=headers
    )
    all_status, all_body, _ = get_from_test_server(
        query_api, "/organization/operations?status=all", headers=headers
    )
    pending_payload = json.loads(pending_body)
    all_payload = json.loads(all_body)

    assert pending_status == 200
    assert [item["operation_id"] for item in pending_payload["operations"]] == [
        pending["operation_id"]
    ]
    assert all_status == 200
    assert {item["operation_id"] for item in all_payload["operations"]} == {
        pending["operation_id"],
        done["operation_id"],
    }
