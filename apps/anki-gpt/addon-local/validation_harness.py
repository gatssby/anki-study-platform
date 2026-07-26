"""Real-Anki validation harness restricted to the disposable profile."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import threading

from aqt import mw
from aqt.utils import showInfo


PROFILE_NAME = "Anki GPT Validation"
RESULT_PATH = Path(__file__).resolve().parent.parent / "audits" / "2026-07-11-final-validation" / "disposable_profile_results.json"
TAG = "anki-gpt-validation"


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def model_fields(model) -> list[str]:
    return [str(field.get("name", "")) for field in model.get("flds", [])]


def find_model(required_fields: set[str], minimum_templates: int = 1, cloze: bool | None = None):
    for model in mw.col.models.all():
        fields = set(model_fields(model))
        if not required_fields.issubset(fields) or len(model.get("tmpls", [])) < minimum_templates:
            continue
        is_cloze = int(model.get("type", 0) or 0) == 1
        if cloze is None or is_cloze == cloze:
            return model
    raise RuntimeError(f"fixture_model_not_found:{sorted(required_fields)}:{minimum_templates}:{cloze}")


def ensure_custom_model(name: str, fields: list[str], templates: list[tuple[str, str, str]]):
    existing = mw.col.models.by_name(name)
    if existing:
        return existing
    models = mw.col.models
    model = models.new(name)
    for field_name in fields:
        models.add_field(model, models.new_field(field_name))
    for template_name, qfmt, afmt in templates:
        template = models.new_template(template_name)
        template["qfmt"] = qfmt
        template["afmt"] = afmt
        models.add_template(model, template)
    models.add(model)
    return mw.col.models.by_name(name)


def add_note(deck_id: int, model, fields: dict[str, str]):
    note = mw.col.new_note(model)
    for name, value in fields.items():
        note[name] = value
    note.add_tag(TAG)
    mw.col.add_note(note, deck_id)
    return mw.col.get_note(int(note.id))


def note_state(note_id: int) -> dict:
    note = mw.col.get_note(int(note_id))
    names = model_fields(mw.col.models.get(note.mid))
    cards = mw.col.db.all("select id, due, type, queue from cards where nid=? order by ord,id", int(note_id))
    return {
        "note_id": int(note.id),
        "model_id": int(note.mid),
        "mod": int(note.mod),
        "usn": int(note.usn),
        "tags": sorted(str(tag) for tag in note.tags),
        "fields": {name: {"length": len(str(note[name])), "sha256": sha(str(note[name]))} for name in names},
        "cards": [{"card_id": int(cid), "due": int(due), "type": int(typ), "queue": int(queue)} for cid, due, typ, queue in cards],
    }


def counts() -> dict:
    return {
        "notes": int(mw.col.db.scalar("select count(*) from notes") or 0),
        "cards": int(mw.col.db.scalar("select count(*) from cards") or 0),
        "decks": len(mw.col.decks.all_names_and_ids()),
    }


def update(org, note_id: int, fields: dict[str, str], *, dry_run: bool, precondition: dict | None = None):
    item = {"note_id": int(note_id), "fields": fields}
    if precondition:
        item.update(precondition)
    return org.update_note_fields([item], dry_run=dry_run, require_preconditions=not dry_run)


def precondition_for(org, note_id: int) -> dict:
    note = mw.col.get_note(int(note_id))
    return org.note_precondition(note, org.note_field_names(note))


def has_error(result: dict, fragment: str) -> bool:
    return fragment in json.dumps(result.get("errors", []), ensure_ascii=False)


def record(results: list[dict], name: str, passed: bool, **details) -> None:
    results.append({"name": name, "passed": bool(passed), **details})


def content_signature(org, note_id: int) -> str:
    note = mw.col.get_note(int(note_id))
    return org.note_content_hash(note, org.note_field_names(note))


def schedule_thread_probe(report: dict, addon_module) -> None:
    after = counts()
    report["after_tests"] = after
    report["thread_probe"] = {"status": "pending"}
    report.pop("fatal_error", None)
    RESULT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    detached = json.loads(json.dumps({"profile": PROFILE_NAME, "counts": after}))
    main_thread_id = threading.get_ident()
    def worker():
        worker_thread_id = threading.get_ident()
        serialized = json.dumps(detached, sort_keys=True)
        guard_error = ""
        try:
            addon_module.build_payload()
        except Exception as exc:
            guard_error = str(exc)
        return {"worker_thread_id": worker_thread_id, "serialized_bytes": len(serialized.encode()), "guard_error": guard_error}
    def done(future):
        def finalize():
            current = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
            try:
                probe = future.result()
                probe["callback_thread_id"] = threading.get_ident()
                probe["main_thread_id"] = main_thread_id
                probe["passed"] = (
                    probe["worker_thread_id"] != main_thread_id
                    and probe["callback_thread_id"] == main_thread_id
                    and probe["guard_error"] == "collection_access_outside_main_thread"
                )
            except Exception as exc:
                probe = {"passed": False, "error": f"{type(exc).__name__}:{exc}"}
            current["thread_probe"] = probe
            current["finished_at"] = datetime.now(timezone.utc).isoformat()
            RESULT_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            passed = sum(1 for item in current["tests"] if item["passed"]) + int(probe.get("passed", False))
            total = len(current["tests"]) + 1
            showInfo(f"Validação descartável concluída: {passed}/{total} testes passaram.\nResultado: {RESULT_PATH}")
        mw.taskman.run_on_main(finalize)
    mw.taskman.run_in_background(worker, done)


def resume_validation(addon_module, org, report: dict) -> None:
    results = report.get("tests", [])
    tagged_ids = sorted(int(nid) for nid in mw.col.find_notes(f"tag:{TAG}"))
    cloze_ids = []
    removed_id = None
    for nid in tagged_ids:
        note = mw.col.get_note(nid)
        model = mw.col.models.get(note.mid)
        name = str(model.get("name", ""))
        if name == "Anki GPT Validation Removable Field":
            removed_id = nid
        elif int(model.get("type", 0) or 0) == 1:
            cloze_ids.append(nid)
    if removed_id is None or len(cloze_ids) < 5:
        raise RuntimeError("resume_fixtures_not_found")

    pc = precondition_for(org, removed_id)
    note = mw.col.get_note(removed_id)
    removable = mw.col.models.get(note.mid)
    if "Back Extra" in model_fields(removable):
        field = next(field for field in removable["flds"] if field.get("name") == "Back Extra")
        mw.col.models.remove_field(removable, field)
    removed_field = update(org, removed_id, {"Back Extra": "apply"}, dry_run=False, precondition=pc)
    record(results, "campo_removido", bool(removed_field["errors"]) and removed_field["changed_count"] == 0)

    duplicate_id = cloze_ids[0]
    duplicate_before = content_signature(org, duplicate_id)
    try:
        org.update_note_fields([
            {"note_id": duplicate_id, "fields": {"Back Extra": "dup1"}},
            {"note_id": duplicate_id, "fields": {"Back Extra": "dup2"}},
        ], dry_run=False)
        duplicate_rejected = False
    except ValueError as exc:
        duplicate_rejected = "duplicate_note_id" in str(exc)
    record(results, "operacao_duplicada", duplicate_rejected and content_signature(org, duplicate_id) == duplicate_before)

    retry_ids = cloze_ids[1:3]
    retry_before = {rid: content_signature(org, rid) for rid in retry_ids}
    retry_items = [{"note_id": rid, "fields": {"Back Extra": f"Retry {i}"}, **precondition_for(org, rid)} for i, rid in enumerate(retry_ids)]
    original_persist = org.persist_note
    calls = {"count": 0}
    def fail_second(note):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("injected_second_write_failure")
        return original_persist(note)
    org.persist_note = fail_second
    try:
        failed = org.update_note_fields(retry_items, dry_run=False, require_preconditions=True)
    finally:
        org.persist_note = original_persist
    rolled_back_ok = failed["rolled_back"] and not failed["rollback_errors"] and all(content_signature(org, rid) == retry_before[rid] for rid in retry_ids)
    record(results, "rollback_sem_residuo", rolled_back_ok, status_semantics="failed_with_rolled_back_true")
    retry_items = [{"note_id": rid, "fields": {"Back Extra": f"Retry {i}"}, **precondition_for(org, rid)} for i, rid in enumerate(retry_ids)]
    retried = org.update_note_fields(retry_items, dry_run=False, require_preconditions=True)
    record(results, "retry_idempotente", not retried["errors"] and retried["changed_count"] == 2)
    report["tests"] = results
    schedule_thread_probe(report, addon_module)


def run_disposable_validation(addon_module, organization_module) -> None:
    if getattr(mw.pm, "name", "") != PROFILE_NAME:
        showInfo("Validação bloqueada: selecione o perfil descartável Anki GPT Validation.")
        return
    if organization_module is None:
        showInfo("Validação bloqueada: módulo organization não carregado.")
        return

    org = organization_module
    if RESULT_PATH.exists():
        previous = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
        if previous.get("fatal_error", "").startswith("AttributeError: 'ModelManager' object has no attribute 'rem_field'"):
            try:
                resume_validation(addon_module, org, previous)
            except Exception as exc:
                previous["fatal_error"] = f"{type(exc).__name__}: {exc}"
                previous["finished_at"] = datetime.now(timezone.utc).isoformat()
                RESULT_PATH.write_text(json.dumps(previous, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                showInfo(f"Retomada da validação falhou: {previous['fatal_error']}")
            return
    results: list[dict] = []
    before = counts()
    created_ids: list[int] = []
    try:
        deck_id = int(mw.col.decks.id("Deck Teste"))
        cloze = find_model({"Text", "Back Extra"}, cloze=True)
        two_card = ensure_custom_model(
            "Anki GPT Validation Two Cards",
            ["Front", "Back"],
            [
                ("Forward", "{{Front}}", "{{FrontSide}}<hr id=answer>{{Back}}"),
                ("Reverse", "{{Back}}", "{{FrontSide}}<hr id=answer>{{Front}}"),
            ],
        )
        custom_a = ensure_custom_model(
            "Anki GPT Validation Model A",
            ["Text", "Back Extra"],
            [("Card", "{{Text}}", "{{FrontSide}}<hr id=answer>{{Back Extra}}")],
        )
        custom_b = ensure_custom_model(
            "Anki GPT Validation Model B",
            ["Text", "Back Extra"],
            [("Card", "{{Text}}", "{{FrontSide}}<hr id=answer>{{Back Extra}}")],
        )

        fixtures = [
            add_note(deck_id, cloze, {"Text": "{{c1::Fixture cloze}}", "Back Extra": ""}),
            add_note(deck_id, cloze, {"Text": "{{c1::Fixture text}}", "Back Extra": "Fixture extra"}),
            add_note(deck_id, cloze, {"Text": '{{c1::<span class="kw">Fixture kw</span>}}', "Back Extra": ""}),
            add_note(deck_id, cloze, {"Text": '{{c1::Fixture hint::<span class="hint">Hint</span>}}', "Back Extra": ""}),
            add_note(deck_id, cloze, {"Text": "{{c1::<b>Fixture HTML</b>}}<br><em>Artificial</em>", "Back Extra": ""}),
            add_note(deck_id, two_card, {"Front": "Fixture two cards", "Back": "Artificial back"}),
        ]
        created_ids.extend(int(note.id) for note in fixtures)
        mw.col.save()
        after_fixtures = counts()
        fixture_states = [note_state(nid) for nid in created_ids]
        record(results, "fixtures_minimas", after_fixtures["notes"] - before["notes"] == 6 and after_fixtures["cards"] - before["cards"] == 7, created_note_count=6, created_card_count=7)

        # 1-2: valid dry-run and apply on artificial note.
        nid = created_ids[1]
        dry = update(org, nid, {"Back Extra": "Fixture applied"}, dry_run=True)
        pre = dry["preconditions"][0]
        record(results, "dry_run_valido", not dry["errors"] and dry["changed_count"] == 1 and note_state(nid)["fields"]["Back Extra"]["sha256"] == sha("Fixture extra"))
        applied = update(org, nid, {"Back Extra": "Fixture applied"}, dry_run=False, precondition=pre)
        record(results, "apply_note_artificial", not applied["errors"] and note_state(nid)["fields"]["Back Extra"]["sha256"] == sha("Fixture applied"))

        # 3: entirely valid batch.
        batch_ids = created_ids[2:4]
        batch_items = []
        for index, batch_id in enumerate(batch_ids):
            pc = precondition_for(org, batch_id)
            batch_items.append({"note_id": batch_id, "fields": {"Back Extra": f"Batch {index}"}, **pc})
        batch = org.update_note_fields(batch_items, dry_run=False, require_preconditions=True)
        record(results, "lote_totalmente_valido", not batch["errors"] and batch["changed_count"] == 2)

        # 4: invalid item in the middle leaves valid neighbors unchanged.
        left, right = created_ids[0], created_ids[4]
        left_before, right_before = note_state(left), note_state(right)
        invalid_batch = org.update_note_fields([
            {"note_id": left, "fields": {"Back Extra": "must-not-stick"}, **precondition_for(org, left)},
            {"note_id": 9999999999999, "fields": {"Text": "invalid"}, "expected_content_hash": "0" * 64},
            {"note_id": right, "fields": {"Back Extra": "must-not-stick"}, **precondition_for(org, right)},
        ], dry_run=False, require_preconditions=True)
        record(results, "lote_invalido_no_meio", bool(invalid_batch["errors"]) and note_state(left) == left_before and note_state(right) == right_before and invalid_batch["changed_count"] == 0)

        # 5/7: target and other-field conflicts.
        conflict_note = add_note(deck_id, cloze, {"Text": "{{c1::Conflict}}", "Back Extra": "before"})
        created_ids.append(int(conflict_note.id)); pc = precondition_for(org, conflict_note.id)
        changed = mw.col.get_note(conflict_note.id); changed["Back Extra"] = "external"; mw.col.update_note(changed); mw.col.save()
        conflict = update(org, conflict_note.id, {"Back Extra": "apply"}, dry_run=False, precondition=pc)
        record(results, "conflito_campo_alvo", has_error(conflict, "note_content_conflict") and mw.col.get_note(conflict_note.id)["Back Extra"] == "external")
        pc = precondition_for(org, conflict_note.id); changed = mw.col.get_note(conflict_note.id); changed["Text"] = "{{c1::Other changed}}"; mw.col.update_note(changed); mw.col.save()
        other_conflict = update(org, conflict_note.id, {"Back Extra": "apply2"}, dry_run=False, precondition=pc)
        record(results, "conflito_outro_campo", has_error(other_conflict, "note_content_conflict"))

        # Tags conflict.
        pc = precondition_for(org, conflict_note.id); changed = mw.col.get_note(conflict_note.id); changed.add_tag("external-tag"); mw.col.update_note(changed); mw.col.save()
        tag_conflict = update(org, conflict_note.id, {"Back Extra": "apply3"}, dry_run=False, precondition=pc)
        record(results, "conflito_tags", has_error(tag_conflict, "note_content_conflict"))

        # 6: deleted after dry-run.
        deleted_note = add_note(deck_id, cloze, {"Text": "{{c1::Delete me}}", "Back Extra": ""}); deleted_id = int(deleted_note.id)
        pc = precondition_for(org, deleted_id); mw.col.remove_notes([deleted_id]); mw.col.save()
        deleted = update(org, deleted_id, {"Back Extra": "apply"}, dry_run=False, precondition=pc)
        record(results, "note_deletada_apos_dry_run", has_error(deleted, "note_not_found"))

        # Deterministic hash and scheduling neutrality.
        sched_id = created_ids[0]; pc1 = precondition_for(org, sched_id); pc2 = precondition_for(org, sched_id)
        record(results, "hash_deterministico", pc1["expected_content_hash"] == pc2["expected_content_hash"])
        card_id = int(mw.col.db.scalar("select id from cards where nid=? limit 1", sched_id))
        card = mw.col.get_card(card_id); card.due = int(card.due) + 1; mw.col.update_card(card); mw.col.save()
        scheduling = update(org, sched_id, {"Back Extra": "Scheduling neutral"}, dry_run=False, precondition=pc1)
        record(results, "scheduling_neutro", not scheduling["errors"])

        # Note type conflict using two artificial models with identical fields.
        model_note = add_note(deck_id, custom_a, {"Text": "Model A", "Back Extra": "before"}); model_id = int(model_note.id); created_ids.append(model_id)
        pc = precondition_for(org, model_id)
        mw.col.db.execute("update notes set mid=? where id=?", int(custom_b["id"]), model_id)
        mw.col.save()
        model_conflict = update(org, model_id, {"Back Extra": "apply"}, dry_run=False, precondition=pc)
        record(results, "conflito_note_type", has_error(model_conflict, "note_content_conflict") or has_error(model_conflict, "note_model_conflict"))

        # Field removed after dry-run on a dedicated artificial model.
        removable = ensure_custom_model(
            "Anki GPT Validation Removable Field",
            ["Text", "Back Extra"],
            [("Card", "{{Text}}", "{{FrontSide}}<hr id=answer>{{Back Extra}}")],
        )
        removed_note = add_note(deck_id, removable, {"Text": "Remove field", "Back Extra": "before"}); removed_id = int(removed_note.id); created_ids.append(removed_id)
        pc = precondition_for(org, removed_id)
        removable = mw.col.models.get(removable["id"])
        mw.col.models.remove_field(removable, removable["flds"][1])
        removed_field = update(org, removed_id, {"Back Extra": "apply"}, dry_run=False, precondition=pc)
        record(results, "campo_removido", bool(removed_field["errors"]) and removed_field["changed_count"] == 0)

        # 8: duplicate update IDs rejected before mutation.
        dup_before = note_state(created_ids[4])
        try:
            org.update_note_fields([
                {"note_id": created_ids[4], "fields": {"Back Extra": "dup1"}},
                {"note_id": created_ids[4], "fields": {"Back Extra": "dup2"}},
            ], dry_run=False)
            duplicate_rejected = False
        except ValueError as exc:
            duplicate_rejected = "duplicate_note_id" in str(exc)
        record(results, "operacao_duplicada", duplicate_rejected and note_state(created_ids[4]) == dup_before)

        # 9-10: fail after first real write, verify rollback, then retry.
        retry_ids = created_ids[2:4]
        retry_before = {rid: note_state(rid) for rid in retry_ids}
        retry_items = [{"note_id": rid, "fields": {"Back Extra": f"Retry {i}"}, **precondition_for(org, rid)} for i, rid in enumerate(retry_ids)]
        original_persist = org.persist_note
        calls = {"count": 0}
        def fail_second(note):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("injected_second_write_failure")
            return original_persist(note)
        org.persist_note = fail_second
        try:
            failed = org.update_note_fields(retry_items, dry_run=False, require_preconditions=True)
        finally:
            org.persist_note = original_persist
        rolled_back_ok = failed["rolled_back"] and not failed["rollback_errors"] and all(note_state(rid) == retry_before[rid] for rid in retry_ids)
        record(results, "rollback_sem_residuo", rolled_back_ok, status_semantics="failed_with_rolled_back_true")
        retry_items = [{"note_id": rid, "fields": {"Back Extra": f"Retry {i}"}, **precondition_for(org, rid)} for i, rid in enumerate(retry_ids)]
        retried = org.update_note_fields(retry_items, dry_run=False, require_preconditions=True)
        record(results, "retry_idempotente", not retried["errors"] and retried["changed_count"] == 2)

        after = counts()
        report = {
            "profile": PROFILE_NAME,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "main_thread_id": threading.get_ident(),
            "before": before,
            "after_fixtures": after_fixtures,
            "after_tests": after,
            "fixture_note_ids": created_ids[:6],
            "fixture_states_before_operations": fixture_states,
            "tests": results,
            "thread_probe": {"status": "pending"},
        }
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        detached = json.loads(json.dumps({"profile": PROFILE_NAME, "counts": after}))
        main_thread_id = threading.get_ident()
        def worker():
            worker_thread_id = threading.get_ident()
            serialized = json.dumps(detached, sort_keys=True)
            guard_error = ""
            try:
                addon_module.build_payload()
            except Exception as exc:
                guard_error = str(exc)
            return {"worker_thread_id": worker_thread_id, "serialized_bytes": len(serialized.encode()), "guard_error": guard_error}
        def done(future):
            def finalize():
                current = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
                try:
                    probe = future.result()
                    probe["callback_thread_id"] = threading.get_ident()
                    probe["main_thread_id"] = main_thread_id
                    probe["passed"] = (
                        probe["worker_thread_id"] != main_thread_id
                        and probe["callback_thread_id"] == main_thread_id
                        and probe["guard_error"] == "collection_access_outside_main_thread"
                    )
                except Exception as exc:
                    probe = {"passed": False, "error": f"{type(exc).__name__}:{exc}"}
                current["thread_probe"] = probe
                current["finished_at"] = datetime.now(timezone.utc).isoformat()
                RESULT_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                passed = sum(1 for item in current["tests"] if item["passed"]) + int(probe.get("passed", False))
                total = len(current["tests"]) + 1
                showInfo(f"Validação descartável concluída: {passed}/{total} testes passaram.\nResultado: {RESULT_PATH}")
            mw.taskman.run_on_main(finalize)
        mw.taskman.run_in_background(worker, done)
    except Exception as exc:
        failure = {
            "profile": getattr(mw.pm, "name", ""),
            "before": before,
            "tests": results,
            "fatal_error": f"{type(exc).__name__}: {exc}",
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        showInfo(f"Validação descartável falhou: {failure['fatal_error']}\nResultado: {RESULT_PATH}")
