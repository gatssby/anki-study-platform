"""Reliability layer for direct-v2 organization operations.

The previous implementation is kept verbatim in organization_legacy.py.  This
module re-exports it and overrides only the direct-v2 note update path, queue
accounting, conflict diagnostics, and the automatic-sync ordering needed to
keep published snapshots aligned with the state seen by apply-v2.
"""

from __future__ import annotations

import hashlib
import json
import sys
import types
import unicodedata

try:
    from .organization_legacy import *  # noqa: F401,F403
    from . import organization_legacy as _legacy
except ImportError:  # pragma: no cover - standalone addon test/import fallback
    from organization_legacy import *  # type: ignore  # noqa: F401,F403
    import organization_legacy as _legacy  # type: ignore


_CURRENT_OPERATION_ID = ""
_ORIGINAL_EXECUTE = _legacy.execute_organization_operation
_ORIGINAL_CHANGE_SUMMARY = _legacy.organization_result_change_summary


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _field_fingerprint(value: str) -> dict:
    value = str(value)
    return {
        "sha256": _sha256_text(value),
        "length": len(value),
        "utf8_bytes": len(value.encode("utf-8")),
        "crlf_count": value.count("\r\n"),
        "lf_count": value.count("\n"),
        "cr_count": value.count("\r"),
        "nfc": unicodedata.normalize("NFC", value) == value,
        "nfkc": unicodedata.normalize("NFKC", value) == value,
        "leading_whitespace": bool(value[:1].isspace()),
        "trailing_whitespace": bool(value[-1:].isspace()),
        "contains_nbsp": "\u00a0" in value,
        "contains_amp_entity": "&" in value and ";" in value,
    }


def _tags_fingerprint(tags) -> dict:
    ordered = sorted(str(tag) for tag in (tags or []))
    canonical = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
    return {
        "sha256": _sha256_text(canonical),
        "count": len(ordered),
        "tags": ordered,
    }


def note_state_diagnostics(note, field_names: list[str]) -> dict:
    fields = {
        field_name: _legacy.get_note_field_value(note, field_name, field_names)
        for field_name in field_names
    }
    precondition = _legacy.note_precondition(note, field_names)
    return {
        "precondition": precondition,
        "field_names": list(field_names),
        "fields": fields,
        "field_fingerprints": {
            field_name: _field_fingerprint(value)
            for field_name, value in fields.items()
        },
        "tags": list(getattr(note, "tags", []) or []),
        "tags_fingerprint": _tags_fingerprint(getattr(note, "tags", []) or []),
    }


def _expected_actual_preconditions(update: dict, current: dict) -> dict:
    return {
        "expected": {
            "content_hash": update.get("expected_content_hash"),
            "mod": update.get("expected_mod"),
            "usn": update.get("expected_usn"),
            "model_id": update.get("expected_model_id"),
        },
        "actual": {
            "content_hash": current.get("expected_content_hash"),
            "mod": current.get("expected_mod"),
            "usn": current.get("expected_usn"),
            "model_id": current.get("expected_model_id"),
        },
    }


def _precondition_conflict(update: dict, current: dict, require: bool) -> str | None:
    expected_hash = update.get("expected_content_hash")
    if require and not expected_hash:
        return "missing_note_precondition"

    expected_model = update.get("expected_model_id")
    if expected_model is not None and expected_model != current.get("expected_model_id"):
        return "note_model_conflict"

    if expected_hash and expected_hash != current.get("expected_content_hash"):
        return "note_content_conflict"

    expected_mod = update.get("expected_mod")
    if expected_mod is not None and expected_mod != current.get("expected_mod"):
        return "note_mod_conflict"

    expected_usn = update.get("expected_usn")
    if expected_usn is not None and expected_usn != current.get("expected_usn"):
        return "note_usn_conflict"

    return None


def _conflict_diagnostics(update: dict, note, field_names: list[str], reason: str) -> dict:
    state = note_state_diagnostics(note, field_names)
    current = state["precondition"]
    versions = _expected_actual_preconditions(update, current)
    changed_components = []
    if versions["expected"]["model_id"] is not None and versions["expected"]["model_id"] != versions["actual"]["model_id"]:
        changed_components.append("model_id")
    if versions["expected"]["mod"] is not None and versions["expected"]["mod"] != versions["actual"]["mod"]:
        changed_components.append("mod")
    if versions["expected"]["usn"] is not None and versions["expected"]["usn"] != versions["actual"]["usn"]:
        changed_components.append("usn")
    if versions["expected"]["content_hash"] and versions["expected"]["content_hash"] != versions["actual"]["content_hash"]:
        if "model_id" not in changed_components:
            changed_components.append("fields_or_tags")
        changed_components.append("content_hash")

    return {
        "reason": reason,
        **versions,
        "changed_components": changed_components,
        "actual_field_fingerprints": state["field_fingerprints"],
        "actual_tags_fingerprint": state["tags_fingerprint"],
    }


def _post_apply_transition(item: dict) -> dict:
    note_id = item["note_id"]
    before_state = item["before_state"]
    field_names = item["field_names"]
    requested_fields = item["fields"]
    reloaded = _legacy.get_note(note_id)
    after_state = note_state_diagnostics(reloaded, field_names)

    unexpected_changes = []
    field_transitions = {}
    for field_name in field_names:
        before = before_state["fields"].get(field_name, "")
        expected_after = requested_fields.get(field_name, before)
        actual_after = after_state["fields"].get(field_name, "")
        if before != actual_after or expected_after != actual_after:
            field_transitions[field_name] = {
                "targeted": field_name in requested_fields,
                "before": _field_fingerprint(before),
                "requested": _field_fingerprint(expected_after),
                "persisted": _field_fingerprint(actual_after),
                "persisted_matches_requested": actual_after == expected_after,
            }
        if actual_after != expected_after:
            unexpected_changes.append({
                "component": f"field:{field_name}",
                "targeted": field_name in requested_fields,
                "reason": (
                    "persisted_value_differs_from_requested"
                    if field_name in requested_fields
                    else "untargeted_field_changed_during_persist"
                ),
            })

    before_tags = sorted(str(tag) for tag in before_state.get("tags", []))
    after_tags = sorted(str(tag) for tag in after_state.get("tags", []))
    if before_tags != after_tags:
        unexpected_changes.append({
            "component": "tags",
            "reason": "tags_changed_during_update_note_persist",
            "before": _tags_fingerprint(before_tags),
            "after": _tags_fingerprint(after_tags),
        })

    before_pre = before_state["precondition"]
    after_pre = after_state["precondition"]
    if before_pre.get("expected_model_id") != after_pre.get("expected_model_id"):
        unexpected_changes.append({
            "component": "model_id",
            "reason": "model_changed_during_update_note_persist",
        })

    return {
        "note_id": note_id,
        "before": before_pre,
        "after": after_pre,
        "field_transitions": field_transitions,
        "tags_before": before_state["tags_fingerprint"],
        "tags_after": after_state["tags_fingerprint"],
        "automatic_or_normalized_changes": unexpected_changes,
        "mod_changed": before_pre.get("expected_mod") != after_pre.get("expected_mod"),
        "usn_changed": before_pre.get("expected_usn") != after_pre.get("expected_usn"),
        "content_hash_changed": before_pre.get("expected_content_hash") != after_pre.get("expected_content_hash"),
    }


def update_note_fields(
    note_updates: list[dict],
    dry_run: bool = True,
    requested_by: str = "",
    reason: str = "",
    require_preconditions: bool = False,
) -> dict:
    """Apply direct-v2 safely per note instead of discarding a whole batch.

    Schema/integrity errors still keep the batch atomic. Optimistic-concurrency
    failures are isolated to the conflicting note; every other note whose
    preconditions pass is committed in one Anki update_notes() call.
    """

    dry_run = _legacy.normalize_bool(dry_run, True)
    updates = _legacy.normalize_note_field_updates(note_updates)
    notes_result = []
    errors = []
    hard_errors = []
    conflict_errors = []
    prepared = []
    operation_id = _CURRENT_OPERATION_ID

    _legacy.log(
        "organization update_note_fields start "
        f"operation_id={operation_id} requested_count={len(updates)} dry_run={dry_run}"
    )

    for update in updates:
        note_id = update["note_id"]
        try:
            note = _legacy.get_note(note_id)
            field_names = _legacy.note_field_names(note)
            unknown_fields = sorted(set(update["fields"]) - set(field_names))
            if unknown_fields:
                raise ValueError(f"unknown_fields: {unknown_fields}")

            before_state = note_state_diagnostics(note, field_names)
            precondition = before_state["precondition"]
            conflict_reason = _precondition_conflict(
                update,
                precondition,
                require=bool(require_preconditions and not dry_run),
            )
            if conflict_reason:
                diagnostics = _conflict_diagnostics(update, note, field_names, conflict_reason)
                error = {
                    "note_id": note_id,
                    "error": f"ValueError: {conflict_reason}",
                    "reason": conflict_reason,
                    "precondition_conflict": diagnostics,
                }
                errors.append(error)
                conflict_errors.append(error)
                notes_result.append({
                    "note_id": note_id,
                    "changed": False,
                    "changed_fields": [],
                    "error": error["error"],
                    "precondition_conflict": diagnostics,
                })
                _legacy.log(
                    "organization update_note_fields precondition_conflict "
                    + json.dumps({
                        "operation_id": operation_id,
                        "note_id": note_id,
                        **diagnostics,
                    }, ensure_ascii=False, sort_keys=True)
                )
                continue

            field_results = {}
            changed_fields = []
            original_target_fields = {}
            for field_name, after in update["fields"].items():
                before = _legacy.get_note_field_value(note, field_name, field_names)
                original_target_fields[field_name] = before
                field_results[field_name] = _legacy.summarize_field_change(before, after)
                field_results[field_name].update(
                    update.get("field_normalization", {}).get(field_name, {
                        "normalized": False,
                        "removed_visual_wrappers_count": 0,
                        "normalized_hints_count": 0,
                    })
                )
                if before != after:
                    changed_fields.append(field_name)

            kw_count_after = sum(
                _legacy.count_class_spans(value, "kw")
                for value in update["fields"].values()
            )
            hint_count_after = sum(
                _legacy.count_class_spans(value, "hint")
                for value in update["fields"].values()
            )
            warnings = []
            if (
                kw_count_after == 0
                and _legacy.update_fields_should_warn_missing_kw(
                    requested_by=requested_by,
                    reason=reason,
                    fields=update["fields"],
                )
            ):
                warnings.append("missing_kw_after_update")

            card_ids = [
                int(card_id)
                for card_id in _legacy.mw.col.db.list(
                    """
                    select id
                    from cards
                    where nid = ?
                    order by ord, id
                    """,
                    int(note_id),
                )
            ]
            note_result = {
                "note_id": note_id,
                "card_ids": card_ids,
                "field_names": field_names,
                "changed_fields": changed_fields,
                "changed": bool(changed_fields),
                "kw_count_after": kw_count_after,
                "hint_count_after": hint_count_after,
                "has_kw_after": kw_count_after > 0,
                "warnings": warnings,
                "fields": field_results,
                "precondition": precondition,
            }
            notes_result.append(note_result)
            prepared.append({
                "note": note,
                "note_id": note_id,
                "field_names": field_names,
                "fields": update["fields"],
                "original_fields": original_target_fields,
                "changed_fields": changed_fields,
                "precondition": precondition,
                "before_state": before_state,
                "note_result": note_result,
            })
        except Exception as exc:
            error = {
                "note_id": note_id,
                "error": f"{type(exc).__name__}: {exc}",
                "reason": "note_validation_error",
            }
            errors.append(error)
            hard_errors.append(error)
            notes_result.append({
                "note_id": note_id,
                "changed": False,
                "changed_fields": [],
                "error": error["error"],
            })
            _legacy.log(
                "organization update_note_fields note failed "
                f"operation_id={operation_id} note_id={note_id} dry_run={dry_run} "
                f"error={error['error']}"
            )

    planned_note_ids = [item["note_id"] for item in prepared if item["changed_fields"]]
    applied_note_ids = []
    rolled_back = False
    rollback_errors = []
    post_apply_transitions = []
    undo_label = "Anki GPT: atualizar campos de notes"
    undo_entry = None
    undo_available = False

    can_apply_valid_subset = not hard_errors

    if can_apply_valid_subset and not dry_run:
        try:
            changed_items = [item for item in prepared if item["changed_fields"]]
            if changed_items:
                undo_entry = _legacy.begin_custom_undo_entry(undo_label)
            for item in changed_items:
                for field_name, after in item["fields"].items():
                    _legacy.set_note_field_value(
                        item["note"], field_name, after, item["field_names"]
                    )
            _legacy.persist_notes_batch([item["note"] for item in changed_items])
            applied_note_ids.extend(item["note_id"] for item in changed_items)
            _legacy.save_collection(strict=True)
            if applied_note_ids:
                undo_available = _legacy.finish_custom_undo_entry(undo_entry)

            for item in changed_items:
                transition = _post_apply_transition(item)
                post_apply_transitions.append(transition)
                item["note_result"]["post_apply_transition"] = transition
                _legacy.log(
                    "organization update_note_fields post_apply_transition "
                    + json.dumps({
                        "operation_id": operation_id,
                        **transition,
                    }, ensure_ascii=False, sort_keys=True)
                )
        except Exception as apply_error:
            rollback_notes = []
            rollback_scope = set(applied_note_ids or planned_note_ids)
            for item in prepared:
                if item["note_id"] not in rollback_scope or not item["changed_fields"]:
                    continue
                try:
                    for field_name, before in item["original_fields"].items():
                        _legacy.set_note_field_value(
                            item["note"], field_name, before, item["field_names"]
                        )
                    rollback_notes.append(item["note"])
                except Exception as rollback_error:
                    rollback_errors.append({
                        "note_id": item["note_id"],
                        "error": f"{type(rollback_error).__name__}: {rollback_error}",
                    })
            if rollback_notes:
                try:
                    _legacy.persist_notes_batch(rollback_notes)
                except Exception as rollback_error:
                    rollback_errors.append({
                        "note_id": None,
                        "error": f"{type(rollback_error).__name__}: {rollback_error}",
                    })
            try:
                _legacy.save_collection(strict=True)
            except Exception as rollback_commit_error:
                rollback_errors.append({
                    "note_id": None,
                    "error": f"{type(rollback_commit_error).__name__}: {rollback_commit_error}",
                })
            rolled_back = not rollback_errors
            errors.append({
                "note_id": None,
                "error": f"batch_apply_failed: {type(apply_error).__name__}: {apply_error}",
                "reason": "batch_apply_failed",
            })
            if rolled_back:
                applied_note_ids = []

    apply_preconditions = [
        {"note_id": item["note_id"], **item["precondition"]}
        for item in prepared
    ]
    partial_apply = bool(conflict_errors and applied_note_ids)
    result = {
        "operation": "update_note_fields",
        "dry_run": dry_run,
        "requested_count": len(updates),
        "changed_count": len(applied_note_ids) if not dry_run else len(planned_note_ids),
        "planned_note_ids": planned_note_ids,
        "affected_note_ids": applied_note_ids if not dry_run else planned_note_ids,
        "conflicted_note_ids": [error["note_id"] for error in conflict_errors],
        "hard_error_note_ids": [
            error["note_id"] for error in hard_errors if error.get("note_id") is not None
        ],
        "notes": notes_result,
        "errors": errors,
        "atomic": not partial_apply,
        "atomic_scope": "validated_subset" if partial_apply else "operation",
        "partial_apply": partial_apply,
        "rolled_back": rolled_back,
        "rollback_errors": rollback_errors,
        "post_apply_transitions": post_apply_transitions,
        "undo_available": undo_available,
        "undo_label": undo_label if undo_available else None,
        "undo_entry": undo_entry if undo_available else None,
        "preconditions_required": bool(require_preconditions and not dry_run),
        "apply_preconditions": apply_preconditions,
        "preconditions": apply_preconditions,
        "warnings": [
            {"note_id": item["note_id"], "warning": warning}
            for item in notes_result
            for warning in item.get("warnings", [])
        ],
    }
    _legacy.log(
        "organization update_note_fields finished "
        f"operation_id={operation_id} requested_count={len(updates)} "
        f"changed_count={result['changed_count']} conflicts={len(conflict_errors)} "
        f"hard_errors={len(hard_errors)} partial_apply={partial_apply} "
        f"errors={len(errors)} dry_run={dry_run}"
    )
    return result


def execute_organization_operation(operation: dict) -> dict:
    global _CURRENT_OPERATION_ID
    previous = _CURRENT_OPERATION_ID
    _CURRENT_OPERATION_ID = _legacy.normalize_operation_id(operation)
    try:
        response = _ORIGINAL_EXECUTE(operation)
    finally:
        _CURRENT_OPERATION_ID = previous

    if (
        isinstance(response, dict)
        and response.get("operation_type") == "update_note_fields"
        and response.get("status") == "partially_applied"
    ):
        response["errors"] = ["update_note_fields_partial_conflict"]
        _legacy.log(
            "organization update_note_fields partial result "
            f"operation_id={response.get('operation_id')} "
            f"affected_note_ids={(response.get('result') or {}).get('affected_note_ids', [])} "
            f"conflicted_note_ids={(response.get('result') or {}).get('conflicted_note_ids', [])}"
        )
    return response


def organization_result_change_summary(result: dict) -> dict:
    if isinstance(result, dict) and result.get("status") == "partially_applied":
        surrogate = dict(result)
        surrogate["ok"] = True
        surrogate["status"] = "done"
        return _ORIGINAL_CHANGE_SUMMARY(surrogate)
    return _ORIGINAL_CHANGE_SUMMARY(result)


def _accumulate_change_summary(summary: dict, changed_note_ids: set[int], result: dict) -> None:
    change_summary = organization_result_change_summary(result)
    if not change_summary.get("changed"):
        return
    summary["changed"] = True
    summary["changed_operations"] += 1
    changed_note_ids.update(change_summary.get("changed_note_ids", []))
    summary["changed_card_count"] += change_summary.get("changed_card_count", 0)


def process_organization_queue() -> dict:
    summary = {
        "fetched": 0,
        "processed": 0,
        "succeeded": 0,
        "partially_applied": 0,
        "failed": 0,
        "skipped": 0,
        "confirmed": 0,
        "confirmation_failed": 0,
        "receipt_replays": 0,
        "receipt_blocked": 0,
        "changed": False,
        "changed_operations": 0,
        "changed_note_ids": [],
        "changed_note_count": 0,
        "changed_card_count": 0,
        "errors": [],
    }
    changed_note_ids: set[int] = set()

    try:
        operations = _legacy.fetch_remote_organization_operations(
            _legacy.ORGANIZATION_MAX_OPERATIONS_PER_RUN
        )
        if operations is None:
            summary["errors"].append("organization_api_no_response")
            return summary
        if not operations:
            _legacy.log("organization queue empty")
            return summary

        summary["fetched"] = len(operations)
        _legacy.log(f"organization queue fetched count={len(operations)}")

        for operation in operations:
            operation_id = operation.get("operation_id", "")
            operation_type = operation.get("operation_type", "")
            dry_run = _legacy.operation_dry_run(operation)
            execution_mode = "preview" if dry_run else "direct"
            if operation.get("status") != "pending":
                summary["skipped"] += 1
                _legacy.log(
                    f"organization skip non-pending operation_id={operation_id} "
                    f"operation_type={operation_type} execution_mode={execution_mode} "
                    f"dry_run={dry_run} status={operation.get('status')}"
                )
                continue

            receipt_state = _legacy.local_operation_receipt_state(operation)
            if receipt_state["state"] == "valid":
                result = receipt_state["result"]
                receipt = receipt_state["receipt"]
                _legacy.upsert_operation_index(
                    operation, result=result, phase="receipt_replayed", receipt=receipt
                )
                summary["processed"] += 1
                if result.get("status") == "partially_applied":
                    summary["partially_applied"] += 1
                    summary["failed"] += 1
                    _accumulate_change_summary(summary, changed_note_ids, result)
                else:
                    summary["succeeded"] += 1
                summary["receipt_replays"] += 1
                if _legacy.report_organization_operation_result(result, receipt):
                    summary["confirmed"] += 1
                else:
                    summary["confirmation_failed"] += 1
                _legacy.log(
                    f"organization receipt replay operation_id={operation_id} "
                    "status=confirmed_without_apply"
                )
                continue

            if receipt_state["state"] in {"expired", "collision", "invalid"}:
                block_reason = receipt_state.get("reason", "receipt_blocked")
                summary["failed"] += 1
                summary["receipt_blocked"] += 1
                summary["errors"].append(block_reason)
                _legacy.log(
                    f"organization receipt blocked operation_id={operation_id} reason={block_reason}"
                )
                continue

            _legacy.log(
                "organization queue processing "
                f"module_file={__file__} operation_id={operation_id} "
                f"operation_type={operation_type} execution_mode={execution_mode} "
                f"dry_run={dry_run}"
            )
            _legacy.upsert_operation_index(
                operation, status="running", phase="processing_started"
            )
            result = execute_organization_operation(operation)
            receipt = _legacy.build_operation_receipt(operation, result)
            _legacy.upsert_operation_index(
                operation, result=result, phase="processing_finished", receipt=receipt
            )
            summary["processed"] += 1

            if result.get("ok") and result.get("status") == "done":
                summary["succeeded"] += 1
                _accumulate_change_summary(summary, changed_note_ids, result)
            elif result.get("status") == "partially_applied":
                summary["partially_applied"] += 1
                summary["failed"] += 1
                summary["errors"].extend(result.get("errors", []))
                _accumulate_change_summary(summary, changed_note_ids, result)
            elif result.get("status") == "skipped":
                summary["skipped"] += 1
            else:
                summary["failed"] += 1
                summary["errors"].extend(result.get("errors", []))

            if _legacy.report_organization_operation_result(result, receipt):
                summary["confirmed"] += 1
            else:
                summary["confirmation_failed"] += 1

            _legacy.log(
                "organization queue processed "
                f"operation_id={operation_id} operation_type={operation_type} "
                f"execution_mode={execution_mode} dry_run={dry_run} "
                f"status={result.get('status')} ok={result.get('ok')} "
                f"changed={organization_result_change_summary(result)['changed']}"
            )

        summary["changed_note_ids"] = sorted(changed_note_ids)
        summary["changed_note_count"] = len(changed_note_ids)
        _legacy.log(
            "organization queue summary "
            f"fetched={summary['fetched']} processed={summary['processed']} "
            f"succeeded={summary['succeeded']} partially_applied={summary['partially_applied']} "
            f"failed={summary['failed']} skipped={summary['skipped']} "
            f"confirmed={summary['confirmed']} confirmation_failed={summary['confirmation_failed']} "
            f"receipt_replays={summary['receipt_replays']} receipt_blocked={summary['receipt_blocked']} "
            f"changed={summary['changed']} changed_operations={summary['changed_operations']} "
            f"changed_note_count={summary['changed_note_count']} "
            f"changed_card_count={summary['changed_card_count']}"
        )
    except Exception as exc:
        _legacy.log(f"organization queue exception {type(exc).__name__}: {exc}")
        summary["errors"].append(f"{type(exc).__name__}: {exc}")

    return summary


def _stable_sync_did_finish(parent) -> None:
    parent.log("hook sync_did_finish direct_v2_stable disparou")
    pipeline_owner = "anki_sync_did_finish"
    decision = parent.publication_decision("anki_sync_did_finish")
    parent.log(
        "auto publish decision "
        f"trigger=anki_sync_did_finish mode={decision['mode']} "
        f"configured={decision['configured']} allowed={decision['allowed']} "
        f"reason={decision['reason']}"
    )
    if not decision["allowed"]:
        return
    acquisition = parent.acquire_sync_pipeline(pipeline_owner)
    if not acquisition["acquired"]:
        return

    try:
        if parent.organization_module is None:
            parent.log(f"organization queue unavailable: {parent.ORGANIZATION_IMPORT_ERROR}")
        else:
            parent.log(
                "organization sync hook delegating before snapshot "
                f"module_file={getattr(parent.organization_module, '__file__', '')}"
            )
            parent.organization_module.process_organization_queue()
        parent.process_tagging_queue()
    except Exception as exc:
        parent.log(f"hook queue processing warning {type(exc).__name__}: {exc}")

    try:
        snapshot_ok = parent.post_full_snapshot("anki_sync_did_finish_after_queues")
    except Exception as exc:
        parent.log(f"hook snapshot exception {type(exc).__name__}: {exc}")
        parent.release_sync_pipeline(pipeline_owner)
        return
    if not snapshot_ok:
        parent.release_sync_pipeline(pipeline_owner)
        return

    def after_hook_media_publish(result, error) -> None:
        try:
            if error is not None:
                parent.log(f"hook media publish failed {type(error).__name__}: {error}")
                return
            if not result or not result.get("ok"):
                parent.log(
                    "hook media publish failed "
                    f"error={result.get('error') if result else 'missing_result'} "
                    f"command={result.get('command', '') if result else ''}"
                )
                return
            parent.log("hook media publish concluida")
        finally:
            parent.release_sync_pipeline(pipeline_owner)

    try:
        schedule_result = parent.start_media_publish_step(
            reason="anki_sync_did_finish_media_publish",
            step_label="Publicando midia do sync automatico",
            on_done=after_hook_media_publish,
            dry_run=False,
            show_progress=False,
        )
    except Exception:
        parent.release_sync_pipeline(pipeline_owner)
        raise
    if schedule_result.get("status") != "queued":
        parent.release_sync_pipeline(pipeline_owner)


def _install_parent_sync_hook() -> None:
    parent = sys.modules.get(__package__)
    if parent is None:
        return
    old_hook = getattr(parent, "on_sync_did_finish", None)
    organization_module = getattr(parent, "organization_module", None)
    if not callable(old_hook) or organization_module is None:
        return
    try:
        from aqt import gui_hooks

        hooks = gui_hooks.sync_did_finish
        while old_hook in hooks:
            hooks.remove(old_hook)

        def stable_hook() -> None:
            _stable_sync_did_finish(parent)

        parent.on_sync_did_finish = stable_hook
        hooks.append(stable_hook)
        parent.log("direct_v2 stable sync hook installed")
    except Exception as exc:  # pragma: no cover - depends on Anki runtime
        try:
            parent.log(
                f"direct_v2 stable sync hook install warning {type(exc).__name__}: {exc}"
            )
        except Exception:
            pass


def _schedule_parent_hook_install() -> None:
    try:
        from aqt.qt import QTimer

        QTimer.singleShot(0, _install_parent_sync_hook)
    except Exception:
        pass


_legacy.update_note_fields = update_note_fields
_legacy.execute_organization_operation = execute_organization_operation
_legacy.organization_result_change_summary = organization_result_change_summary
_legacy.process_organization_queue = process_organization_queue


class _MirroredOrganizationModule(types.ModuleType):
    """Keep legacy globals synchronized with public-module monkeypatches.

    Production uses the same objects already, but the regression suite replaces
    dependencies such as mw/get_note/save_collection on the public module. The
    mirror makes those test doubles exercise the real wrapper path instead of
    bypassing it or accidentally reaching Anki runtime objects.
    """

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        legacy = self.__dict__.get("_legacy")
        if legacy is not None and not name.startswith("_") and hasattr(legacy, name):
            setattr(legacy, name, value)


sys.modules[__name__].__class__ = _MirroredOrganizationModule
_schedule_parent_hook_install()
