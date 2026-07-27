from pathlib import Path
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from aqt import mw
import hashlib
import html
import json
import os
import re
import shutil
import unicodedata

try:
    from .html_utils import strip_html_text, strip_visual_wrappers
except Exception:
    from html_utils import strip_html_text, strip_visual_wrappers

try:
    from .runtime_paths import append_log_resilient, get_runtime_paths
except ImportError:
    from runtime_paths import append_log_resilient, get_runtime_paths

try:
    from .note_preconditions import (
        build_note_precondition as build_canonical_note_precondition,
        note_content_hash as canonical_note_content_hash,
    )
except ImportError:
    from note_preconditions import (
        build_note_precondition as build_canonical_note_precondition,
        note_content_hash as canonical_note_content_hash,
    )

RUNTIME_PATHS = get_runtime_paths()
LOG_FILE = RUNTIME_PATHS.log_file
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
ORGANIZATION_API_BASE = "https://gatsby-anki.137.131.191.66.nip.io"
ORGANIZATION_TOKEN_ENV = "ANKI_GPT_TAGGING_TOKEN"
ORGANIZATION_TOKEN_FILE = RUNTIME_PATHS.token_file
REORGANIZATION_LOG_FILE = RUNTIME_PATHS.reorganization_log_file
OPERATIONS_INDEX_FILE = RUNTIME_PATHS.operations_index_file
IDEAL_DECK_TAG = "organizado::deck_ideal"
DESTINATION_TAG_PREFIX = "organizado::destino"
SUPPORTED_CREATE_NOTE_TYPES = {
    "prettify-minimal-basic",
    "prettify-minimal-basic_reverse",
    "prettify-minimal-cloze",
}
DEFAULT_CREATE_NOTE_TAGS = ["GPT"]
MAX_CREATE_NOTES_PER_OPERATION = 20
ORGANIZATION_MAX_OPERATIONS_PER_RUN = 10
ORGANIZATION_OPERATIONS_PANEL_LIMIT = 500
ORGANIZATION_TIMEOUT_SECONDS = 20
OPERATION_MAX_AGE_SECONDS = int(os.environ.get("ANKI_GPT_OPERATION_MAX_AGE_SECONDS", "86400"))
OPERATION_RECEIPT_SCHEMA_VERSION = 1
OPERATION_RECEIPT_TTL_SECONDS = int(os.environ.get("ANKI_GPT_OPERATION_RECEIPT_TTL_SECONDS", str(90 * 86400)))
EXECUTION_MODES = {"preview", "direct"}
# The backend is the configurable source of truth for newly created
# operations. This is only a defensive fallback for malformed schema-v3 data.
NEW_OPERATION_FALLBACK_MODE = "direct"
ORGANIZATION_OPERATION_TYPES = {
    "create_deck",
    "move_cards_to_deck",
    "move_notes_to_deck",
    "get_reorganization_log",
    "undo_reorganization",
    "undo_last_reorganization",
    "mark_notes_as_ideal_deck",
    "mark_cards_as_ideal_deck",
    "check_ideal_deck_status",
    "list_note_types",
    "get_note_type_fields",
    "create_note",
    "create_notes",
    "replace_note_tags",
    "update_note_fields",
    "reorder_cards_by_material",
}
DRY_RUN_CAPABLE_OPERATION_TYPES = {
    "move_cards_to_deck",
    "move_notes_to_deck",
    "create_note",
    "create_notes",
    "replace_note_tags",
    "update_note_fields",
    "reorder_cards_by_material",
}


def validate_operation_age(operation: dict, schema_version: int, dry_run: bool) -> None:
    if schema_version < 2 or dry_run:
        return
    created_at = operation.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("missing_operation_created_at")
    try:
        created = datetime.fromisoformat(created_at.strip().replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.astimezone()
        age = (datetime.now().astimezone() - created.astimezone()).total_seconds()
    except Exception:
        raise ValueError("invalid_operation_created_at") from None
    if age < -300:
        raise ValueError("operation_created_at_in_future")
    if age > OPERATION_MAX_AGE_SECONDS:
        raise ValueError("operation_expired")


class OperationFailedWithResult(Exception):
    def __init__(self, message: str, result: dict):
        super().__init__(message)
        self.result = result


def log(msg: str) -> None:
    append_log_resilient(
        LOG_FILE,
        msg,
        max_bytes=LOG_MAX_BYTES,
        backup_count=LOG_BACKUP_COUNT,
    )


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_organization_token() -> str:
    token = os.environ.get(ORGANIZATION_TOKEN_ENV, "").strip()
    if token:
        return token

    try:
        token = ORGANIZATION_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as e:
        log(
            "organization token file unreadable "
            f"path={ORGANIZATION_TOKEN_FILE} error_type={type(e).__name__}"
        )
        return ""
    if not token or any(character.isspace() for character in token):
        log(f"organization token file invalid path={ORGANIZATION_TOKEN_FILE}")
        return ""
    return token


def organization_api_request(path: str, method: str = "GET", payload=None):
    token = load_organization_token()
    if not token:
        log(
            "organization queue skipped: missing token "
            f"env={ORGANIZATION_TOKEN_ENV} file={ORGANIZATION_TOKEN_FILE}"
        )
        return None

    headers = {"X-Tagging-Token": token}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(
        ORGANIZATION_API_BASE + path,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urlopen(req, timeout=ORGANIZATION_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log(f"organization HTTPError path={path} status={e.code} reason={e.reason} body={body}")
    except URLError as e:
        log(f"organization URLError path={path} reason={e.reason}")
    except Exception as e:
        log(f"organization request exception path={path} {type(e).__name__}: {e}")

    return None


def reorder_order_hash_payload(order: dict) -> dict:
    return {
        "deck": order.get("deck"),
        "target_created_column": order.get("target_created_column"),
        "ordered_note_ids": order.get("ordered_note_ids", []),
        "expected_eligible_card_ids": order.get("expected_eligible_card_ids", []),
    }


def reorder_order_sha256(order: dict) -> str:
    canonical = json.dumps(
        reorder_order_hash_payload(order),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_remote_reorder_order(order_id: str) -> dict:
    if not isinstance(order_id, str) or not order_id.strip():
        raise ValueError("invalid_order_id")
    order_id = order_id.strip()
    response = organization_api_request(f"/organization/reorder-order?order_id={quote(order_id, safe='')}")
    if not isinstance(response, dict):
        raise ValueError("reorder_order_fetch_failed")
    if response.get("error"):
        raise ValueError(f"reorder_order_fetch_failed: {response.get('error')}")
    if response.get("order_id") != order_id:
        raise ValueError("reorder_order_id_mismatch")
    return response


def hydrate_reorder_payload_from_order_id(payload: dict) -> dict:
    order_id = payload.get("order_id")
    if not order_id:
        return payload

    order = load_remote_reorder_order(order_id)
    if order.get("deck") != payload.get("deck"):
        raise ValueError("reorder_order_deck_mismatch")
    if order.get("target_created_column") != payload.get("target_created_column", "note"):
        raise ValueError("reorder_order_target_created_column_mismatch")

    calculated_sha256 = reorder_order_sha256(order)
    expected_sha256 = payload.get("order_sha256")
    if expected_sha256 and expected_sha256 != calculated_sha256:
        raise ValueError("reorder_order_sha256_mismatch")
    if order.get("sha256") and order.get("sha256") != calculated_sha256:
        raise ValueError("reorder_order_sha256_mismatch")

    ordered_note_ids = order.get("ordered_note_ids")
    if not isinstance(ordered_note_ids, list) or not ordered_note_ids:
        raise ValueError("reorder_order_missing_ordered_note_ids")
    expected_eligible_card_ids = order.get("expected_eligible_card_ids", [])
    if expected_eligible_card_ids is None:
        expected_eligible_card_ids = []
    if not isinstance(expected_eligible_card_ids, list):
        raise ValueError("reorder_order_invalid_expected_eligible_card_ids")

    if payload.get("ordered_note_ids_count") is not None and int(payload.get("ordered_note_ids_count")) != len(ordered_note_ids):
        raise ValueError("reorder_order_ordered_note_ids_count_mismatch")
    if (
        payload.get("expected_eligible_card_ids_count") is not None
        and int(payload.get("expected_eligible_card_ids_count")) != len(expected_eligible_card_ids)
    ):
        raise ValueError("reorder_order_expected_eligible_card_ids_count_mismatch")

    hydrated = dict(payload)
    hydrated["ordered_note_ids"] = ordered_note_ids
    hydrated["expected_eligible_card_ids"] = expected_eligible_card_ids
    hydrated["order_sha256"] = calculated_sha256
    log(
        "organization reorder order loaded "
        f"order_id={order_id} ordered_note_ids={len(ordered_note_ids)} "
        f"expected_eligible_card_ids={len(expected_eligible_card_ids)}"
    )
    return hydrated


def note_field_updates_hash_payload(updates: dict) -> dict:
    return {
        "note_updates": updates.get("note_updates", []),
    }


def note_field_updates_sha256(updates: dict) -> str:
    canonical = json.dumps(
        note_field_updates_hash_payload(updates),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_remote_note_field_updates(updates_id: str) -> dict:
    if not isinstance(updates_id, str) or not updates_id.strip():
        raise ValueError("invalid_updates_id")
    updates_id = updates_id.strip()
    response = organization_api_request(
        f"/organization/note-field-updates?updates_id={quote(updates_id, safe='')}"
    )
    if not isinstance(response, dict):
        raise ValueError("note_field_updates_fetch_failed")
    if response.get("error"):
        raise ValueError(f"note_field_updates_fetch_failed: {response.get('error')}")
    if response.get("updates_id") != updates_id:
        raise ValueError("note_field_updates_id_mismatch")
    return response


def hydrate_update_note_fields_payload_from_updates_id(payload: dict) -> dict:
    updates_id = payload.get("updates_id")
    if not updates_id:
        return payload

    updates = load_remote_note_field_updates(updates_id)
    calculated_sha256 = note_field_updates_sha256(updates)
    expected_sha256 = payload.get("updates_sha256")
    if expected_sha256 and expected_sha256 != calculated_sha256:
        raise ValueError("note_field_updates_sha256_mismatch")
    if updates.get("sha256") and updates.get("sha256") != calculated_sha256:
        raise ValueError("note_field_updates_sha256_mismatch")

    note_updates = updates.get("note_updates")
    if not isinstance(note_updates, list) or not note_updates:
        raise ValueError("note_field_updates_missing_note_updates")
    if payload.get("note_updates_count") is not None and int(payload.get("note_updates_count")) != len(note_updates):
        raise ValueError("note_field_updates_count_mismatch")
    note_ids = [item.get("note_id") for item in note_updates if isinstance(item, dict)]
    if payload.get("note_ids_count") is not None and int(payload.get("note_ids_count")) != len(set(note_ids)):
        raise ValueError("note_field_updates_note_ids_count_mismatch")

    hydrated = dict(payload)
    hydrated["note_updates"] = note_updates
    hydrated["updates_sha256"] = calculated_sha256
    log(
        "organization note field updates loaded "
        f"updates_id={updates_id} note_updates={len(note_updates)} "
        f"note_ids={len(set(note_ids))}"
    )
    return hydrated


def empty_operations_index() -> dict:
    return {
        "version": 1,
        "updated_at": None,
        "operations": {},
    }


def load_operations_index() -> dict:
    if not OPERATIONS_INDEX_FILE.exists():
        return empty_operations_index()

    try:
        loaded = json.loads(OPERATIONS_INDEX_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log(
            "organization operations index parse warning "
            f"path={OPERATIONS_INDEX_FILE} {type(e).__name__}: {e}"
        )
        return empty_operations_index()

    if isinstance(loaded, list):
        operations = {}
        for item in loaded:
            if isinstance(item, dict):
                operation_id = normalize_operation_id(item)
                if operation_id:
                    operations[operation_id] = item
        return {
            "version": 1,
            "updated_at": None,
            "operations": operations,
        }

    if not isinstance(loaded, dict):
        log(f"organization operations index invalid root path={OPERATIONS_INDEX_FILE}")
        return empty_operations_index()

    operations = loaded.get("operations", {})
    if isinstance(operations, list):
        operations = {
            normalize_operation_id(item): item
            for item in operations
            if isinstance(item, dict) and normalize_operation_id(item)
        }
    if not isinstance(operations, dict):
        operations = {}

    loaded["version"] = loaded.get("version", 1)
    loaded["operations"] = {
        str(operation_id): entry
        for operation_id, entry in operations.items()
        if operation_id and isinstance(entry, dict)
    }
    return loaded


def save_operations_index(index: dict) -> None:
    if not isinstance(index, dict):
        index = empty_operations_index()
    operations = index.get("operations")
    if not isinstance(operations, dict):
        operations = {}
    index["version"] = 1
    index["updated_at"] = now_iso()
    index["operations"] = operations
    atomic_write_json(OPERATIONS_INDEX_FILE, index)


def normalize_operation_id(operation: dict) -> str:
    if not isinstance(operation, dict):
        return ""
    for key in ("operation_id", "op_id", "id", "batch_id"):
        value = operation.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def operation_payload(operation: dict) -> dict:
    payload = operation.get("payload") if isinstance(operation, dict) else {}
    return payload if isinstance(payload, dict) else {}


def normalize_operation_type(operation: dict) -> str:
    payload = operation_payload(operation)
    for source in (operation, payload):
        for key in ("operation_type", "type", "function", "operation"):
            value = source.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return ""


def id_list_summary(values) -> dict:
    if not isinstance(values, list):
        values = []
    return {
        "count": len(values),
        "first": values[:5],
        "last": values[-5:],
    }


def compact_reorder_operation_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    compact = dict(payload)
    for key in ("ordered_note_ids", "ordered_card_ids", "expected_eligible_card_ids"):
        if isinstance(compact.get(key), list):
            compact[f"{key}_summary"] = id_list_summary(compact.get(key))
            compact.pop(key, None)
    return compact


def compact_reorder_result_payload(result_payload: dict) -> dict:
    if not isinstance(result_payload, dict):
        return result_payload

    scalar_keys = [
        "operation",
        "execution_mode",
        "dry_run",
        "batch_id",
        "operation_id",
        "deck",
        "target_created_column",
        "ordering_unit",
        "multi_card_note_policy",
        "base_datetime",
        "effective_base_datetime",
        "spacing_ms",
        "scope",
        "apply_created_date",
        "apply_note_created_date",
        "applied_count",
        "skipped_count",
        "total_notes_reordered",
        "total_cards_reordered",
        "total_notes_skipped",
        "total_cards_skipped",
        "planned_count",
        "reorder_log_path",
        "errors",
        "warnings",
        "preconditions",
        "atomic",
        "rolled_back",
        "rollback_errors",
        "preconditions_required",
        "post_apply_verification",
        "post_apply_created_audit",
        "backup",
        "ordered_note_ids",
        "ordered_card_ids",
        "eligible_card_ids",
        "expected_eligible_card_ids",
        "planned_new_cids",
        "planned_new_nids",
        "skipped",
        "reorder_log_entries",
    ]
    compact = {
        key: result_payload[key]
        for key in scalar_keys
        if key in result_payload
    }
    for key in (
        "ordered_note_ids",
        "ordered_card_ids",
        "eligible_card_ids",
        "expected_eligible_card_ids",
    ):
        if key in result_payload and key not in compact:
            compact[key] = result_payload.get(key)
    if "proposed_order" in result_payload:
        proposed_order = result_payload.get("proposed_order") or []
        compact["proposed_order_count"] = len(proposed_order)
        compact["proposed_order_audit"] = [
            {
                key: item.get(key)
                for key in (
                    "position",
                    "nid",
                    "old_nid",
                    "new_nid",
                    "cid",
                    "old_cid",
                    "new_cid",
                    "note_card_ids",
                    "eligible",
                    "warnings",
                )
                if key in item
            }
            for item in proposed_order
            if isinstance(item, dict)
        ]
    if "diagnostic_summary" in result_payload:
        compact["diagnostic_summary"] = result_payload.get("diagnostic_summary")
    return compact


def compact_update_note_fields_operation_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    compact = dict(payload)
    if isinstance(compact.get("note_updates"), list):
        compact["note_updates_audit"] = [
            {
                "note_id": item.get("note_id"),
                "field_names": sorted((item.get("fields") or {}).keys()),
                **{
                    key: item.get(key)
                    for key in (
                        "expected_content_hash",
                        "expected_mod",
                        "expected_usn",
                        "expected_model_id",
                    )
                    if key in item
                },
            }
            for item in compact.get("note_updates")
            if isinstance(item, dict)
        ]
        compact["note_updates_summary"] = id_list_summary(
            [item["note_id"] for item in compact["note_updates_audit"]]
        )
        compact.pop("note_updates", None)
    return compact


def compact_update_note_fields_result_payload(result_payload: dict) -> dict:
    if not isinstance(result_payload, dict):
        return result_payload

    scalar_keys = [
        "operation",
        "execution_mode",
        "dry_run",
        "requested_count",
        "changed_count",
        "note_updates_count",
        "note_ids_count",
        "updates_id",
        "updates_sha256",
        "errors",
        "warnings",
        "apply_preconditions",
        "atomic",
        "rolled_back",
        "rollback_errors",
        "undo_available",
        "undo_label",
        "undo_entry",
        "preconditions_required",
        "planned_note_ids",
        "affected_note_ids",
        "changed_note_ids",
        "notes",
        "preconditions",
    ]
    compact = {
        key: result_payload[key]
        for key in scalar_keys
        if key in result_payload
    }
    for key in ("note_ids", "changed_note_ids", "affected_note_ids", "planned_note_ids"):
        if key in result_payload and key not in compact:
            compact[key] = result_payload.get(key)
    if "notes" in compact:
        compact["notes_count"] = len(compact.get("notes") or [])
    return compact


def compact_organization_operation_for_index(operation: dict) -> dict:
    operation_type = normalize_operation_type(operation)
    payload = operation_payload(operation)
    if operation_type == "reorder_cards_by_material" and payload.get("order_id"):
        compact = dict(operation)
        compact["payload"] = compact_reorder_operation_payload(payload)
        return compact
    if operation_type == "update_note_fields" and payload.get("updates_id"):
        compact = dict(operation)
        compact["payload"] = compact_update_note_fields_operation_payload(payload)
        return compact
    return operation


def compact_organization_result_for_storage(result: dict) -> dict:
    if not isinstance(result, dict):
        return result
    operation_result = result.get("result")
    if not isinstance(operation_result, dict):
        return result
    if result.get("operation_type") == "reorder_cards_by_material" and operation_result.get("order_id"):
        compact = dict(result)
        compact["result"] = compact_reorder_result_payload(operation_result)
        return compact
    if result.get("operation_type") == "update_note_fields" and operation_result.get("updates_id"):
        compact = dict(result)
        compact["result"] = compact_update_note_fields_result_payload(operation_result)
        return compact
    return result


def canonical_sha256(value) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def operation_receipt_operation_payload(operation: dict) -> dict:
    schema_version = int(operation.get("operation_schema_version") or 1)
    payload = dict(operation_payload(operation))
    if schema_version < 3:
        payload.pop("execution_mode", None)
    receipt_payload = {
        "operation_id": normalize_operation_id(operation),
        "operation_type": normalize_operation_type(operation),
        "operation_schema_version": schema_version,
        "confirmed_by_user": operation.get("confirmed_by_user") is True,
        "payload": payload,
    }
    if schema_version >= 3:
        receipt_payload["execution_mode"] = operation_execution_mode(operation)
    return receipt_payload


def operation_receipt_preconditions_hash(operation: dict) -> str:
    payload = operation_payload(operation)
    hashes = []
    for update in payload.get("note_updates", []) if isinstance(payload.get("note_updates"), list) else []:
        if isinstance(update, dict) and isinstance(update.get("expected_content_hash"), str):
            hashes.append(update["expected_content_hash"])
    material = {
        "updates_id": payload.get("updates_id"),
        "updates_sha256": payload.get("updates_sha256"),
        "expected_content_hashes": sorted(hashes),
    }
    return canonical_sha256(material)


def build_operation_receipt(operation: dict, result: dict, now_value=None):
    payload = operation_payload(operation)
    if operation_execution_mode(operation) == "preview":
        return None
    if not isinstance(result, dict) or result.get("status") not in {"done", "partially_applied"}:
        return None
    confirmation_payload = compact_organization_result_for_storage(result)
    operation_hash = canonical_sha256(operation_receipt_operation_payload(operation))
    result_hash = canonical_sha256(confirmation_payload)
    applied_at = now_value or datetime.now().astimezone()
    expires_at = applied_at + timedelta(seconds=max(OPERATION_RECEIPT_TTL_SECONDS, 1))
    receipt_id = canonical_sha256({
        "operation_id": normalize_operation_id(operation),
        "operation_hash": operation_hash,
        "result_hash": result_hash,
    })
    return {
        "schema_version": OPERATION_RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "operation_id": normalize_operation_id(operation),
        "operation_type": normalize_operation_type(operation),
        "updates_id": payload.get("updates_id"),
        "applied_at": applied_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "operation_hash": operation_hash,
        "result_hash": result_hash,
        "preconditions_hash": operation_receipt_preconditions_hash(operation),
        "confirmation_payload": confirmation_payload,
    }


def receipt_confirmation_metadata(receipt: dict | None):
    if not isinstance(receipt, dict):
        return None
    return {
        key: receipt.get(key)
        for key in (
            "schema_version",
            "receipt_id",
            "operation_id",
            "operation_type",
            "updates_id",
            "applied_at",
            "expires_at",
            "operation_hash",
            "result_hash",
            "preconditions_hash",
        )
    }


def validate_operation_receipt(operation: dict, receipt: dict | None, now_value=None) -> dict:
    if not isinstance(receipt, dict):
        return {"state": "missing", "receipt": None}
    if receipt.get("schema_version") != OPERATION_RECEIPT_SCHEMA_VERSION:
        return {"state": "invalid", "receipt": receipt, "reason": "receipt_schema_mismatch"}
    if receipt.get("operation_id") != normalize_operation_id(operation):
        return {"state": "collision", "receipt": receipt, "reason": "receipt_operation_id_mismatch"}
    if receipt.get("operation_type") != normalize_operation_type(operation):
        return {"state": "collision", "receipt": receipt, "reason": "receipt_operation_type_mismatch"}
    expected_operation_hash = canonical_sha256(operation_receipt_operation_payload(operation))
    if receipt.get("operation_hash") != expected_operation_hash:
        return {"state": "collision", "receipt": receipt, "reason": "receipt_operation_hash_mismatch"}
    confirmation_payload = receipt.get("confirmation_payload")
    if not isinstance(confirmation_payload, dict) or receipt.get("result_hash") != canonical_sha256(confirmation_payload):
        return {"state": "invalid", "receipt": receipt, "reason": "receipt_result_hash_mismatch"}
    try:
        expires_at = datetime.fromisoformat(str(receipt.get("expires_at")))
        current = now_value or datetime.now().astimezone()
        if expires_at.tzinfo is None:
            expires_at = expires_at.astimezone()
    except (TypeError, ValueError):
        return {"state": "invalid", "receipt": receipt, "reason": "receipt_expiry_invalid"}
    if current >= expires_at:
        return {"state": "expired", "receipt": receipt, "reason": "receipt_expired"}
    return {"state": "valid", "receipt": receipt, "result": confirmation_payload}


def local_operation_receipt_state(operation: dict) -> dict:
    operation_id = normalize_operation_id(operation)
    index = load_operations_index()
    entry = index.get("operations", {}).get(operation_id, {})
    receipt = entry.get("receipt") if isinstance(entry, dict) else None
    return validate_operation_receipt(operation, receipt)


def operation_target_summary(operation: dict) -> str:
    payload = operation_payload(operation)
    for key in ("target_deck", "deck", "deck_name", "target"):
        value = payload.get(key, operation.get(key) if isinstance(operation, dict) else None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for nested_key in ("deck", "deck_name", "target_deck", "name"):
                nested_value = value.get(nested_key)
                if isinstance(nested_value, str) and nested_value.strip():
                    return nested_value.strip()

    count_parts = []
    for key, label in (
        ("card_ids", "cards"),
        ("note_ids", "notes"),
        ("ordered_card_ids", "cards ordenados"),
        ("ordered_note_ids", "notes ordenadas"),
    ):
        value = payload.get(key)
        if isinstance(value, list) and value:
            count_parts.append(f"{label}: {len(value)}")
    return ", ".join(count_parts)


def operation_created_at(operation: dict, existing: dict | None = None) -> str:
    existing = existing if isinstance(existing, dict) else {}
    for source in (operation, operation_payload(operation), existing):
        for key in ("created_at", "created", "requested_at", "timestamp"):
            value = source.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return now_iso()


def operation_updated_at(operation: dict, existing: dict | None = None) -> str:
    existing = existing if isinstance(existing, dict) else {}
    for source in (operation, operation_payload(operation), existing):
        for key in ("updated_at", "updated", "finished_at", "processed_at"):
            value = source.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return now_iso()


def normalize_execution_mode(*sources, default=NEW_OPERATION_FALLBACK_MODE) -> str:
    """Normalize execution intent without conflating it with queue state."""
    normalized_default = str(default or "").strip().lower()
    if normalized_default not in EXECUTION_MODES:
        raise ValueError("invalid_default_execution_mode")

    modes = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        if "execution_mode" in source:
            mode = source.get("execution_mode")
            if not isinstance(mode, str) or mode.strip().lower() not in EXECUTION_MODES:
                raise ValueError("invalid_execution_mode: expected preview or direct")
            modes.append(mode.strip().lower())
        if "dry_run" in source:
            dry_run = source.get("dry_run")
            if not isinstance(dry_run, bool):
                raise ValueError("invalid_dry_run")
            modes.append("preview" if dry_run else "direct")

    if len(set(modes)) > 1:
        raise ValueError(
            "execution_mode_dry_run_mismatch: preview requires dry_run=true "
            "and direct requires dry_run=false"
        )
    return modes[0] if modes else normalized_default


def operation_execution_mode(operation: dict) -> str:
    if not isinstance(operation, dict):
        return NEW_OPERATION_FALLBACK_MODE
    raw_operation = operation.get("raw_operation")
    operation_type = normalize_operation_type(operation)
    schema_version = int(
        operation.get("operation_schema_version")
        or (
            raw_operation.get("operation_schema_version")
            if isinstance(raw_operation, dict)
            else 1
        )
        or 1
    )
    legacy_default = (
        "preview"
        if schema_version < 3 and operation_type in DRY_RUN_CAPABLE_OPERATION_TYPES
        else "direct"
    )
    candidates = [
        operation,
        operation_payload(operation),
        raw_operation,
        operation_payload(raw_operation or {}),
        operation.get("last_result"),
        (operation.get("last_result") or {}).get("result")
        if isinstance(operation.get("last_result"), dict) else None,
        operation.get("result"),
    ]
    return normalize_execution_mode(*candidates, default=legacy_default)


def operation_dry_run(operation: dict):
    return operation_execution_mode(operation) == "preview"


def normalize_operation_state(raw_status) -> str:
    status = str(raw_status or "").strip().lower()
    aliases = {
        "pending_addon_execution": "pending",
        "queued": "pending",
        "processing": "running",
        "in_progress": "running",
        "completed": "done",
        "success": "done",
        "succeeded": "done",
        "applied": "done",
        "error": "failed",
    }
    return aliases.get(status, status or "unknown")


def operation_status(operation: dict) -> str:
    if not isinstance(operation, dict):
        return "unknown"

    raw_status = str(operation.get("status", "") or "").strip().lower()
    if raw_status not in {"", "dry_run", "applied"}:
        return normalize_operation_state(raw_status)

    # Compatibility with index entries written before mode and state were
    # separated. Prefer a completed local result/confirmation over the stale
    # pending status embedded in the originally fetched operation.
    last_result = operation.get("last_result")
    if isinstance(last_result, dict) and last_result.get("status"):
        return normalize_operation_state(last_result.get("status"))

    raw_operation = operation.get("raw_operation")
    for source in (operation, raw_operation):
        if not isinstance(source, dict):
            continue
        for key in ("execution_result", "execution_confirmation"):
            execution_result = source.get(key)
            if isinstance(execution_result, dict) and execution_result.get("status"):
                return normalize_operation_state(execution_result.get("status"))

    if raw_status in {"dry_run", "applied"}:
        return "done"
    if isinstance(raw_operation, dict) and raw_operation.get("status"):
        return normalize_operation_state(raw_operation.get("status"))
    return "unknown"


def result_status(result: dict, operation: dict | None = None) -> str:
    if not isinstance(result, dict):
        return "unknown"
    if result.get("status"):
        return normalize_operation_state(result.get("status"))
    return "done" if result.get("ok") else "failed"


def operation_mode_label(operation: dict) -> str:
    try:
        return (
            "Prévia"
            if operation_execution_mode(operation) == "preview"
            else "Aplicação real"
        )
    except ValueError:
        return "Modo inválido"


def operation_state_label(operation: dict) -> str:
    status = operation_status(operation)
    labels = {
        "pending": "Pendente",
        "running": "Em processamento",
        "done": "Concluída",
        "failed": "Falhou",
        "partially_applied": "Parcialmente aplicada",
        "skipped": "Ignorada",
        "unknown": "Desconhecido",
    }
    return labels.get(status, str(status or "unknown"))


def operation_result_payload(operation: dict) -> dict:
    if not isinstance(operation, dict):
        return {}
    for candidate in (
        operation.get("result"),
        (operation.get("last_result") or {}).get("result")
        if isinstance(operation.get("last_result"), dict) else None,
        (operation.get("raw_operation") or {}).get("result")
        if isinstance(operation.get("raw_operation"), dict) else None,
    ):
        if isinstance(candidate, dict):
            return candidate
    return {}


def operation_result_label(operation: dict) -> str:
    result = operation_result_payload(operation)
    count = result.get("changed_count")
    if isinstance(count, bool) or not isinstance(count, int):
        return ""
    if operation_dry_run(operation) is True:
        return f"{count} alteração prevista" if count == 1 else f"{count} alterações previstas"
    return f"{count} note alterada" if count == 1 else f"{count} notes alteradas"


def operation_error_label(operation: dict) -> str:
    if not isinstance(operation, dict):
        return ""
    errors = []
    for source in (
        operation,
        operation.get("execution_result"),
        operation.get("execution_confirmation"),
        operation.get("last_result"),
        operation.get("result"),
        (operation.get("raw_operation") or {}).get("execution_confirmation")
        if isinstance(operation.get("raw_operation"), dict) else None,
        (operation.get("raw_operation") or {}).get("execution_result")
        if isinstance(operation.get("raw_operation"), dict) else None,
    ):
        if not isinstance(source, dict):
            continue
        raw_errors = source.get("errors")
        if isinstance(raw_errors, list):
            errors.extend(str(item) for item in raw_errors if str(item).strip())
        elif source.get("error"):
            errors.append(str(source.get("error")))
        elif source.get("last_error"):
            errors.append(str(source.get("last_error")))
    return "; ".join(dict.fromkeys(errors))


def operation_last_error(operation: dict, result: dict | None = None) -> str:
    if isinstance(result, dict):
        errors = result.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(item) for item in errors[:3])
        if result.get("error"):
            return str(result.get("error"))

    for key in ("last_error", "error", "error_summary", "message"):
        value = operation.get(key) if isinstance(operation, dict) else None
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def upsert_operation_index(
    operation: dict,
    *,
    result: dict | None = None,
    status: str | None = None,
    phase: str = "",
    receipt: dict | None = None,
) -> None:
    operation_id = normalize_operation_id(operation)
    if not operation_id:
        log("organization operations index skipped operation without id")
        return

    index = load_operations_index()
    operations = index.setdefault("operations", {})
    existing = operations.get(operation_id, {})
    if not isinstance(existing, dict):
        existing = {}

    stored_receipt = existing.get("receipt") if isinstance(existing.get("receipt"), dict) else None
    receipt_state = validate_operation_receipt(operation, stored_receipt)
    if phase == "fetched" and receipt_state["state"] in {"valid", "expired", "collision", "invalid"}:
        resolved_status = operation_status(existing)
    else:
        resolved_status = status or (result_status(result, operation) if result is not None else operation_status(operation))
    updated_at = now_iso()
    history = existing.get("history", [])
    if not isinstance(history, list):
        history = []
    history.append({
        "at": updated_at,
        "phase": phase,
        "status": resolved_status,
    })
    history = history[-25:]

    stored_operation = compact_organization_operation_for_index(operation)
    stored_result = compact_organization_result_for_storage(result) if result is not None else None

    entry = dict(existing)
    entry.update({
        "operation_id": operation_id,
        "op_id": operation_id,
        "operation_type": normalize_operation_type(operation) or existing.get("operation_type", ""),
        "target": operation_target_summary(operation) or existing.get("target", ""),
        "created_at": operation_created_at(operation, existing),
        "updated_at": updated_at,
        "source_updated_at": operation_updated_at(operation, existing),
        "status": resolved_status,
        "last_error": operation_last_error(operation, result),
        "raw_operation": stored_operation,
        "history": history,
    })
    dry_run = operation_dry_run(operation)
    if dry_run is None and result is not None:
        dry_run = operation_dry_run(result)
    if dry_run is not None:
        entry["dry_run"] = dry_run
        entry["execution_mode"] = "preview" if dry_run else "direct"
    if stored_result is not None:
        entry["last_result"] = stored_result
    if receipt is not None:
        entry["receipt"] = receipt

    operations[operation_id] = entry
    save_operations_index(index)
    log(
        "organization operations index upsert "
        f"operation_id={operation_id} operation_type={entry.get('operation_type')} "
        f"status={resolved_status} phase={phase} path={OPERATIONS_INDEX_FILE}"
    )


def fetch_remote_organization_operations(
    limit: int | None = None,
    status_filter: str = "pending",
):
    operation_limit = ORGANIZATION_MAX_OPERATIONS_PER_RUN if limit is None else limit
    status_filter = str(status_filter or "pending").strip() or "pending"
    response = organization_api_request(
        f"/organization/operations?status={quote(status_filter)}&limit={operation_limit}"
    )
    if not response:
        return None

    operations = response.get("operations", [])
    if not isinstance(operations, list):
        log("organization operations fetch invalid response")
        return []

    for operation in operations:
        if isinstance(operation, dict):
            upsert_operation_index(operation, phase="fetched")
    return [operation for operation in operations if isinstance(operation, dict)]


def index_entry_from_reorganization_batch(batch: dict) -> dict:
    operation_id = normalize_operation_id(batch)
    target = ""
    new_decks = batch.get("new_decks")
    if isinstance(new_decks, list) and new_decks:
        target = ", ".join(str(item) for item in new_decks[:3])

    timestamp = str(batch.get("timestamp", "") or now_iso())
    operation_type = str(batch.get("operation_type", "") or "")
    if not operation_type and batch.get("reordered_count"):
        operation_type = "reorder_cards_by_material"

    return {
        "operation_id": operation_id,
        "op_id": operation_id,
        "operation_type": operation_type,
        "target": target,
        "created_at": timestamp,
        "updated_at": timestamp,
        "status": "applied",
        "last_error": "",
        "raw_operation": batch,
        "source": "reorganization_log",
    }


def list_known_operations(refresh_remote: bool = False) -> list[dict]:
    if refresh_remote:
        remote_operations = fetch_remote_organization_operations(
            limit=ORGANIZATION_OPERATIONS_PANEL_LIMIT,
            status_filter="all",
        )
        if remote_operations is None:
            raise RuntimeError("organization_operations_refresh_failed")

    index = load_operations_index()
    operations_by_id = {
        operation_id: dict(entry)
        for operation_id, entry in index.get("operations", {}).items()
        if isinstance(entry, dict)
    }

    try:
        for batch in reorganization_batches():
            operation_id = normalize_operation_id(batch)
            if operation_id and operation_id not in operations_by_id:
                operations_by_id[operation_id] = index_entry_from_reorganization_batch(batch)
    except Exception as e:
        log(f"organization operations log merge warning {type(e).__name__}: {e}")

    operations = list(operations_by_id.values())
    operations.sort(
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )
    return operations


def remove_operation_from_local_index(operation_id: str) -> dict:
    operation_id = str(operation_id or "").strip()
    if not operation_id:
        return {"ok": False, "error": "operation_id_empty"}

    index = load_operations_index()
    operations = index.get("operations", {})
    if not isinstance(operations, dict) or operation_id not in operations:
        log(f"organization operations index remove missing operation_id={operation_id}")
        return {"ok": False, "error": "operation_not_found"}

    entry = operations.pop(operation_id)
    save_operations_index(index)
    log(
        "organization operations index removed "
        f"operation_id={operation_id} previous_status={entry.get('status')} path={OPERATIONS_INDEX_FILE}"
    )
    return {
        "ok": True,
        "operation_id": operation_id,
        "previous_status": entry.get("status"),
        "index_path": str(OPERATIONS_INDEX_FILE),
    }


def deck_names() -> set[str]:
    return {item.name for item in mw.col.decks.all_names_and_ids()}


def get_deck_name_map() -> dict:
    return {item.id: item.name for item in mw.col.decks.all_names_and_ids()}


def get_model(model_name: str) -> dict:
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError("model_name_empty")
    model_name = model_name.strip()
    try:
        model = mw.col.models.by_name(model_name)
    except Exception:
        model = None
    if model is None:
        raise ValueError(f"note_type_not_found: {model_name}")
    return model


def model_field_names(model: dict) -> list[str]:
    return [str(field.get("name", "")) for field in model.get("flds", [])]


def model_template_names(model: dict) -> list[str]:
    return [str(template.get("name", "")) for template in model.get("tmpls", [])]


def normalize_int_ids(raw_ids, field_name: str) -> list[int]:
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError(f"{field_name}_empty")
    ids = []
    seen = set()
    for raw_id in raw_ids:
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            raise ValueError(f"invalid_{field_name}")
        if raw_id not in seen:
            seen.add(raw_id)
            ids.append(raw_id)
    return ids


def normalize_tags(raw_tags) -> list[str]:
    if raw_tags is None:
        return []
    if not isinstance(raw_tags, list):
        raise ValueError("invalid_add_tags")
    tags = []
    for raw_tag in raw_tags:
        if not isinstance(raw_tag, str) or not raw_tag.strip():
            raise ValueError("invalid_add_tag")
        tag = raw_tag.strip()
        if tag not in tags:
            tags.append(tag)
    return tags


def normalize_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("invalid_boolean_option")
    return value


def deck_slug_component(value: str) -> str:
    text = unicodedata.normalize("NFD", value.strip().lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def deck_name_to_tag_slug(deck_name: str) -> str:
    if not isinstance(deck_name, str) or not deck_name.strip():
        raise ValueError("deck_name_empty")
    parts = [deck_slug_component(part) for part in deck_name.strip().split("::")]
    parts = [part for part in parts if part]
    if not parts:
        raise ValueError("deck_slug_empty")
    return "::".join(parts)


def ideal_destination_tag(deck_name: str) -> str:
    return f"{DESTINATION_TAG_PREFIX}::{deck_name_to_tag_slug(deck_name)}"


def ideal_deck_tags(deck_name: str) -> list[str]:
    return [IDEAL_DECK_TAG, ideal_destination_tag(deck_name)]


def relevant_ideal_tags(tags: list[str]) -> list[str]:
    return [
        tag for tag in tags
        if tag == IDEAL_DECK_TAG or tag.startswith(f"{DESTINATION_TAG_PREFIX}::")
    ]


def destination_tags(tags: list[str]) -> list[str]:
    return [
        tag for tag in tags
        if tag.startswith(f"{DESTINATION_TAG_PREFIX}::")
    ]


def comparable_text(value) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text


def normalize_note_fields_for_compare(values: list[str]) -> str:
    return "\x1f".join(comparable_text(value) for value in values)


def find_cloze_numbers_in_text(text: str) -> list[int]:
    numbers = []
    for match in re.finditer(r"\{\{c(\d+)::.+?\}\}", text or "", flags=re.DOTALL):
        number = int(match.group(1))
        if number not in numbers:
            numbers.append(number)
    return sorted(numbers)


def detect_broken_cloze_syntax(text: str) -> list[str]:
    text = text or ""
    errors = []
    if text.count("{{") != text.count("}}"):
        errors.append("unbalanced_cloze_braces")
    for match in re.finditer(r"\{\{c[^}]*", text):
        candidate = match.group(0)
        end = text.find("}}", match.start())
        if end == -1:
            errors.append("unterminated_cloze")
            continue
        candidate = text[match.start(): end + 2]
        if not re.match(r"\{\{c\d+::.+?\}\}$", candidate, flags=re.DOTALL):
            errors.append(f"invalid_cloze_syntax: {candidate[:80]}")
    return errors


def get_note(note_id: int):
    try:
        return mw.col.get_note(note_id)
    except Exception as e:
        raise ValueError(f"note_not_found: {note_id}") from e


def get_card(card_id: int):
    try:
        return mw.col.get_card(card_id)
    except Exception as e:
        raise ValueError(f"card_not_found: {card_id}") from e


def card_attr(card, name: str, default=None):
    return getattr(card, name, default)


def scheduling_snapshot(card) -> dict:
    return {
        "queue": card_attr(card, "queue", None),
        "type": card_attr(card, "type", None),
        "due": card_attr(card, "due", None),
        "ivl": card_attr(card, "ivl", None),
        "factor": card_attr(card, "factor", None),
        "reps": card_attr(card, "reps", None),
        "lapses": card_attr(card, "lapses", None),
        "left": card_attr(card, "left", None),
        "odue": card_attr(card, "odue", None),
        "odid": card_attr(card, "odid", None),
    }


def note_cards(note_id: int) -> list:
    card_ids = mw.col.db.list(
        """
        select id
        from cards
        where nid = ?
        order by ord, id
        """,
        note_id,
    )
    return [get_card(int(card_id)) for card_id in card_ids]


def template_name_for_card(card, note):
    try:
        model = mw.col.models.get(note.mid)
        templates = model.get("tmpls", []) if model else []
        ord_value = int(card_attr(card, "ord", -1))
        if 0 <= ord_value < len(templates):
            name = templates[ord_value].get("name")
            return str(name) if name is not None else None
    except Exception as e:
        log(f"organization template lookup warning card_id={card_attr(card, 'id', '')}: {type(e).__name__}: {e}")
    return None


def note_type_name(note) -> str:
    try:
        model = mw.col.models.get(note.mid)
        return str(model.get("name", "")) if model else ""
    except Exception:
        return ""


def card_preview(card, target_deck: str, deck_name_map: dict, note_cache=None) -> dict:
    note_id = int(card_attr(card, "nid"))
    note_cache = note_cache if note_cache is not None else {}
    note = note_cache.get(note_id)
    if note is None:
        note = get_note(note_id)
        note_cache[note_id] = note

    sibling_cards = note_cards(note_id)
    sibling_deck_ids = sorted({int(card_attr(sibling, "did")) for sibling in sibling_cards})
    current_deck_id = int(card_attr(card, "did"))
    current_deck = deck_name_map.get(current_deck_id, "")

    return {
        "card_id": int(card_attr(card, "id")),
        "note_id": note_id,
        "ord": int(card_attr(card, "ord")),
        "template_name": template_name_for_card(card, note),
        "note_type": note_type_name(note),
        "tags": list(note.tags),
        "current_deck_id": current_deck_id,
        "current_deck": current_deck,
        "target_deck": target_deck,
        "queue": int(card_attr(card, "queue")),
        "type": int(card_attr(card, "type")),
        "due": int(card_attr(card, "due")),
        "ivl": int(card_attr(card, "ivl")),
        "factor": int(card_attr(card, "factor")),
        "reps": int(card_attr(card, "reps")),
        "lapses": int(card_attr(card, "lapses")),
        "left": card_attr(card, "left", None),
        "odid": int(card_attr(card, "odid", 0)),
        "odue": int(card_attr(card, "odue", 0)),
        "note_card_count": len(sibling_cards),
        "note_has_multiple_cards": len(sibling_cards) > 1,
        "note_cards_in_multiple_decks": len(sibling_deck_ids) > 1,
        "note_card_ids": [int(card_attr(sibling, "id")) for sibling in sibling_cards],
        "note_card_decks": [
            {
                "deck_id": deck_id,
                "deck": deck_name_map.get(deck_id, ""),
            }
            for deck_id in sibling_deck_ids
        ],
    }


def ensure_deck(deck_name: str, dry_run: bool) -> dict:
    if not isinstance(deck_name, str) or not deck_name.strip():
        raise ValueError("target_deck_empty")

    deck_name = deck_name.strip()
    existing = deck_name in deck_names()
    if dry_run:
        return {
            "deck": deck_name,
            "deck_id": None,
            "created": False,
            "would_create": not existing,
        }

    if existing:
        deck_id = mw.col.decks.id(deck_name)
        return {
            "deck": deck_name,
            "deck_id": int(deck_id),
            "created": False,
            "would_create": False,
        }

    result = create_deck(deck_name)
    deck_id = mw.col.decks.id(deck_name)
    return {
        "deck": deck_name,
        "deck_id": int(deck_id),
        "created": bool(result.get("created")),
        "would_create": True,
    }


def create_deck(deck_name: str) -> dict:
    if not isinstance(deck_name, str) or not deck_name.strip():
        raise ValueError("deck_name_empty")

    deck_name = deck_name.strip()
    if deck_name in deck_names():
        return {
            "deck": deck_name,
            "created": False,
        }

    deck_id = mw.col.decks.id(deck_name)

    try:
        if hasattr(mw.col.decks, "get") and hasattr(mw.col.decks, "save"):
            deck = mw.col.decks.get(deck_id)
            if deck is not None:
                mw.col.decks.save(deck)
    except Exception as e:
        log(f"organization deck explicit save warning deck={deck_name} {type(e).__name__}: {e}")

    try:
        if hasattr(mw.col, "save"):
            mw.col.save()
    except Exception as e:
        log(f"organization deck save warning deck={deck_name} {type(e).__name__}: {e}")

    if deck_name not in deck_names():
        raise RuntimeError("deck_create_failed")

    return {
        "deck": deck_name,
        "created": True,
    }


def persist_note(note) -> None:
    if hasattr(mw.col, "update_note"):
        mw.col.update_note(note)
    elif hasattr(note, "flush"):
        note.flush()
    else:
        raise RuntimeError("no_supported_note_persist_method")


def persist_notes_batch(notes: list) -> None:
    if not notes:
        return
    update_notes = getattr(mw.col, "update_notes", None)
    if callable(update_notes):
        update_notes(notes)
        return
    for note in notes:
        persist_note(note)


def add_tags_to_notes(note_ids: list[int], tags: list[str]) -> dict:
    changed_note_ids = []
    if not tags:
        return {
            "tags": [],
            "changed_note_ids": [],
            "changed_count": 0,
        }

    for note_id in note_ids:
        note = get_note(note_id)
        changed = False
        for tag in tags:
            if tag in set(note.tags):
                continue
            if hasattr(note, "add_tag"):
                note.add_tag(tag)
            else:
                note.tags.append(tag)
            changed = True
        if changed:
            persist_note(note)
            changed_note_ids.append(note_id)

    return {
        "tags": tags,
        "changed_note_ids": changed_note_ids,
        "changed_count": len(changed_note_ids),
    }


def remove_tag_from_note(note, tag: str) -> bool:
    if tag not in set(note.tags):
        return False
    if hasattr(note, "del_tag"):
        note.del_tag(tag)
    elif hasattr(note, "remove_tag"):
        note.remove_tag(tag)
    else:
        note.tags = [current for current in note.tags if current != tag]
    return True


def replace_note_tags(note_ids: list[int], remove_tags=None, add_tags=None, dry_run: bool = True) -> dict:
    dry_run = normalize_bool(dry_run, True)
    note_ids = normalize_int_ids(note_ids, "note_ids")
    remove_tags = normalize_tags(remove_tags)
    add_tags = normalize_tags(add_tags)
    if not remove_tags and not add_tags:
        raise ValueError("replace_note_tags_noop")

    notes_result = []
    changed_note_ids = []
    log(
        "organization replace_note_tags start "
        f"note_ids={note_ids} remove_tags={remove_tags} add_tags={add_tags} dry_run={dry_run}"
    )

    for note_id in note_ids:
        note = get_note(note_id)
        before_tags = list(note.tags)
        after_tags = [tag for tag in before_tags if tag not in set(remove_tags)]
        for tag in add_tags:
            if tag not in set(after_tags):
                after_tags.append(tag)

        tags_to_remove = [tag for tag in before_tags if tag in set(remove_tags)]
        tags_to_add = [tag for tag in add_tags if tag not in set(before_tags)]
        changed = before_tags != after_tags

        if changed and not dry_run:
            for tag in tags_to_remove:
                remove_tag_from_note(note, tag)
            for tag in tags_to_add:
                if hasattr(note, "add_tag"):
                    note.add_tag(tag)
                elif tag not in note.tags:
                    note.tags.append(tag)
            persist_note(note)
            changed_note_ids.append(note_id)
        elif changed:
            changed_note_ids.append(note_id)

        card_ids = [
            int(card_id)
            for card_id in mw.col.db.list(
                """
                select id
                from cards
                where nid = ?
                order by ord, id
                """,
                int(note_id),
            )
        ]
        notes_result.append({
            "note_id": note_id,
            "card_ids": card_ids,
            "before_tags": before_tags,
            "remove_tags": tags_to_remove,
            "add_tags": tags_to_add,
            "after_tags": after_tags,
            "changed": changed,
        })

    if not dry_run:
        save_collection()

    result = {
        "operation": "replace_note_tags",
        "dry_run": dry_run,
        "note_ids": note_ids,
        "remove_tags": remove_tags,
        "add_tags": add_tags,
        "changed_note_ids": changed_note_ids,
        "changed_count": len(changed_note_ids),
        "notes": notes_result,
    }
    log(
        "organization replace_note_tags finished "
        f"note_ids={note_ids} changed_count={len(changed_note_ids)} dry_run={dry_run}"
    )
    return result


VISUAL_NORMALIZER_REMOVED_TAGS = {
    "font",
    "b",
    "strong",
    "u",
    "mark",
    "s",
    "strike",
    "del",
}
VISUAL_NORMALIZER_PRESERVED_SPAN_CLASSES = {"kw", "hint"}
UNSAFE_HTML_TAGS = {"script", "iframe", "object", "embed", "meta", "link", "base", "form"}
URL_HTML_ATTRIBUTES = {"href", "src", "xlink:href", "formaction"}


def validate_safe_html_tag(tag_name, attrs) -> None:
    if tag_name in UNSAFE_HTML_TAGS:
        raise ValueError(f"unsafe_html_tag: {tag_name}")
    for raw_name, raw_value in attrs:
        name = str(raw_name or "").strip().casefold()
        value = str(raw_value or "").strip().casefold()
        if name.startswith("on"):
            raise ValueError(f"unsafe_html_attribute: {name}")
        if name in URL_HTML_ATTRIBUTES and (
            value.startswith("javascript:")
            or value.startswith("vbscript:")
            or value.startswith("data:text/html")
        ):
            raise ValueError(f"unsafe_html_url: {name}")


def normalize_span_class(attrs):
    classes = []
    for name, value in attrs:
        if str(name).lower() == "class" and value:
            classes.extend(str(value).split())
    if "hint" in classes:
        return "hint"
    if "kw" in classes:
        return "kw"
    return None


def capitalize_first_alpha(text):
    chars = list(text)
    for index, char in enumerate(chars):
        if not char.isalpha():
            continue
        upper = char.upper()
        if char != upper:
            chars[index] = upper
            return "".join(chars), True, True
        return text, False, True
    return text, False, False


class VisualHtmlNormalizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.stack = []
        self.hint_needs_capitalization = []
        self.removed_visual_wrappers_count = 0
        self.normalized_hints_count = 0

    def handle_starttag(self, tag, attrs):
        tag_name = tag.lower()
        validate_safe_html_tag(tag_name, attrs)
        if tag_name == "span":
            span_class = normalize_span_class(attrs)
            if span_class in VISUAL_NORMALIZER_PRESERVED_SPAN_CLASSES:
                self.parts.append(f'<span class="{span_class}">')
                is_hint = span_class == "hint"
                self.stack.append({"tag": tag_name, "preserve": True, "hint": is_hint})
                if is_hint:
                    self.hint_needs_capitalization.append(True)
                return
            self.removed_visual_wrappers_count += 1
            self.stack.append({"tag": tag_name, "preserve": False, "hint": False})
            return

        if tag_name in VISUAL_NORMALIZER_REMOVED_TAGS:
            self.removed_visual_wrappers_count += 1
            self.stack.append({"tag": tag_name, "preserve": False, "hint": False})
            return

        self.parts.append(self._format_starttag(tag, attrs))
        self.stack.append({"tag": tag_name, "preserve": True, "hint": False})

    def handle_startendtag(self, tag, attrs):
        tag_name = tag.lower()
        validate_safe_html_tag(tag_name, attrs)
        if tag_name == "span":
            span_class = normalize_span_class(attrs)
            if span_class in VISUAL_NORMALIZER_PRESERVED_SPAN_CLASSES:
                self.parts.append(f'<span class="{span_class}" />')
            else:
                self.removed_visual_wrappers_count += 1
            return
        if tag_name in VISUAL_NORMALIZER_REMOVED_TAGS:
            self.removed_visual_wrappers_count += 1
            return
        self.parts.append(self._format_starttag(tag, attrs, close=True))

    def handle_endtag(self, tag):
        tag_name = tag.lower()
        item = self._pop_stack(tag_name)
        if not item:
            if tag_name not in VISUAL_NORMALIZER_REMOVED_TAGS and tag_name != "span":
                self.parts.append(f"</{tag}>")
            return
        if item.get("hint") and self.hint_needs_capitalization:
            self.hint_needs_capitalization.pop()
        if item.get("preserve"):
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self.hint_needs_capitalization:
            normalized, changed, consumed = capitalize_first_alpha(data)
            if consumed:
                self.hint_needs_capitalization[-1] = False
                if changed:
                    self.normalized_hints_count += 1
            self.parts.append(normalized)
            return
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.parts.append(f"<?{data}>")

    def unknown_decl(self, data):
        self.parts.append(f"<![{data}]>")

    def _pop_stack(self, tag_name):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].get("tag") == tag_name:
                return self.stack.pop(index)
        return None

    def _format_starttag(self, tag, attrs, close=False):
        rendered_attrs = []
        for name, value in attrs:
            if value is None:
                rendered_attrs.append(html.escape(str(name), quote=True))
            else:
                rendered_attrs.append(
                    f'{html.escape(str(name), quote=True)}="{html.escape(str(value), quote=True)}"'
                )
        attr_text = f" {' '.join(rendered_attrs)}" if rendered_attrs else ""
        suffix = " />" if close else ">"
        return f"<{tag}{attr_text}{suffix}"


def normalize_cloze_plain_hints(value):
    parts = []
    index = 0
    normalized_count = 0
    while True:
        start = value.find("{{c", index)
        if start < 0:
            parts.append(value[index:])
            break
        end = value.find("}}", start + 3)
        if end < 0:
            parts.append(value[index:])
            break

        parts.append(value[index:start])
        cloze = value[start + 2:end]
        fields = cloze.split("::", 2)
        valid = (
            len(fields) == 3
            and len(fields[0]) > 1
            and fields[0][0] == "c"
            and fields[0][1:].isdigit()
            and fields[2].strip()
        )
        if not valid:
            parts.append(value[start:end + 2])
            index = end + 2
            continue

        hint = fields[2]
        if hint.strip().startswith('<span class="hint">'):
            parts.append(value[start:end + 2])
            index = end + 2
            continue

        normalized_hint, _, _ = capitalize_first_alpha(hint)
        parts.append(f"{{{{{fields[0]}::{fields[1]}::<span class=\"hint\">{normalized_hint}</span>}}}}")
        normalized_count += 1
        index = end + 2

    return "".join(parts), normalized_count


def normalize_visual_html(value):
    parser = VisualHtmlNormalizer()
    parser.feed(value)
    parser.close()
    normalized = "".join(parser.parts)
    normalized, cloze_hint_count = normalize_cloze_plain_hints(normalized)
    stats = {
        "normalized": normalized != value,
        "removed_visual_wrappers_count": parser.removed_visual_wrappers_count,
        "normalized_hints_count": parser.normalized_hints_count + cloze_hint_count,
    }
    return normalized, stats


def normalize_note_field_updates(note_updates) -> list[dict]:
    if not isinstance(note_updates, list) or not note_updates:
        raise ValueError("invalid_note_updates")
    if len(note_updates) > 50:
        raise ValueError("too_many_note_updates: max=50")

    normalized = []
    seen = set()
    for index, raw_update in enumerate(note_updates):
        if not isinstance(raw_update, dict):
            raise ValueError(f"invalid_note_update: index={index}")
        note_id = raw_update.get("note_id")
        if isinstance(note_id, bool) or not isinstance(note_id, int):
            raise ValueError(f"invalid_note_id: index={index}")
        if note_id in seen:
            raise ValueError(f"duplicate_note_id: {note_id}")
        seen.add(note_id)

        raw_fields = raw_update.get("fields")
        if not isinstance(raw_fields, dict) or not raw_fields:
            raise ValueError(f"invalid_fields: note_id={note_id}")
        fields = {}
        field_normalization = {}
        raw_field_normalization = raw_update.get("field_normalization", {})
        if not isinstance(raw_field_normalization, dict):
            raw_field_normalization = {}
        for field_name, value in raw_fields.items():
            if not isinstance(field_name, str) or not field_name.strip():
                raise ValueError(f"invalid_field_name: note_id={note_id}")
            if not isinstance(value, str):
                raise ValueError(f"invalid_field_value: note_id={note_id} field={field_name}")
            normalized_value, local_stats = normalize_visual_html(value)
            normalized_field_name = field_name.strip()
            remote_stats = raw_field_normalization.get(normalized_field_name, {})
            if not isinstance(remote_stats, dict):
                remote_stats = {}
            combined_stats = {
                "normalized": bool(remote_stats.get("normalized")) or local_stats["normalized"],
                "removed_visual_wrappers_count": int(remote_stats.get("removed_visual_wrappers_count", 0) or 0)
                + local_stats["removed_visual_wrappers_count"],
                "normalized_hints_count": int(remote_stats.get("normalized_hints_count", 0) or 0)
                + local_stats["normalized_hints_count"],
            }
            fields[normalized_field_name] = normalized_value
            field_normalization[normalized_field_name] = combined_stats

        expected_content_hash = raw_update.get("expected_content_hash")
        if expected_content_hash is not None and (
            not isinstance(expected_content_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_content_hash)
        ):
            raise ValueError(f"invalid_expected_content_hash: note_id={note_id}")
        for key in ("expected_mod", "expected_usn", "expected_model_id"):
            value = raw_update.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"invalid_{key}: note_id={note_id}")

        normalized.append({
            "note_id": note_id,
            "fields": fields,
            "field_normalization": field_normalization,
            **{
                key: raw_update[key]
                for key in (
                    "expected_content_hash",
                    "expected_mod",
                    "expected_usn",
                    "expected_model_id",
                )
                if key in raw_update
            },
        })
    return normalized


def note_field_names(note) -> list[str]:
    model = mw.col.models.get(note.mid)
    if not model:
        raise ValueError(f"note_model_not_found: {int(note.id)}")
    return model_field_names(model)


def get_note_field_value(note, field_name: str, field_names: list[str]) -> str:
    try:
        return str(note[field_name])
    except Exception:
        try:
            return str(note.fields[field_names.index(field_name)])
        except Exception:
            return ""


def set_note_field_value(note, field_name: str, value: str, field_names: list[str]) -> None:
    try:
        note[field_name] = value
    except Exception:
        note.fields[field_names.index(field_name)] = value


def note_version_value(note, name: str):
    value = getattr(note, name, None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def note_content_hash(note, field_names: list[str]) -> str:
    return canonical_note_content_hash(
        model_id=note_version_value(note, "mid"),
        field_names=field_names,
        fields={
            field_name: get_note_field_value(note, field_name, field_names)
            for field_name in field_names
        },
        tags=getattr(note, "tags", []) or [],
    )


def note_precondition(note, field_names: list[str]) -> dict:
    return build_canonical_note_precondition(
        model_id=note_version_value(note, "mid"),
        mod=note_version_value(note, "mod"),
        usn=note_version_value(note, "usn"),
        field_names=field_names,
        fields={
            field_name: get_note_field_value(note, field_name, field_names)
            for field_name in field_names
        },
        tags=getattr(note, "tags", []) or [],
    )


def validate_note_precondition(update: dict, current: dict, require: bool) -> None:
    expected_hash = update.get("expected_content_hash")
    if require and not expected_hash:
        raise ValueError(
            "missing_note_precondition: expected top-level key "
            "expected_content_hash (optional companions: expected_mod, "
            "expected_usn, expected_model_id)"
        )
    if expected_hash and expected_hash != current["expected_content_hash"]:
        raise ValueError("note_content_conflict")
    for key, error_code in (
        ("expected_mod", "note_mod_conflict"),
        ("expected_usn", "note_usn_conflict"),
        ("expected_model_id", "note_model_conflict"),
    ):
        expected = update.get(key)
        if expected is not None and expected != current.get(key):
            raise ValueError(error_code)


def summarize_field_change(before: str, after: str) -> dict:
    return {
        "changed": before != after,
        "before_length": len(before),
        "after_length": len(after),
        "before_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
        "after_sha256": hashlib.sha256(after.encode("utf-8")).hexdigest(),
    }


def count_class_spans(value: str, class_name: str) -> int:
    pattern = r"<span\s+class=[\"']" + re.escape(class_name) + r"[\"']\s*>"
    return len(re.findall(pattern, value or "", flags=re.IGNORECASE))


def update_fields_should_warn_missing_kw(requested_by: str = "", reason: str = "", fields=None) -> bool:
    marker_text = f"{requested_by or ''} {reason or ''}".casefold()
    if "grif" in marker_text or "normaliz" in marker_text or "normaliza" in marker_text:
        return True
    fields = fields or {}
    for value in fields.values():
        if '<span class="kw">' in value or '<span class="hint">' in value:
            return True
    return False


def begin_custom_undo_entry(label: str):
    add_entry = getattr(mw.col, "add_custom_undo_entry", None)
    if not callable(add_entry):
        return None
    return add_entry(label)


def finish_custom_undo_entry(entry) -> bool:
    if entry is None:
        return False
    merge_entries = getattr(mw.col, "merge_undo_entries", None)
    if not callable(merge_entries):
        return False
    merge_entries(entry)
    return True


def update_note_fields(
    note_updates: list[dict],
    dry_run: bool = True,
    requested_by: str = "",
    reason: str = "",
    require_preconditions: bool = False,
) -> dict:
    dry_run = normalize_bool(dry_run, True)
    updates = normalize_note_field_updates(note_updates)
    notes_result = []
    errors = []
    prepared = []

    log(
        "organization update_note_fields start "
        f"requested_count={len(updates)} dry_run={dry_run}"
    )

    # Phase 1: resolve and validate every note before the first mutation.
    for update in updates:
        note_id = update["note_id"]
        try:
            note = get_note(note_id)
            field_names = note_field_names(note)
            unknown_fields = sorted(set(update["fields"]) - set(field_names))
            if unknown_fields:
                raise ValueError(f"unknown_fields: {unknown_fields}")
            precondition = note_precondition(note, field_names)
            validate_note_precondition(
                update,
                precondition,
                require=bool(require_preconditions and not dry_run),
            )

            field_results = {}
            changed_fields = []
            original_fields = {}
            for field_name, after in update["fields"].items():
                before = get_note_field_value(note, field_name, field_names)
                original_fields[field_name] = before
                field_results[field_name] = summarize_field_change(before, after)
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
                count_class_spans(value, "kw")
                for value in update["fields"].values()
            )
            hint_count_after = sum(
                count_class_spans(value, "hint")
                for value in update["fields"].values()
            )
            has_kw_after = kw_count_after > 0
            warnings = []
            if (
                not has_kw_after
                and update_fields_should_warn_missing_kw(
                    requested_by=requested_by,
                    reason=reason,
                    fields=update["fields"],
                )
            ):
                warnings.append("missing_kw_after_update")

            card_ids = [
                int(card_id)
                for card_id in mw.col.db.list(
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
                "has_kw_after": has_kw_after,
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
                "original_fields": original_fields,
                "changed_fields": changed_fields,
                "precondition": precondition,
            })
        except Exception as e:
            error = {
                "note_id": note_id,
                "error": f"{type(e).__name__}: {e}",
            }
            errors.append(error)
            notes_result.append({
                "note_id": note_id,
                "changed": False,
                "changed_fields": [],
                "error": error["error"],
            })
            log(
                "organization update_note_fields note failed "
                f"note_id={note_id} dry_run={dry_run} error={error['error']}"
            )

    planned_note_ids = [item["note_id"] for item in prepared if item["changed_fields"]]
    applied_note_ids = []
    rolled_back = False
    rollback_errors = []
    undo_label = "Anki GPT: atualizar campos de notes"
    undo_entry = None
    undo_available = False

    if not errors and not dry_run:
        try:
            if planned_note_ids:
                undo_entry = begin_custom_undo_entry(undo_label)
            changed_items = []
            for item in prepared:
                if not item["changed_fields"]:
                    continue
                for field_name, after in item["fields"].items():
                    set_note_field_value(item["note"], field_name, after, item["field_names"])
                changed_items.append(item)
            persist_notes_batch([item["note"] for item in changed_items])
            applied_note_ids.extend(item["note_id"] for item in changed_items)
            save_collection(strict=True)
            if applied_note_ids:
                undo_available = finish_custom_undo_entry(undo_entry)
        except Exception as apply_error:
            rollback_notes = []
            for item in prepared:
                if not item["changed_fields"]:
                    continue
                try:
                    for field_name, before in item["original_fields"].items():
                        set_note_field_value(item["note"], field_name, before, item["field_names"])
                    rollback_notes.append(item["note"])
                except Exception as rollback_error:
                    rollback_errors.append({
                        "note_id": item["note_id"],
                        "error": f"{type(rollback_error).__name__}: {rollback_error}",
                    })
            if rollback_notes:
                try:
                    persist_notes_batch(rollback_notes)
                except Exception as rollback_error:
                    rollback_errors.append({
                        "note_id": None,
                        "error": f"{type(rollback_error).__name__}: {rollback_error}",
                    })
            try:
                save_collection(strict=True)
            except Exception as rollback_commit_error:
                rollback_errors.append({
                    "note_id": None,
                    "error": f"{type(rollback_commit_error).__name__}: {rollback_commit_error}",
                })
            rolled_back = not rollback_errors
            errors.append({
                "note_id": None,
                "error": f"batch_apply_failed: {type(apply_error).__name__}: {apply_error}",
            })
            if rolled_back:
                applied_note_ids = []

    apply_preconditions = [
        {"note_id": item["note_id"], **item["precondition"]}
        for item in prepared
    ]
    result = {
        "operation": "update_note_fields",
        "dry_run": dry_run,
        "requested_count": len(updates),
        "changed_count": len(applied_note_ids) if not dry_run else len(planned_note_ids),
        "planned_note_ids": planned_note_ids,
        "affected_note_ids": applied_note_ids if not dry_run else planned_note_ids,
        "notes": notes_result,
        "errors": errors,
        "atomic": True,
        "rolled_back": rolled_back,
        "rollback_errors": rollback_errors,
        "undo_available": undo_available,
        "undo_label": undo_label if undo_available else None,
        "undo_entry": undo_entry if undo_available else None,
        "preconditions_required": bool(require_preconditions and not dry_run),
        # Public, stable apply-v2 contract.  The backend persists this key and
        # can materialize a conditioned updates_id from the dry-run operation.
        "apply_preconditions": apply_preconditions,
        # Backward-compatible alias for local callers and older clients.
        "preconditions": apply_preconditions,
        "warnings": [
            {
                "note_id": item["note_id"],
                "warning": warning,
            }
            for item in notes_result
            for warning in item.get("warnings", [])
        ],
    }
    log(
        "organization update_note_fields finished "
        f"requested_count={len(updates)} changed_count={result['changed_count']} "
        f"errors={len(errors)} dry_run={dry_run}"
    )
    return result


def replace_ideal_destination_tags(note_ids: list[int], deck_name: str, dry_run: bool = False) -> dict:
    note_ids = normalize_int_ids(note_ids, "note_ids")
    deck_name = (deck_name or "").strip()
    destination_tag = ideal_destination_tag(deck_name)
    desired_tags = [IDEAL_DECK_TAG, destination_tag]

    changed_note_ids = []
    removals_by_note = {}
    additions_by_note = {}

    for note_id in note_ids:
        note = get_note(note_id)
        existing_tags = list(note.tags)
        tags_to_remove = [
            tag for tag in destination_tags(existing_tags)
            if tag != destination_tag
        ]
        tags_to_add = [
            tag for tag in desired_tags
            if tag not in set(existing_tags)
        ]

        if tags_to_remove:
            removals_by_note[str(note_id)] = tags_to_remove
        if tags_to_add:
            additions_by_note[str(note_id)] = tags_to_add

        if dry_run or (not tags_to_remove and not tags_to_add):
            continue

        changed = False
        for tag in tags_to_remove:
            changed = remove_tag_from_note(note, tag) or changed
        for tag in tags_to_add:
            if hasattr(note, "add_tag"):
                note.add_tag(tag)
            else:
                note.tags.append(tag)
            changed = True

        if changed:
            persist_note(note)
            changed_note_ids.append(note_id)

    destination_tags_to_remove = []
    for tags in removals_by_note.values():
        for tag in tags:
            if tag not in destination_tags_to_remove:
                destination_tags_to_remove.append(tag)

    tags_to_add_summary = []
    for tags in additions_by_note.values():
        for tag in tags:
            if tag not in tags_to_add_summary:
                tags_to_add_summary.append(tag)

    return {
        "tags": desired_tags,
        "destination_tag_to_add": destination_tag,
        "destination_tags_to_remove": destination_tags_to_remove,
        "removals_by_note": removals_by_note,
        "additions_by_note": additions_by_note,
        "tags_to_add": tags_to_add_summary,
        "changed_note_ids": changed_note_ids,
        "changed_count": len(changed_note_ids),
        "dry_run": dry_run,
    }


def mark_notes_as_ideal_deck(note_ids: list[int], deck_name: str) -> dict:
    note_ids = normalize_int_ids(note_ids, "note_ids")
    deck_name = (deck_name or "").strip()
    tag_result = replace_ideal_destination_tags(note_ids, deck_name, dry_run=False)
    save_collection()
    return {
        "operation": "mark_notes_as_ideal_deck",
        "note_ids": note_ids,
        "deck_name": deck_name,
        "deck_slug": deck_name_to_tag_slug(deck_name),
        "tags": tag_result["tags"],
        "destination_tag_to_add": tag_result["destination_tag_to_add"],
        "destination_tags_to_remove": tag_result["destination_tags_to_remove"],
        "removals_by_note": tag_result["removals_by_note"],
        "additions_by_note": tag_result["additions_by_note"],
        "changed_note_ids": tag_result["changed_note_ids"],
        "changed_count": tag_result["changed_count"],
        "preserved": {
            "card_id": True,
            "note_id": True,
            "scheduling": True,
            "review_history": True,
            "fields": True,
            "media": True,
        },
    }


def mark_cards_as_ideal_deck(card_ids: list[int], deck_name: str) -> dict:
    card_ids = normalize_int_ids(card_ids, "card_ids")
    cards = [get_card(card_id) for card_id in card_ids]
    note_ids = sorted({int(card_attr(card, "nid")) for card in cards})
    result = mark_notes_as_ideal_deck(note_ids, deck_name)
    result["operation"] = "mark_cards_as_ideal_deck"
    result["card_ids"] = card_ids
    return result


def note_ideal_status(note, current_decks: list[str], note_id=None) -> dict:
    tags = list(note.tags)
    dest_tags = destination_tags(tags)
    expected_slugs = [
        tag[len(f"{DESTINATION_TAG_PREFIX}::"):]
        for tag in dest_tags
    ]
    current_deck_slugs = []
    for deck in current_decks:
        if deck:
            try:
                current_deck_slugs.append(deck_name_to_tag_slug(deck))
            except ValueError:
                pass

    if expected_slugs and current_deck_slugs:
        seems_match = any(slug in current_deck_slugs for slug in expected_slugs)
    else:
        seems_match = None

    return {
        "note_id": int(note_id if note_id is not None else getattr(note, "id", 0)),
        "has_ideal_deck_tag": IDEAL_DECK_TAG in set(tags),
        "destination_tags": dest_tags,
        "expected_deck_slug": expected_slugs[-1] if expected_slugs else None,
        "parece_bater_com_deck_atual": seems_match,
        "relevant_tags": relevant_ideal_tags(tags),
    }


def check_ideal_deck_status(card_ids=None, note_ids=None) -> dict:
    card_ids = [] if card_ids is None else card_ids
    note_ids = [] if note_ids is None else note_ids
    if not isinstance(card_ids, list) or not isinstance(note_ids, list):
        raise ValueError("invalid_ideal_status_ids")
    if not card_ids and not note_ids:
        raise ValueError("ideal_status_ids_empty")

    normalized_card_ids = []
    if card_ids:
        normalized_card_ids = normalize_int_ids(card_ids, "card_ids")
    normalized_note_ids = []
    if note_ids:
        normalized_note_ids = normalize_int_ids(note_ids, "note_ids")

    deck_name_map = get_deck_name_map()
    card_items = []
    note_ids_from_cards = set()
    for card_id in normalized_card_ids:
        card = get_card(card_id)
        note = get_note(int(card_attr(card, "nid")))
        current_deck = deck_name_map.get(int(card_attr(card, "did")), "")
        status = note_ideal_status(note, [current_deck], note_id=int(card_attr(card, "nid")))
        status.update({
            "card_id": int(card_attr(card, "id")),
            "current_deck": current_deck,
            "current_deck_id": int(card_attr(card, "did")),
        })
        card_items.append(status)
        note_ids_from_cards.add(int(card_attr(card, "nid")))

    note_items = []
    for note_id in normalized_note_ids:
        note = get_note(note_id)
        cards = note_cards(note_id)
        current_decks = [
            deck_name_map.get(int(card_attr(card, "did")), "")
            for card in cards
        ]
        status = note_ideal_status(note, current_decks, note_id=note_id)
        status.update({
            "current_decks": sorted(set(current_decks)),
            "card_ids": [int(card_attr(card, "id")) for card in cards],
        })
        note_items.append(status)

    return {
        "operation": "check_ideal_deck_status",
        "card_ids": normalized_card_ids,
        "note_ids": normalized_note_ids,
        "card_statuses": card_items,
        "note_statuses": note_items,
        "resolved_note_ids": sorted(set(normalized_note_ids) | note_ids_from_cards),
        "ideal_tag": IDEAL_DECK_TAG,
        "destination_tag_prefix": DESTINATION_TAG_PREFIX,
    }


def list_note_types() -> dict:
    note_types = []
    for item in mw.col.models.all_names_and_ids():
        model = mw.col.models.get(item.id)
        fields = model_field_names(model) if model else []
        templates = model_template_names(model) if model else []
        note_types.append({
            "id": int(item.id),
            "name": item.name,
            "supported_for_creation": item.name in SUPPORTED_CREATE_NOTE_TYPES,
            "fields": fields,
            "templates": templates,
            "template_count": len(templates),
        })
    note_types.sort(key=lambda item: item["name"])
    return {
        "operation": "list_note_types",
        "count": len(note_types),
        "supported_note_types": sorted(SUPPORTED_CREATE_NOTE_TYPES),
        "note_types": note_types,
    }


def get_note_type_fields(model_name: str) -> dict:
    model = get_model(model_name)
    fields = [
        {
            "ord": int(field.get("ord", index)),
            "name": str(field.get("name", "")),
            "sticky": bool(field.get("sticky", False)),
            "rtl": bool(field.get("rtl", False)),
        }
        for index, field in enumerate(model.get("flds", []))
    ]
    templates = [
        {
            "ord": int(template.get("ord", index)),
            "name": str(template.get("name", "")),
        }
        for index, template in enumerate(model.get("tmpls", []))
    ]
    return {
        "operation": "get_note_type_fields",
        "model_id": int(model.get("id", 0)),
        "model_name": str(model.get("name", model_name)),
        "supported_for_creation": str(model.get("name", model_name)) in SUPPORTED_CREATE_NOTE_TYPES,
        "fields": fields,
        "field_names": [field["name"] for field in fields],
        "templates": templates,
        "template_names": [template["name"] for template in templates],
        "is_cloze": "cloze" in str(model.get("name", model_name)).casefold() or int(model.get("type", 0)) == 1,
    }


def normalize_create_tags(raw_tags) -> list[str]:
    if raw_tags is None:
        return list(DEFAULT_CREATE_NOTE_TAGS)
    if not isinstance(raw_tags, list):
        raise ValueError("invalid_tags")
    tags = []
    for raw_tag in raw_tags:
        if not isinstance(raw_tag, str) or not raw_tag.strip():
            raise ValueError("invalid_tag")
        tag = raw_tag.strip()
        if tag not in tags:
            tags.append(tag)
    return tags


def normalize_create_fields(raw_fields) -> dict:
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise ValueError("invalid_fields")
    fields = {}
    for key, value in raw_fields.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("invalid_field_name")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"invalid_field_value: {key}")
        fields[key.strip()] = value
    return fields


def build_ordered_field_values(model: dict, fields: dict) -> list[str]:
    field_names = model_field_names(model)
    unknown = sorted(set(fields) - set(field_names))
    if unknown:
        raise ValueError(f"unknown_fields: {unknown}")
    if not field_names:
        raise ValueError("model_has_no_fields")

    values = [str(fields.get(name, "")) for name in field_names]
    if not comparable_text(values[0]):
        raise ValueError(f"first_field_empty: {field_names[0]}")
    return values


def validate_cloze_fields(model: dict, fields: dict) -> dict:
    model_name = str(model.get("name", ""))
    is_cloze = "cloze" in model_name.casefold() or int(model.get("type", 0)) == 1
    all_text = "\n".join(str(value) for value in fields.values())
    broken = detect_broken_cloze_syntax(all_text)
    if broken:
        raise ValueError("; ".join(broken))

    cloze_numbers = find_cloze_numbers_in_text(all_text)
    if is_cloze and not cloze_numbers:
        raise ValueError("cloze_note_requires_at_least_one_cloze_deletion")
    return {
        "is_cloze": is_cloze,
        "cloze_numbers": cloze_numbers,
    }


def duplicate_candidates(model: dict, ordered_values: list[str]) -> list[dict]:
    target_all = normalize_note_fields_for_compare(ordered_values)
    target_first = comparable_text(ordered_values[0] if ordered_values else "")
    candidates = []
    note_rows = mw.col.db.all(
        """
        select id, flds
        from notes
        where mid = ?
        """,
        int(model.get("id")),
    )
    for note_id, raw_fields in note_rows:
        existing_values = str(raw_fields or "").split("\x1f")
        existing_all = normalize_note_fields_for_compare(existing_values)
        existing_first = comparable_text(existing_values[0] if existing_values else "")
        if existing_all == target_all or (target_first and existing_first == target_first):
            card_ids = mw.col.db.list(
                """
                select id
                from cards
                where nid = ?
                order by ord, id
                """,
                int(note_id),
            )
            candidates.append({
                "note_id": int(note_id),
                "card_ids": [int(card_id) for card_id in card_ids],
                "match": "all_fields" if existing_all == target_all else "first_field",
            })
    return candidates


def apply_fields_to_note(note, model: dict, fields: dict) -> None:
    field_names = model_field_names(model)
    for name in field_names:
        value = str(fields.get(name, ""))
        try:
            note[name] = value
        except Exception:
            note.fields[field_names.index(name)] = value


def apply_tags_to_new_note(note, tags: list[str]) -> None:
    for tag in tags:
        if hasattr(note, "add_tag"):
            note.add_tag(tag)
        elif tag not in note.tags:
            note.tags.append(tag)


def validate_create_note_payload(deck_name: str, model_name: str, fields: dict, tags=None, allow_duplicate=False) -> dict:
    if not isinstance(deck_name, str) or not deck_name.strip():
        raise ValueError("deck_name_empty")
    deck_name = deck_name.strip()
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError("model_name_empty")
    model_name = model_name.strip()
    if model_name not in SUPPORTED_CREATE_NOTE_TYPES:
        raise ValueError(f"unsupported_note_type_for_creation: {model_name}")
    if not isinstance(allow_duplicate, bool):
        raise ValueError("invalid_allow_duplicate")

    model = get_model(model_name)
    normalized_fields = normalize_create_fields(fields)
    ordered_values = build_ordered_field_values(model, normalized_fields)
    cloze_result = validate_cloze_fields(model, normalized_fields)
    normalized_tags = normalize_create_tags(tags)
    duplicates = duplicate_candidates(model, ordered_values)
    warnings = []
    if duplicates:
        warnings.append("possible_duplicate_note")

    return {
        "deck_name": deck_name,
        "model": model,
        "model_id": int(model.get("id")),
        "model_name": model_name,
        "field_names": model_field_names(model),
        "fields": normalized_fields,
        "ordered_values": ordered_values,
        "tags": normalized_tags,
        "allow_duplicate": allow_duplicate,
        "duplicate": bool(duplicates),
        "duplicate_candidates": duplicates,
        "warnings": warnings,
        "cloze_numbers": cloze_result["cloze_numbers"],
        "is_cloze": cloze_result["is_cloze"],
    }


def create_note(deck_name: str, model_name: str, fields: dict, tags=None, allow_duplicate: bool = False, dry_run: bool = True) -> dict:
    dry_run = normalize_bool(dry_run, True)
    validation = validate_create_note_payload(deck_name, model_name, fields, tags, allow_duplicate)
    deck_result = ensure_deck(validation["deck_name"], dry_run=True)
    result = {
        "operation": "create_note",
        "dry_run": dry_run,
        "created": False,
        "deck_name": validation["deck_name"],
        "deck_id": None,
        "would_create_deck": deck_result.get("would_create", False),
        "model_name": validation["model_name"],
        "model_id": validation["model_id"],
        "field_names": validation["field_names"],
        "fields": validation["fields"],
        "tags": validation["tags"],
        "allow_duplicate": validation["allow_duplicate"],
        "duplicate": validation["duplicate"],
        "duplicate_candidates": validation["duplicate_candidates"],
        "warnings": validation["warnings"],
        "is_cloze": validation["is_cloze"],
        "cloze_numbers": validation["cloze_numbers"],
        "note_id": None,
        "card_ids": [],
    }

    if dry_run:
        return result

    if validation["duplicate"] and not validation["allow_duplicate"]:
        raise OperationFailedWithResult("duplicate_note", result)

    deck_result = ensure_deck(validation["deck_name"], dry_run=False)
    note = mw.col.new_note(validation["model"])
    apply_fields_to_note(note, validation["model"], validation["fields"])
    apply_tags_to_new_note(note, validation["tags"])
    mw.col.add_note(note, int(deck_result["deck_id"]))
    save_collection()

    note_id = int(note.id)
    card_ids = [
        int(card_id)
        for card_id in mw.col.db.list(
            """
            select id
            from cards
            where nid = ?
            order by ord, id
            """,
            note_id,
        )
    ]
    result.update({
        "created": True,
        "deck_id": int(deck_result["deck_id"]),
        "created_deck": bool(deck_result.get("created", False)),
        "note_id": note_id,
        "card_ids": card_ids,
        "generated_card_count": len(card_ids),
    })
    return result


def preview_create_notes(notes: list[dict]) -> dict:
    return create_notes(notes, dry_run=True)


def create_notes(notes: list[dict], dry_run: bool = True) -> dict:
    dry_run = normalize_bool(dry_run, True)
    if not isinstance(notes, list) or not notes:
        raise ValueError("invalid_notes")
    if len(notes) > MAX_CREATE_NOTES_PER_OPERATION:
        raise ValueError(f"too_many_notes: max={MAX_CREATE_NOTES_PER_OPERATION}")

    if not dry_run:
        preflight_results = []
        preflight_errors = []
        for index, note_payload in enumerate(notes):
            if not isinstance(note_payload, dict):
                preflight_errors.append({"index": index, "error": "invalid_note_payload"})
                continue
            try:
                preview = create_note(
                    deck_name=note_payload.get("deck_name"),
                    model_name=note_payload.get("model_name"),
                    fields=note_payload.get("fields"),
                    tags=note_payload.get("tags"),
                    allow_duplicate=note_payload.get("allow_duplicate", False),
                    dry_run=True,
                )
                preview["index"] = index
                preflight_results.append(preview)
                if preview.get("duplicate") and not preview.get("allow_duplicate"):
                    preflight_errors.append({"index": index, "error": "duplicate_note"})
            except Exception as e:
                preflight_errors.append({"index": index, "error": f"{type(e).__name__}: {e}"})
        if preflight_errors:
            raise OperationFailedWithResult("create_notes_preflight_failed", {
                "operation": "create_notes",
                "dry_run": False,
                "created_count": 0,
                "notes": preflight_results,
                "errors": preflight_errors,
            })

    results = []
    errors = []
    created_note_ids = []
    created_card_ids = []
    for index, note_payload in enumerate(notes):
        if not isinstance(note_payload, dict):
            errors.append({"index": index, "error": "invalid_note_payload"})
            continue
        try:
            result = create_note(
                deck_name=note_payload.get("deck_name"),
                model_name=note_payload.get("model_name"),
                fields=note_payload.get("fields"),
                tags=note_payload.get("tags"),
                allow_duplicate=note_payload.get("allow_duplicate", False),
                dry_run=dry_run,
            )
            result["index"] = index
            results.append(result)
            if result.get("created"):
                created_note_ids.append(result["note_id"])
                created_card_ids.extend(result["card_ids"])
        except OperationFailedWithResult as e:
            e.result["index"] = index
            results.append(e.result)
            errors.append({"index": index, "error": str(e)})
            if not dry_run:
                raise
        except Exception as e:
            errors.append({"index": index, "error": f"{type(e).__name__}: {e}"})
            if not dry_run:
                raise

    if dry_run and errors:
        raise OperationFailedWithResult("create_notes_validation_failed", {
            "operation": "create_notes",
            "dry_run": dry_run,
            "created_count": 0,
            "notes": results,
            "errors": errors,
        })

    return {
        "operation": "create_notes",
        "dry_run": dry_run,
        "requested_count": len(notes),
        "created_count": len(created_note_ids),
        "created_note_ids": created_note_ids,
        "created_card_ids": created_card_ids,
        "notes": results,
        "errors": errors,
    }


def update_cards_deck(cards: list, target_deck_id: int) -> None:
    card_ids = [int(card_attr(card, "id")) for card in cards]

    if hasattr(mw.col, "set_deck"):
        mw.col.set_deck(card_ids, target_deck_id)
        return

    for card in cards:
        setattr(card, "did", target_deck_id)

    if hasattr(mw.col, "update_cards"):
        mw.col.update_cards(cards)
        return

    if hasattr(mw.col, "update_card"):
        for card in cards:
            mw.col.update_card(card)
        return

    for card_id in card_ids:
        mw.col.db.execute(
            """
            update cards
            set did = ?
            where id = ?
            """,
            target_deck_id,
            card_id,
        )


def save_collection(strict: bool = False) -> None:
    try:
        if hasattr(mw.col, "save"):
            mw.col.save()
    except Exception as e:
        log(f"organization collection save warning {type(e).__name__}: {e}")
        if strict:
            raise


def append_reorganization_log(entry: dict) -> None:
    REORGANIZATION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with REORGANIZATION_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def load_reorganization_log_entries() -> list[dict]:
    if not REORGANIZATION_LOG_FILE.exists():
        return []

    entries = []
    with REORGANIZATION_LOG_FILE.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if isinstance(entry, dict):
                    entries.append(entry)
            except Exception as e:
                log(
                    "organization move log parse warning "
                    f"path={REORGANIZATION_LOG_FILE} line={line_number} "
                    f"{type(e).__name__}: {e}"
                )
    return entries


def reorganization_batches(entries=None) -> list[dict]:
    entries = load_reorganization_log_entries() if entries is None else entries
    batches_by_id = {}
    undone_batch_ids = set()
    undo_entries_by_batch = {}

    for entry in entries:
        if entry.get("event_type") in {"undo", "undo_reorder_created"}:
            undone_batch_id = entry.get("undone_batch_id")
            if undone_batch_id:
                undone_batch_ids.add(undone_batch_id)
                undo_entries_by_batch.setdefault(undone_batch_id, []).append(entry)
            continue

        if entry.get("event_type") not in {"move", "reorder_created"}:
            continue

        batch_id = entry.get("batch_id")
        if not batch_id:
            continue

        batch = batches_by_id.setdefault(
            batch_id,
            {
                "batch_id": batch_id,
                "operation_id": entry.get("operation_id", ""),
                "operation_type": entry.get("operation_type", ""),
                "timestamp": entry.get("timestamp", ""),
                "card_count": 0,
                "moved_count": 0,
                "failed_count": 0,
                "card_ids": [],
                "note_ids": [],
                "old_decks": [],
                "new_decks": [],
                "tags_added": [],
                "old_cids": [],
                "new_cids": [],
                "reordered_count": 0,
                "entries": [],
            },
        )

        if entry.get("timestamp", "") < batch.get("timestamp", ""):
            batch["timestamp"] = entry.get("timestamp", "")

        batch["entries"].append(entry)
        batch["card_count"] += 1
        if entry.get("status") == "moved":
            batch["moved_count"] += 1
        elif entry.get("status") == "failed":
            batch["failed_count"] += 1
        elif entry.get("status") == "reordered":
            batch["reordered_count"] += 1

        for key in ("card_ids", "note_ids", "old_decks", "new_decks", "tags_added", "old_cids", "new_cids"):
            value_key = {
                "card_ids": "card_id",
                "note_ids": "note_id",
                "old_decks": "old_deck",
                "new_decks": "new_deck",
                "tags_added": "tags_added",
                "old_cids": "old_cid",
                "new_cids": "new_cid",
            }[key]
            if key == "card_ids" and entry.get("event_type") == "reorder_created":
                value_key = "new_cid"
            if key == "note_ids" and entry.get("event_type") == "reorder_created":
                value_key = "old_nid"
            value = entry.get(value_key)
            values = value if isinstance(value, list) else [value]
            for item in values:
                if item is not None and item not in batch[key]:
                    batch[key].append(item)

    batches = list(batches_by_id.values())
    for batch in batches:
        batch["undone"] = batch["batch_id"] in undone_batch_ids
        batch["undo_entries"] = undo_entries_by_batch.get(batch["batch_id"], [])

    batches.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
    return batches


def get_reorganization_log(limit: int = 20) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("invalid_limit")
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    batches = reorganization_batches()
    return {
        "log_path": str(REORGANIZATION_LOG_FILE),
        "limit": limit,
        "count": len(batches),
        "returned": min(limit, len(batches)),
        "batches": batches[:limit],
        "format": "jsonl_per_card_move_and_undo",
    }


def move_log_entry(
    *,
    batch_id: str,
    operation_id: str,
    operation_type: str,
    card_before,
    card_after,
    old_deck: str,
    old_deck_id: int,
    new_deck: str,
    new_deck_id: int,
    template_name: str,
    note_type: str,
    tags_added: list[str],
    scheduling_before: dict,
    status: str = "moved",
    error: str = "",
) -> dict:
    return {
        "event_type": "move",
        "status": status,
        "batch_id": batch_id,
        "operation_id": operation_id,
        "operation_type": operation_type,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "card_id": int(card_attr(card_before, "id")),
        "note_id": int(card_attr(card_before, "nid")),
        "old_deck": old_deck,
        "old_deck_id": int(old_deck_id),
        "new_deck": new_deck,
        "new_deck_id": int(new_deck_id),
        "ord": int(card_attr(card_before, "ord")),
        "template_name": template_name,
        "note_type": note_type,
        "tags_added": tags_added,
        "scheduling_before": scheduling_before,
        "scheduling_after": scheduling_snapshot(card_after),
        "error": error,
    }


def moved_log_entries_for_batch(batch_id: str) -> list[dict]:
    return [
        entry for entry in load_reorganization_log_entries()
        if entry.get("event_type") == "move"
        and entry.get("batch_id") == batch_id
        and entry.get("status") == "moved"
    ]


def undone_batch_ids(entries=None) -> set[str]:
    entries = load_reorganization_log_entries() if entries is None else entries
    return {
        entry.get("undone_batch_id")
        for entry in entries
        if entry.get("event_type") in {"undo", "undo_reorder_created"}
        and entry.get("status") == "undone"
        and entry.get("undone_batch_id")
    }


def undo_reorder_note_created_dates(batch_id: str, reorder_entries: list[dict]) -> dict:
    groups = {}
    for entry in reorder_entries:
        old_nid = int(entry.get("old_nid") or entry.get("note_id") or 0)
        new_nid = int(entry.get("new_nid") or old_nid)
        groups.setdefault((old_nid, new_nid), []).append(entry)

    conflicts = []
    for (old_nid, new_nid), group_entries in groups.items():
        current_note = mw.col.db.scalar("select id from notes where id = ?", new_nid)
        if current_note is None:
            conflicts.append({
                "old_nid": old_nid,
                "new_nid": new_nid,
                "reason": "current_new_nid_missing",
            })
        old_note_exists = mw.col.db.scalar("select id from notes where id = ?", old_nid)
        if old_note_exists is not None and old_nid != new_nid:
            conflicts.append({
                "old_nid": old_nid,
                "new_nid": new_nid,
                "reason": "old_nid_already_exists",
            })
        for entry in group_entries:
            old_cid = int(entry["old_cid"])
            new_cid = int(entry["new_cid"])
            current = mw.col.db.first("select id, nid from cards where id = ?", new_cid)
            if current is None:
                conflicts.append({
                    "old_cid": old_cid,
                    "new_cid": new_cid,
                    "old_nid": old_nid,
                    "new_nid": new_nid,
                    "reason": "current_new_cid_missing",
                })
                continue
            if int(current[1]) != new_nid:
                conflicts.append({
                    "old_cid": old_cid,
                    "new_cid": new_cid,
                    "old_nid": old_nid,
                    "new_nid": new_nid,
                    "reason": "current_card_nid_mismatch",
                    "current_nid": int(current[1]),
                })
            old_exists = mw.col.db.scalar("select id from cards where id = ?", old_cid)
            if old_exists is not None and old_cid != new_cid:
                conflicts.append({
                    "old_cid": old_cid,
                    "new_cid": new_cid,
                    "old_nid": old_nid,
                    "new_nid": new_nid,
                    "reason": "old_cid_already_exists",
                })

    if conflicts:
        result = {
            "undone": False,
            "batch_id": batch_id,
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
            "errors": ["undo_conflict_reorder_created_date"],
        }
        raise OperationFailedWithResult("undo_conflict_reorder_created_date", result)

    undo_batch_id = f"undo-{datetime.utcnow().strftime('%Y%m%dT%H%M%S%fZ')}-{batch_id}"
    now_seconds = int(datetime.now().timestamp())
    restored_entries = []
    transaction_started = False
    try:
        mw.col.db.execute("begin")
        transaction_started = True
        for (old_nid, new_nid), group_entries in groups.items():
            mw.col.db.execute(
                """
                update notes
                set id = ?, mod = ?, usn = -1
                where id = ?
                """,
                old_nid,
                now_seconds,
                new_nid,
            )
            for entry in group_entries:
                old_cid = int(entry["old_cid"])
                new_cid = int(entry["new_cid"])
                mw.col.db.execute(
                    """
                    update cards
                    set id = ?, nid = ?, mod = ?, usn = -1
                    where id = ?
                    """,
                    old_cid,
                    old_nid,
                    now_seconds,
                    new_cid,
                )
                restored_entries.append({
                    "event_type": "undo_reorder_created",
                    "status": "undone",
                    "batch_id": undo_batch_id,
                    "undone_batch_id": batch_id,
                    "operation_id": entry.get("operation_id", ""),
                    "operation_type": "undo_reorganization",
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "target_created_column": "note",
                    "multi_card_note_policy": entry.get("multi_card_note_policy"),
                    "old_cid": old_cid,
                    "new_cid": new_cid,
                    "old_nid": old_nid,
                    "new_nid": new_nid,
                    "note_card_count": entry.get("note_card_count"),
                    "note_card_ids": entry.get("note_card_ids"),
                    "card_ord": entry.get("card_ord"),
                    "restored_cid": old_cid,
                    "restored_nid": old_nid,
                    "note_id": old_nid,
                    "from_created": entry.get("new_created"),
                    "restored_created": entry.get("old_created"),
                    "backup_path": entry.get("backup_path", ""),
                    "revlog_cid_updated": False,
                })
        mw.col.db.execute("commit")
        transaction_started = False
    except Exception:
        if transaction_started:
            try:
                mw.col.db.execute("rollback")
            except Exception as rollback_error:
                log(
                    "organization reorder undo note rollback warning "
                    f"batch_id={batch_id} {type(rollback_error).__name__}: {rollback_error}"
                )
        raise

    for entry in restored_entries:
        append_reorganization_log(entry)
    save_collection()
    log(
        "organization reorder undo note finished "
        f"batch_id={batch_id} restored_count={len(restored_entries)}"
    )
    return {
        "undone": True,
        "batch_id": batch_id,
        "undo_batch_id": undo_batch_id,
        "restored_count": len(restored_entries),
        "restored_entries": restored_entries,
        "errors": [],
    }


def undo_reorder_created_dates(batch_id: str, entries: list[dict]) -> dict:
    reorder_entries = [
        entry for entry in entries
        if entry.get("event_type") == "reorder_created"
        and entry.get("batch_id") == batch_id
        and entry.get("status") == "reordered"
    ]
    if not reorder_entries:
        raise ValueError(f"batch_not_found_or_empty: {batch_id}")

    if any(entry.get("target_created_column") == "note" for entry in reorder_entries):
        return undo_reorder_note_created_dates(batch_id, reorder_entries)

    conflicts = []
    for entry in reorder_entries:
        old_cid = int(entry["old_cid"])
        new_cid = int(entry["new_cid"])
        target_created_column = entry.get("target_created_column", "card")
        old_nid = int(entry.get("old_nid") or entry.get("note_id") or 0)
        new_nid = int(entry.get("new_nid") or old_nid)
        current = mw.col.db.first("select id, nid from cards where id = ?", new_cid)
        if current is None:
            conflicts.append({
                "old_cid": old_cid,
                "new_cid": new_cid,
                "reason": "current_new_cid_missing",
            })
            continue
        old_exists = mw.col.db.scalar("select id from cards where id = ?", old_cid)
        if old_exists is not None and old_cid != new_cid:
            conflicts.append({
                "old_cid": old_cid,
                "new_cid": new_cid,
                "reason": "old_cid_already_exists",
            })
        if target_created_column == "note":
            if int(current[1]) != new_nid:
                conflicts.append({
                    "old_cid": old_cid,
                    "new_cid": new_cid,
                    "old_nid": old_nid,
                    "new_nid": new_nid,
                    "reason": "current_card_nid_mismatch",
                    "current_nid": int(current[1]),
                })
            current_note = mw.col.db.scalar("select id from notes where id = ?", new_nid)
            if current_note is None:
                conflicts.append({
                    "old_cid": old_cid,
                    "new_cid": new_cid,
                    "old_nid": old_nid,
                    "new_nid": new_nid,
                    "reason": "current_new_nid_missing",
                })
            old_note_exists = mw.col.db.scalar("select id from notes where id = ?", old_nid)
            if old_note_exists is not None and old_nid != new_nid:
                conflicts.append({
                    "old_cid": old_cid,
                    "new_cid": new_cid,
                    "old_nid": old_nid,
                    "new_nid": new_nid,
                    "reason": "old_nid_already_exists",
                })

    if conflicts:
        result = {
            "undone": False,
            "batch_id": batch_id,
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
            "errors": ["undo_conflict_reorder_created_date"],
        }
        raise OperationFailedWithResult("undo_conflict_reorder_created_date", result)

    undo_batch_id = f"undo-{datetime.utcnow().strftime('%Y%m%dT%H%M%S%fZ')}-{batch_id}"
    now_seconds = int(datetime.now().timestamp())
    restored_entries = []
    transaction_started = False
    try:
        if any(entry.get("target_created_column") == "note" for entry in reorder_entries):
            mw.col.db.execute("begin")
            transaction_started = True
        for entry in reversed(reorder_entries):
            old_cid = int(entry["old_cid"])
            new_cid = int(entry["new_cid"])
            target_created_column = entry.get("target_created_column", "card")
            old_nid = int(entry.get("old_nid") or entry.get("note_id") or 0)
            new_nid = int(entry.get("new_nid") or old_nid)
            if target_created_column == "note":
                mw.col.db.execute(
                    """
                    update notes
                    set id = ?, mod = ?, usn = -1
                    where id = ?
                    """,
                    old_nid,
                    now_seconds,
                    new_nid,
                )
                mw.col.db.execute(
                    """
                    update cards
                    set id = ?, nid = ?, mod = ?, usn = -1
                    where id = ?
                    """,
                    old_cid,
                    old_nid,
                    now_seconds,
                    new_cid,
                )
            else:
                mw.col.db.execute(
                    """
                    update cards
                    set id = ?, mod = ?, usn = -1
                    where id = ?
                    """,
                    old_cid,
                    now_seconds,
                    new_cid,
                )
            undo_entry = {
                "event_type": "undo_reorder_created",
                "status": "undone",
                "batch_id": undo_batch_id,
                "undone_batch_id": batch_id,
                "operation_id": entry.get("operation_id", ""),
                "operation_type": "undo_reorganization",
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "target_created_column": target_created_column,
                "old_cid": old_cid,
                "new_cid": new_cid,
                "old_nid": old_nid,
                "new_nid": new_nid,
                "restored_cid": old_cid,
                "restored_nid": old_nid,
                "note_id": old_nid,
                "from_created": entry.get("new_created"),
                "restored_created": entry.get("old_created"),
                "backup_path": entry.get("backup_path", ""),
                "revlog_cid_updated": False,
            }
            restored_entries.append(undo_entry)
        if transaction_started:
            mw.col.db.execute("commit")
            transaction_started = False
    except Exception:
        if transaction_started:
            try:
                mw.col.db.execute("rollback")
            except Exception as rollback_error:
                log(
                    "organization reorder undo rollback warning "
                    f"batch_id={batch_id} {type(rollback_error).__name__}: {rollback_error}"
                )
        raise

    for undo_entry in restored_entries:
        append_reorganization_log(undo_entry)
        log(
            "organization reorder undo entry "
            + json.dumps(undo_entry, ensure_ascii=False, sort_keys=True)
        )

    save_collection()
    log(
        "organization reorder undo finished "
        f"batch_id={batch_id} undo_batch_id={undo_batch_id} restored_count={len(restored_entries)}"
    )
    return {
        "undone": True,
        "batch_id": batch_id,
        "undo_batch_id": undo_batch_id,
        "restored_count": len(restored_entries),
        "card_ids": [entry["restored_cid"] for entry in restored_entries],
        "note_ids": sorted({
            entry["note_id"]
            for entry in restored_entries
            if entry.get("note_id")
        }),
        "revlog_cid_updated": False,
        "preserved": {
            "note_id": True,
            "scheduling": True,
            "review_history": False,
            "fields": True,
            "media": True,
        },
        "entries": restored_entries,
        "errors": [],
    }


def undo_reorganization(batch_id: str) -> dict:
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ValueError("batch_id_empty")
    batch_id = batch_id.strip()

    entries = load_reorganization_log_entries()
    if batch_id in undone_batch_ids(entries):
        raise ValueError(f"batch_already_undone: {batch_id}")

    move_entries = [
        entry for entry in entries
        if entry.get("event_type") == "move"
        and entry.get("batch_id") == batch_id
        and entry.get("status") == "moved"
    ]
    if not move_entries:
        return undo_reorder_created_dates(batch_id, entries)

    conflicts = []
    cards = []
    deck_name_map = get_deck_name_map()
    for entry in move_entries:
        card_id = int(entry["card_id"])
        card = get_card(card_id)
        current_deck_id = int(card_attr(card, "did"))
        expected_deck_id = int(entry["new_deck_id"])
        if current_deck_id != expected_deck_id:
            conflicts.append({
                "card_id": card_id,
                "note_id": int(card_attr(card, "nid")),
                "expected_new_deck": entry.get("new_deck", ""),
                "expected_new_deck_id": expected_deck_id,
                "current_deck": deck_name_map.get(current_deck_id, ""),
                "current_deck_id": current_deck_id,
            })
        cards.append(card)

    if conflicts:
        result = {
            "undone": False,
            "batch_id": batch_id,
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
            "errors": ["undo_conflict_card_not_in_recorded_new_deck"],
        }
        raise OperationFailedWithResult("undo_conflict_card_not_in_recorded_new_deck", result)

    undo_batch_id = f"undo-{datetime.utcnow().strftime('%Y%m%dT%H%M%S%fZ')}-{batch_id}"
    by_old_deck_id = {}
    for card, entry in zip(cards, move_entries):
        old_deck_id = int(entry["old_deck_id"])
        current_decks = get_deck_name_map()
        if old_deck_id not in current_decks:
            old_deck = entry.get("old_deck", "")
            if not old_deck:
                raise ValueError(f"undo_old_deck_missing: {old_deck_id}")
            old_deck_result = ensure_deck(old_deck, dry_run=False)
            old_deck_id = int(old_deck_result["deck_id"])
            entry["restore_deck_id"] = old_deck_id
            entry["restore_deck_recreated"] = bool(old_deck_result.get("created"))
        else:
            entry["restore_deck_id"] = old_deck_id
            entry["restore_deck_recreated"] = False
        by_old_deck_id.setdefault(entry["restore_deck_id"], []).append(card)

    moved_back_entries = []
    for old_deck_id, grouped_cards in by_old_deck_id.items():
        update_cards_deck(grouped_cards, old_deck_id)

    save_collection()

    for entry in move_entries:
        card = get_card(int(entry["card_id"]))
        current_deck_id = int(card_attr(card, "did"))
        old_deck_id = int(entry["restore_deck_id"])
        current_deck = get_deck_name_map().get(current_deck_id, "")
        if current_deck_id != old_deck_id:
            raise RuntimeError(
                f"undo_incomplete: card_id={entry['card_id']} "
                f"current_deck_id={current_deck_id} expected_old_deck_id={old_deck_id}"
            )

        undo_entry = {
            "event_type": "undo",
            "status": "undone",
            "batch_id": undo_batch_id,
            "undone_batch_id": batch_id,
            "operation_id": entry.get("operation_id", ""),
            "operation_type": "undo_reorganization",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "card_id": int(entry["card_id"]),
            "note_id": int(entry["note_id"]),
            "from_deck": entry.get("new_deck", ""),
            "from_deck_id": int(entry["new_deck_id"]),
            "restored_deck": current_deck,
            "restored_deck_id": current_deck_id,
            "original_old_deck_id": int(entry["old_deck_id"]),
            "restore_deck_recreated": bool(entry.get("restore_deck_recreated")),
            "tags_removed": [],
            "scheduling_after_undo": scheduling_snapshot(card),
        }
        append_reorganization_log(undo_entry)
        moved_back_entries.append(undo_entry)

    return {
        "undone": True,
        "batch_id": batch_id,
        "undo_batch_id": undo_batch_id,
        "restored_count": len(moved_back_entries),
        "card_ids": [entry["card_id"] for entry in moved_back_entries],
        "note_ids": sorted({entry["note_id"] for entry in moved_back_entries}),
        "tags_removed": [],
        "preserved": {
            "card_id": True,
            "note_id": True,
            "scheduling": True,
            "review_history": True,
            "fields": True,
            "media": True,
        },
        "entries": moved_back_entries,
        "errors": [],
    }


def undo_last_reorganization() -> dict:
    batches = reorganization_batches()
    for batch in batches:
        if not batch.get("undone") and (
            batch.get("moved_count", 0) > 0
            or batch.get("reordered_count", 0) > 0
        ):
            return undo_reorganization(batch["batch_id"])
    raise ValueError("no_reorganization_batch_to_undo")


def normalize_deck_argument(deck: str) -> str:
    if not isinstance(deck, str) or not deck.strip():
        raise ValueError("deck_empty")
    value = deck.strip()
    if value.casefold().startswith("deck:"):
        value = value[5:].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    if not value:
        raise ValueError("deck_empty")
    return value


def deck_id_by_name(deck_name: str) -> int:
    for item in mw.col.decks.all_names_and_ids():
        if item.name == deck_name:
            return int(item.id)
    raise ValueError(f"deck_not_found: {deck_name}")


def effective_base_datetime_string(value) -> str:
    if value is None:
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    if not isinstance(value, str):
        raise ValueError("invalid_base_datetime")
    raw = value.strip()
    if not raw:
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    return raw


def has_explicit_base_datetime(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def timestamp_ms_from_datetime_string(value) -> int:
    raw = effective_base_datetime_string(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return int(parsed.timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError("invalid_base_datetime")


def local_day_bounds_ms(base_ms: int) -> tuple[int, int]:
    day_start = datetime.fromtimestamp(int(base_ms) / 1000).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(day_start.timestamp() * 1000)
    end_ms = start_ms + 24 * 60 * 60 * 1000 - 1
    return start_ms, end_ms


def used_created_ids_for_day(start_ms: int, end_ms: int) -> set[int]:
    ids = set()
    for table, column in (("cards", "id"), ("notes", "id"), ("revlog", "cid")):
        try:
            ids.update(
                int(value)
                for value in mw.col.db.list(
                    f"select {column} from {table} where {column} between ? and ?",
                    int(start_ms),
                    int(end_ms),
                )
            )
        except Exception as e:
            log(f"organization reorder day-id scan warning table={table} {type(e).__name__}: {e}")
    return ids


def align_after_used_id(day_start_ms: int, used_id: int, spacing_ms: int) -> int:
    offset = max(0, int(used_id) - int(day_start_ms))
    return int(day_start_ms) + ((offset // int(spacing_ms)) + 1) * int(spacing_ms)


def minute_bucket(day_start_ms: int, value_ms: int) -> int:
    return max(0, (int(value_ms) - int(day_start_ms)) // 60000)


def choose_reorder_base_ms(
    *,
    requested_base_datetime,
    initial_base_ms: int,
    planned_offsets: list[int],
    spacing_ms: int,
) -> dict:
    initial_base_ms = int(initial_base_ms)
    if has_explicit_base_datetime(requested_base_datetime) or not planned_offsets:
        return {
            "base_ms": initial_base_ms,
            "auto_shifted_base_datetime": False,
            "collision_avoidance_reason": "explicit_base_datetime" if planned_offsets else "no_eligible_items",
        }

    spacing_ms = int(spacing_ms)
    offsets = sorted({int(offset) for offset in planned_offsets})
    block_span_ms = max(offsets)
    day_start_ms, day_end_ms = local_day_bounds_ms(initial_base_ms)
    if day_start_ms + block_span_ms > day_end_ms:
        raise ValueError("no_free_created_slot_for_day")

    used_ids = used_created_ids_for_day(day_start_ms, day_end_ms)
    used_minutes = {minute_bucket(day_start_ms, used_id) for used_id in used_ids}
    initial_planned_ids = {day_start_ms + offset for offset in offsets}
    initial_planned_minutes = {minute_bucket(day_start_ms, planned_id) for planned_id in initial_planned_ids}
    if not initial_planned_ids.intersection(used_ids) and not initial_planned_minutes.intersection(used_minutes):
        return {
            "base_ms": day_start_ms,
            "auto_shifted_base_datetime": False,
            "collision_avoidance_reason": "initial_day_start_available",
        }

    max_used_minute = max(used_minutes) if used_minutes else 0
    candidate_ms = day_start_ms + (max_used_minute + 1) * 60000
    if candidate_ms + block_span_ms > day_end_ms:
        raise ValueError("no_free_created_slot_for_day")
    planned_ids = {candidate_ms + offset for offset in offsets}
    planned_minutes = {minute_bucket(day_start_ms, planned_id) for planned_id in planned_ids}
    if planned_ids.intersection(used_ids) or planned_minutes.intersection(used_minutes):
        raise ValueError("no_free_created_slot_for_day")
    return {
        "base_ms": candidate_ms,
        "auto_shifted_base_datetime": candidate_ms != day_start_ms,
        "collision_avoidance_reason": "shifted_after_last_used_created_minute_for_day",
    }


def datetime_string_from_cid(cid: int) -> str:
    try:
        return datetime.fromtimestamp(int(cid) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def card_state_label(card_type: int, queue: int) -> str:
    if card_type == 0:
        if queue == 0:
            return "new"
        if queue == -1:
            return "new_suspended"
        if queue in (-2, -3):
            return "new_buried"
        return f"new_queue_{queue}"
    if card_type == 1:
        return "learning"
    if card_type == 2:
        return "review"
    if card_type == 3:
        return "relearning"
    return f"type_{card_type}_queue_{queue}"


def normalize_target_created_column(target_created_column=None, apply_note_created_date: bool = False) -> str:
    if apply_note_created_date:
        return "note"
    if target_created_column is None:
        return "card"
    if not isinstance(target_created_column, str):
        raise ValueError("invalid_target_created_column")
    value = target_created_column.strip().casefold()
    if value not in {"card", "note"}:
        raise ValueError("unsupported_target_created_column")
    return value


def normalize_multi_card_note_policy(value=None, target_created_column: str = "card") -> str:
    if value is None:
        return "group_if_all_new" if target_created_column == "note" else "single_card_only"
    if not isinstance(value, str):
        raise ValueError("unsupported_multi_card_note_policy")
    value = value.strip().casefold()
    if value not in {"single_card_only", "group_if_all_new"}:
        raise ValueError("unsupported_multi_card_note_policy")
    return value


def note_summary(note, max_chars: int = 160) -> str:
    parts = []
    try:
        for name in list(note.keys()):
            value = note[name]
            if isinstance(value, str) and value.strip():
                parts.append(strip_html_text(value))
    except Exception:
        pass
    summary = re.sub(r"\s+", " ", " ".join(parts)).strip()
    if len(summary) > max_chars:
        summary = summary[: max_chars - 3].rstrip() + "..."
    return summary


def deck_reorder_cards(deck_name: str) -> list[dict]:
    deck_id = deck_id_by_name(deck_name)
    rows = mw.col.db.all(
        """
        select id, nid, did, ord, type, queue, due, ivl, reps, lapses, flags, data
        from cards
        where did = ?
        order by nid, ord, id
        """,
        deck_id,
    )
    cards = []
    note_cache = {}
    for row in rows:
        (
            card_id,
            note_id,
            deck_id_value,
            ord_value,
            card_type,
            queue,
            due,
            ivl,
            reps,
            lapses,
            flags,
            data,
        ) = row
        note_id = int(note_id)
        note = note_cache.get(note_id)
        if note is None:
            note = get_note(note_id)
            note_cache[note_id] = note
        note_card_rows = mw.col.db.all(
            """
            select id, did, ord, type, queue
            from cards
            where nid = ?
            order by ord, id
            """,
            note_id,
        )
        cards.append({
            "cid": int(card_id),
            "nid": note_id,
            "deck_id": int(deck_id_value),
            "ord": int(ord_value),
            "type": int(card_type),
            "queue": int(queue),
            "due": int(due),
            "ivl": int(ivl),
            "reps": int(reps),
            "lapses": int(lapses),
            "flags": int(flags),
            "data": data if isinstance(data, str) else "",
            "note_type": note_type_name(note),
            "template_name": template_name_for_card(get_card(int(card_id)), note),
            "tags": list(note.tags),
            "summary": note_summary(note),
            "note_card_count": len(note_card_rows),
            "note_card_ids": [int(item[0]) for item in note_card_rows],
            "note_cards": [
                {
                    "cid": int(item[0]),
                    "did": int(item[1]),
                    "ord": int(item[2]),
                    "type": int(item[3]),
                    "queue": int(item[4]),
                    "card_state": card_state_label(int(item[3]), int(item[4])),
                }
                for item in note_card_rows
            ],
        })
    return cards


def order_reorder_cards(cards: list[dict], ordered_card_ids=None, ordered_note_ids=None) -> tuple[list[dict], dict]:
    ordered_card_ids = ordered_card_ids if isinstance(ordered_card_ids, list) else []
    ordered_note_ids = ordered_note_ids if isinstance(ordered_note_ids, list) else []
    by_cid = {card["cid"]: card for card in cards}
    by_nid = {}
    for card in cards:
        by_nid.setdefault(card["nid"], []).append(card)

    duplicate_ids = set()
    seen_requested = set()
    ordered = []
    seen_cards = set()

    for raw_cid in ordered_card_ids:
        cid = int(raw_cid)
        if cid in seen_requested:
            duplicate_ids.add(cid)
            continue
        seen_requested.add(cid)
        card = by_cid.get(cid)
        if card is not None and cid not in seen_cards:
            ordered.append(card)
            seen_cards.add(cid)

    for raw_nid in ordered_note_ids:
        nid = int(raw_nid)
        for card in by_nid.get(nid, []):
            cid = card["cid"]
            if cid not in seen_cards:
                ordered.append(card)
                seen_cards.add(cid)

    for card in sorted(cards, key=lambda item: (item["nid"], item["ord"], item["cid"])):
        if card["cid"] not in seen_cards:
            ordered.append(card)
            seen_cards.add(card["cid"])

    return ordered, {
        "used_explicit_order": bool(ordered_card_ids or ordered_note_ids),
        "duplicate_order_card_ids": sorted(duplicate_ids),
    }


def order_reorder_note_groups(cards: list[dict], ordered_card_ids=None, ordered_note_ids=None) -> tuple[list[dict], dict]:
    ordered_card_ids = ordered_card_ids if isinstance(ordered_card_ids, list) else []
    ordered_note_ids = ordered_note_ids if isinstance(ordered_note_ids, list) else []
    groups_by_nid = {}
    card_to_nid = {}
    for card in cards:
        nid = int(card["nid"])
        group = groups_by_nid.setdefault(nid, {
            "nid": nid,
            "old_nid": nid,
            "cards_in_deck": [],
            "summary": card.get("summary", ""),
            "note_type": card.get("note_type", ""),
            "tags": card.get("tags", []),
            "note_cards": card.get("note_cards", []),
        })
        group["cards_in_deck"].append(card)
        card_to_nid[int(card["cid"])] = nid
        for note_card in card.get("note_cards", []):
            card_to_nid[int(note_card["cid"])] = nid

    for group in groups_by_nid.values():
        group["cards_in_deck"].sort(key=lambda item: (int(item["ord"]), int(item["cid"])))
        group["note_cards"] = sorted(group.get("note_cards", []), key=lambda item: (int(item["ord"]), int(item["cid"])))
        group["note_card_ids"] = [int(item["cid"]) for item in group["note_cards"]]
        group["note_card_count"] = len(group["note_cards"])
        group["deck_card_ids"] = [int(item["cid"]) for item in group["cards_in_deck"]]

    duplicate_card_ids = set()
    duplicate_note_ids = set()
    seen_requested_cards = set()
    seen_requested_notes = set()
    ordered = []
    seen_notes = set()

    for raw_nid in ordered_note_ids:
        nid = int(raw_nid)
        if nid in seen_requested_notes:
            duplicate_note_ids.add(nid)
            continue
        seen_requested_notes.add(nid)
        group = groups_by_nid.get(nid)
        if group is not None and nid not in seen_notes:
            ordered.append(group)
            seen_notes.add(nid)

    for raw_cid in ordered_card_ids:
        cid = int(raw_cid)
        if cid in seen_requested_cards:
            duplicate_card_ids.add(cid)
            continue
        seen_requested_cards.add(cid)
        nid = card_to_nid.get(cid)
        group = groups_by_nid.get(nid)
        if group is not None and nid not in seen_notes:
            ordered.append(group)
            seen_notes.add(nid)

    for group in sorted(groups_by_nid.values(), key=lambda item: (
        int(item["cards_in_deck"][0]["nid"]),
        int(item["cards_in_deck"][0]["ord"]),
        int(item["cards_in_deck"][0]["cid"]),
    )):
        nid = int(group["nid"])
        if nid not in seen_notes:
            ordered.append(group)
            seen_notes.add(nid)

    return ordered, {
        "used_explicit_order": bool(ordered_card_ids or ordered_note_ids),
        "duplicate_order_card_ids": sorted(duplicate_card_ids),
        "duplicate_order_note_ids": sorted(duplicate_note_ids),
    }


def card_prior_revlog_count(cid: int) -> int:
    return int(mw.col.db.scalar("select count(*) from revlog where cid = ?", int(cid)) or 0)


def is_used_card_id(cid: int, ignored_card_ids=None) -> bool:
    ignored_card_ids = set(ignored_card_ids or [])
    row = mw.col.db.scalar("select id from cards where id = ?", int(cid))
    return row is not None and int(row) not in ignored_card_ids


def is_used_revlog_cid(cid: int) -> bool:
    return mw.col.db.scalar("select cid from revlog where cid = ?", int(cid)) is not None


def collection_path() -> Path:
    path = getattr(mw.col, "path", None)
    if callable(path):
        path = path()
    if not path:
        raise RuntimeError("collection_path_unavailable")
    return Path(path)


def create_reorder_backup(batch_id: str) -> dict:
    save_collection()
    try:
        mw.col.db.execute("pragma wal_checkpoint(full)")
    except Exception as e:
        log(f"organization reorder backup checkpoint warning batch_id={batch_id} {type(e).__name__}: {e}")

    source = collection_path()
    if not source.exists():
        raise RuntimeError(f"collection_file_not_found: {source}")

    safe_batch_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", batch_id)[:120]
    backup_dir = RUNTIME_PATHS.backups / "reorder_created_date"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{safe_batch_id}-{source.name}"
    shutil.copy2(str(source), str(backup_path))

    sidecars = []
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists():
            sidecar_backup = Path(str(backup_path) + suffix)
            shutil.copy2(str(sidecar), str(sidecar_backup))
            sidecars.append(str(sidecar_backup))

    return {
        "source": str(source),
        "backup_path": str(backup_path),
        "sidecar_backups": sidecars,
    }


def evaluate_reorder_note_group(group: dict, multi_card_note_policy: str) -> tuple[bool, list[str], dict]:
    note_cards = list(group.get("note_cards", []))
    note_card_ids = [int(card["cid"]) for card in note_cards]
    deck_card_ids = {int(cid) for cid in group.get("deck_card_ids", [])}
    note_card_count = len(note_cards)
    warnings = []
    non_eligible_by_state = {}
    eligible = True

    if multi_card_note_policy == "single_card_only" and note_card_count != 1:
        eligible = False
        warnings.append("note_has_multiple_cards_requires_policy")
        non_eligible_by_state["note_has_multiple_cards_requires_policy"] = note_card_count
    elif not note_cards:
        eligible = False
        warnings.append("note_has_no_cards")
        non_eligible_by_state["note_has_no_cards"] = 1
    elif multi_card_note_policy == "group_if_all_new":
        outside_scope_ids = sorted(set(note_card_ids) - deck_card_ids)
        if outside_scope_ids:
            eligible = False
            warnings.append("multi_card_note_cards_outside_scope")
            non_eligible_by_state["multi_card_note_cards_outside_scope"] = len(outside_scope_ids)
        non_new_cards = [card for card in note_cards if int(card["type"]) != 0]
        if non_new_cards:
            eligible = False
            warnings.append("multi_card_note_has_non_new_cards")
            for card in non_new_cards:
                state = card.get("card_state") or card_state_label(int(card["type"]), int(card["queue"]))
                non_eligible_by_state[state] = non_eligible_by_state.get(state, 0) + 1
    else:
        eligible = False
        warnings.append("unsupported_multi_card_note_policy")

    return eligible, warnings, non_eligible_by_state


def build_reorder_note_preview(
    *,
    deck: str,
    dry_run: bool,
    base_datetime,
    spacing_ms: int,
    target_created_column: str,
    multi_card_note_policy: str,
    ordered_card_ids=None,
    ordered_note_ids=None,
    operation_id: str = "",
    batch_id: str = "",
) -> dict:
    deck_name = normalize_deck_argument(deck)
    requested_base_datetime = base_datetime
    effective_base_datetime = effective_base_datetime_string(base_datetime)
    initial_base_ms = timestamp_ms_from_datetime_string(effective_base_datetime)
    spacing_ms = int(spacing_ms)
    if spacing_ms < 1:
        raise ValueError("invalid_spacing_ms")

    cards = deck_reorder_cards(deck_name)
    ordered_groups, order_info = order_reorder_note_groups(cards, ordered_card_ids, ordered_note_ids)
    batch_id = (batch_id or operation_id or f"reorder-{datetime.utcnow().strftime('%Y%m%dT%H%M%S%fZ')}").strip()

    planned_offsets = []
    next_offset = 0
    for group in ordered_groups:
        eligible, _warnings, _state_counts = evaluate_reorder_note_group(group, multi_card_note_policy)
        if not eligible:
            continue
        note_card_count = len(group.get("note_cards", []))
        planned_offsets.append(next_offset)
        planned_offsets.extend(next_offset + index for index in range(note_card_count))
        next_offset += max(spacing_ms, note_card_count)
    base_choice = choose_reorder_base_ms(
        requested_base_datetime=requested_base_datetime,
        initial_base_ms=initial_base_ms,
        planned_offsets=planned_offsets,
        spacing_ms=spacing_ms,
    )
    base_ms = int(base_choice["base_ms"])
    effective_base_datetime = datetime_string_from_cid(base_ms)

    items = []
    eligible_card_ids = []
    non_eligible_by_state = {}
    planned_new_cids = set()
    planned_new_nids = set()
    collision_warnings = []
    duplicate_order_card_ids = set(order_info["duplicate_order_card_ids"])
    duplicate_order_note_ids = set(order_info["duplicate_order_note_ids"])
    next_note_ms = base_ms

    for position, group in enumerate(ordered_groups, start=1):
        old_nid = int(group["old_nid"])
        note_cards = list(group.get("note_cards", []))
        note_card_ids = [int(card["cid"]) for card in note_cards]
        deck_card_ids = {int(cid) for cid in group.get("deck_card_ids", [])}
        note_card_count = len(note_cards)
        eligible, warnings, state_counts = evaluate_reorder_note_group(group, multi_card_note_policy)
        for state, count in state_counts.items():
            non_eligible_by_state[state] = non_eligible_by_state.get(state, 0) + int(count)

        if old_nid in duplicate_order_note_ids:
            warnings.append("possible_duplicate_position")
        if duplicate_order_card_ids.intersection(note_card_ids):
            warnings.append("possible_duplicate_position")
        if not order_info["used_explicit_order"]:
            warnings.append("insufficient_context_for_ordering")

        new_nid = None
        if eligible:
            new_nid = next_note_ms
            if new_nid in planned_new_nids:
                warnings.append("possible_duplicate_position")
                collision_warnings.append({"nid": new_nid, "reason": "duplicate_planned_new_nid"})
            planned_new_nids.add(new_nid)
        else:
            new_nid = old_nid

        card_items = []
        for card_index, card in enumerate(note_cards):
            old_cid = int(card["cid"])
            card_type = int(card["type"])
            queue = int(card["queue"])
            state = card.get("card_state") or card_state_label(card_type, queue)
            card_warnings = list(warnings)
            if eligible:
                new_cid = next_note_ms + card_index
                new_created = datetime_string_from_cid(new_cid)
                eligible_card_ids.append(old_cid)
                if queue < 0:
                    card_warnings.append("card_suspended_but_new")
                if card_prior_revlog_count(old_cid) > 0:
                    card_warnings.append("card_currently_new_but_has_prior_revlog")
                if new_cid in planned_new_cids:
                    card_warnings.append("possible_duplicate_position")
                    collision_warnings.append({"cid": new_cid, "reason": "duplicate_planned_new_cid"})
                planned_new_cids.add(new_cid)
            else:
                new_cid = None
                new_created = None
                if card_type != 0:
                    card_warnings.append("skipped_not_new")

            card_items.append({
                "old_cid": old_cid,
                "cid": old_cid,
                "current_cid": old_cid,
                "new_cid": new_cid,
                "ord": int(card["ord"]),
                "card_ord": int(card["ord"]),
                "card_state": state,
                "queue": queue,
                "type": card_type,
                "created_current": datetime_string_from_cid(old_cid),
                "old_created": datetime_string_from_cid(old_cid),
                "created_new": new_created,
                "new_created": new_created,
                "eligible": eligible and card_type == 0,
                "warnings": sorted(set(card_warnings)),
            })

        if eligible:
            next_note_ms += max(spacing_ms, note_card_count)

        first_card = card_items[0] if card_items else {}
        reason = (
            "explicit_order_from_payload"
            if order_info["used_explicit_order"]
            else "fallback_current_note_template_order"
        )
        group_warnings = sorted(set(warnings) | {
            warning
            for card in card_items
            for warning in card.get("warnings", [])
        })
        items.append({
            "position": position,
            "unit": "note",
            "nid": old_nid,
            "old_nid": old_nid,
            "new_nid": new_nid,
            "cid": first_card.get("old_cid"),
            "old_cid": first_card.get("old_cid"),
            "current_cid": first_card.get("old_cid"),
            "new_cid": first_card.get("new_cid"),
            "card_state": "group_all_new" if eligible else "group_skipped",
            "created_current": first_card.get("created_current"),
            "note_created_current": datetime_string_from_cid(old_nid),
            "old_created": datetime_string_from_cid(old_nid),
            "created_new": datetime_string_from_cid(new_nid) if eligible else None,
            "new_created": datetime_string_from_cid(new_nid) if eligible else None,
            "summary": group.get("summary", ""),
            "note_type": group.get("note_type", ""),
            "template_name": "note_group",
            "note_card_count": note_card_count,
            "note_card_ids": note_card_ids,
            "cards": card_items,
            "reason": reason,
            "eligible": eligible,
            "target_created_column": target_created_column,
            "multi_card_note_policy": multi_card_note_policy,
            "warnings": group_warnings,
        })

    return {
        "operation": "reorder_cards_by_material",
        "operation_id": operation_id,
        "batch_id": batch_id,
        "dry_run": dry_run,
        "deck": deck_name,
        "scope": "currently_new_cards",
        "apply_created_date": True,
        "target_created_column": target_created_column,
        "apply_note_created_date": True,
        "multi_card_note_policy": multi_card_note_policy,
        "ordering_unit": "note",
        "requested_base_datetime": requested_base_datetime,
        "effective_base_datetime": effective_base_datetime,
        "auto_shifted_base_datetime": base_choice["auto_shifted_base_datetime"],
        "collision_avoidance_reason": base_choice["collision_avoidance_reason"],
        "base_datetime": effective_base_datetime,
        "base_datetime_ms": base_ms,
        "spacing_ms": spacing_ms,
        "total_cards": len(cards),
        "total_notes": len({card["nid"] for card in cards}),
        "total_eligible_new": len(eligible_card_ids),
        "total_not_eligible": len(cards) - len(eligible_card_ids),
        "not_eligible_by_state": non_eligible_by_state,
        "eligible_card_ids": eligible_card_ids,
        "ordered_card_ids": [cid for item in items for cid in item.get("note_card_ids", [])],
        "ordered_note_ids": [item["old_nid"] for item in items],
        "planned_new_cids": [
            card["new_cid"]
            for item in items
            if item.get("eligible")
            for card in item.get("cards", [])
            if card.get("new_cid") is not None
        ],
        "planned_new_nids": [
            item["new_nid"]
            for item in items
            if item.get("eligible")
        ],
        "ordering_source": "explicit_payload" if order_info["used_explicit_order"] else "fallback_current_note_template_order",
        "warnings": sorted({
            warning
            for item in items
            for warning in item.get("warnings", [])
        }),
        "collision_warnings": collision_warnings,
        "proposed_order": items,
        "preserved": {
            "note_id": False,
            "scheduling": True,
            "review_history": False,
            "fields": True,
            "media": True,
            "notes_id": False,
        },
        "notes_id_changed": True,
        "revlog_cid_updated": False,
        "errors": [],
    }


def build_reorder_preview(
    *,
    deck: str,
    dry_run: bool,
    base_datetime,
    spacing_ms: int,
    target_created_column: str = "card",
    multi_card_note_policy: str = "single_card_only",
    ordered_card_ids=None,
    ordered_note_ids=None,
    operation_id: str = "",
    batch_id: str = "",
) -> dict:
    if target_created_column == "note":
        return build_reorder_note_preview(
            deck=deck,
            dry_run=dry_run,
            base_datetime=base_datetime,
            spacing_ms=spacing_ms,
            target_created_column=target_created_column,
            multi_card_note_policy=multi_card_note_policy,
            ordered_card_ids=ordered_card_ids,
            ordered_note_ids=ordered_note_ids,
            operation_id=operation_id,
            batch_id=batch_id,
        )

    deck_name = normalize_deck_argument(deck)
    requested_base_datetime = base_datetime
    effective_base_datetime = effective_base_datetime_string(base_datetime)
    initial_base_ms = timestamp_ms_from_datetime_string(effective_base_datetime)
    spacing_ms = int(spacing_ms)
    if spacing_ms < 1:
        raise ValueError("invalid_spacing_ms")

    cards = deck_reorder_cards(deck_name)
    ordered_cards, order_info = order_reorder_cards(cards, ordered_card_ids, ordered_note_ids)
    batch_id = (batch_id or operation_id or f"reorder-{datetime.utcnow().strftime('%Y%m%dT%H%M%S%fZ')}").strip()
    planned_offsets = [
        index * spacing_ms
        for index, card in enumerate(card for card in ordered_cards if int(card["type"]) == 0)
    ]
    base_choice = choose_reorder_base_ms(
        requested_base_datetime=requested_base_datetime,
        initial_base_ms=initial_base_ms,
        planned_offsets=planned_offsets,
        spacing_ms=spacing_ms,
    )
    base_ms = int(base_choice["base_ms"])
    effective_base_datetime = datetime_string_from_cid(base_ms)

    eligible_index = 0
    items = []
    eligible_card_ids = []
    non_eligible_by_state = {}
    planned_new_cids = set()
    collision_warnings = []
    duplicate_order_ids = set(order_info["duplicate_order_card_ids"])

    for position, card in enumerate(ordered_cards, start=1):
        card_type = int(card["type"])
        queue = int(card["queue"])
        current_cid = int(card["cid"])
        state = card_state_label(card_type, queue)
        warnings = []
        note_card_count = int(card.get("note_card_count") or 0)
        eligible = card_type == 0

        if not eligible:
            warnings.append("skipped_not_new")
            non_eligible_by_state[state] = non_eligible_by_state.get(state, 0) + 1
        elif target_created_column == "note" and note_card_count != 1:
            eligible = False
            warnings.append("note_has_multiple_cards_requires_policy")
            non_eligible_by_state["note_has_multiple_cards_requires_policy"] = (
                non_eligible_by_state.get("note_has_multiple_cards_requires_policy", 0) + 1
            )
        else:
            eligible_card_ids.append(current_cid)
            if queue < 0:
                warnings.append("card_suspended_but_new")
            if card_prior_revlog_count(current_cid) > 0:
                warnings.append("card_currently_new_but_has_prior_revlog")

        if current_cid in duplicate_order_ids:
            warnings.append("possible_duplicate_position")
        if not order_info["used_explicit_order"]:
            warnings.append("insufficient_context_for_ordering")

        new_cid = None
        new_nid = None
        new_created = None
        if eligible:
            new_cid = base_ms + eligible_index * spacing_ms
            new_nid = new_cid if target_created_column == "note" else int(card["nid"])
            eligible_index += 1
            new_created = datetime_string_from_cid(new_cid)
            if new_cid in planned_new_cids:
                warnings.append("possible_duplicate_position")
                collision_warnings.append({
                    "cid": new_cid,
                    "reason": "duplicate_planned_new_cid",
                })
            planned_new_cids.add(new_cid)

        reason = (
            "explicit_order_from_payload"
            if order_info["used_explicit_order"]
            else "fallback_current_note_template_order"
        )
        old_created_for_target = (
            datetime_string_from_cid(int(card["nid"]))
            if target_created_column == "note"
            else datetime_string_from_cid(current_cid)
        )
        items.append({
            "position": position,
            "nid": int(card["nid"]),
            "old_nid": int(card["nid"]),
            "new_nid": new_nid if new_nid is not None else int(card["nid"]),
            "cid": current_cid,
            "old_cid": current_cid,
            "current_cid": current_cid,
            "new_cid": new_cid,
            "card_state": state,
            "queue": queue,
            "type": card_type,
            "created_current": datetime_string_from_cid(current_cid),
            "note_created_current": datetime_string_from_cid(int(card["nid"])),
            "old_created": old_created_for_target,
            "created_new": new_created,
            "new_created": new_created,
            "summary": card["summary"],
            "note_type": card["note_type"],
            "template_name": card["template_name"],
            "note_card_count": note_card_count,
            "note_card_ids": card.get("note_card_ids", []),
            "reason": reason,
            "eligible": eligible,
            "warnings": warnings,
        })

    return {
        "operation": "reorder_cards_by_material",
        "operation_id": operation_id,
        "batch_id": batch_id,
        "dry_run": dry_run,
        "deck": deck_name,
        "scope": "currently_new_cards",
        "apply_created_date": True,
        "target_created_column": target_created_column,
        "apply_note_created_date": target_created_column == "note",
        "multi_card_note_policy": multi_card_note_policy,
        "ordering_unit": "card",
        "requested_base_datetime": requested_base_datetime,
        "effective_base_datetime": effective_base_datetime,
        "auto_shifted_base_datetime": base_choice["auto_shifted_base_datetime"],
        "collision_avoidance_reason": base_choice["collision_avoidance_reason"],
        "base_datetime": effective_base_datetime,
        "base_datetime_ms": base_ms,
        "spacing_ms": spacing_ms,
        "total_cards": len(cards),
        "total_notes": len({card["nid"] for card in cards}),
        "total_eligible_new": len(eligible_card_ids),
        "total_not_eligible": len(cards) - len(eligible_card_ids),
        "not_eligible_by_state": non_eligible_by_state,
        "eligible_card_ids": eligible_card_ids,
        "ordered_card_ids": [item["cid"] for item in items],
        "planned_new_cids": [item["new_cid"] for item in items if item.get("new_cid") is not None],
        "planned_new_nids": [
            item["new_nid"]
            for item in items
            if item.get("eligible") and target_created_column == "note"
        ],
        "ordering_source": "explicit_payload" if order_info["used_explicit_order"] else "fallback_current_note_template_order",
        "warnings": sorted({
            warning
            for item in items
            for warning in item.get("warnings", [])
        }),
        "collision_warnings": collision_warnings,
        "proposed_order": items,
        "preserved": {
            "note_id": True,
            "scheduling": True,
            "review_history": False,
            "fields": True,
            "media": True,
            "notes_id": True,
        },
        "notes_id_changed": False,
        "revlog_cid_updated": False,
        "errors": [],
    }


def assert_reorder_note_group_apply_safe(preview: dict, expected_eligible_count=None, expected_eligible_card_ids=None) -> None:
    eligible_ids = list(preview.get("eligible_card_ids", []))
    if expected_eligible_count is not None and int(expected_eligible_count) != len(eligible_ids):
        raise OperationFailedWithResult("eligible_count_changed_since_dry_run", {
            **preview,
            "errors": ["eligible_count_changed_since_dry_run"],
            "expected_eligible_count": int(expected_eligible_count),
            "current_eligible_count": len(eligible_ids),
        })

    if expected_eligible_card_ids:
        expected = sorted(int(cid) for cid in expected_eligible_card_ids)
        current = sorted(int(cid) for cid in eligible_ids)
        if expected != current:
            raise OperationFailedWithResult("eligible_card_ids_changed_since_dry_run", {
                **preview,
                "errors": ["eligible_card_ids_changed_since_dry_run"],
                "expected_eligible_card_ids": expected,
                "current_eligible_card_ids": current,
            })

    new_cids = [
        int(card["new_cid"])
        for item in preview["proposed_order"]
        if item.get("eligible")
        for card in item.get("cards", [])
        if card.get("new_cid") is not None
    ]
    if len(new_cids) != len(set(new_cids)):
        raise ValueError("planned_new_cid_collision")

    new_nids = [
        int(item["new_nid"])
        for item in preview["proposed_order"]
        if item.get("eligible")
    ]
    if len(new_nids) != len(set(new_nids)):
        raise ValueError("planned_new_nid_collision")

    old_cids = {
        int(card["old_cid"])
        for item in preview["proposed_order"]
        if item.get("eligible")
        for card in item.get("cards", [])
    }
    old_nids = {int(item["old_nid"]) for item in preview["proposed_order"] if item.get("eligible")}
    collisions = []

    for item in preview["proposed_order"]:
        if not item.get("eligible"):
            continue
        old_nid = int(item["old_nid"])
        new_nid = int(item["new_nid"])
        expected_old_cids = [int(card["old_cid"]) for card in item.get("cards", [])]
        current_note = mw.col.db.scalar("select id from notes where id = ?", old_nid)
        if current_note is None:
            collisions.append({
                "old_nid": old_nid,
                "new_nid": new_nid,
                "reason": "old_nid_missing",
            })
            continue

        current_cards = mw.col.db.all(
            """
            select id, type, queue
            from cards
            where nid = ?
            order by ord, id
            """,
            old_nid,
        )
        current_card_ids = [int(row[0]) for row in current_cards]
        if current_card_ids != expected_old_cids:
            collisions.append({
                "old_nid": old_nid,
                "new_nid": new_nid,
                "reason": "note_card_set_changed_since_preview",
                "expected_card_ids": expected_old_cids,
                "current_card_ids": current_card_ids,
            })
            continue
        non_new_cards = [int(row[0]) for row in current_cards if int(row[1]) != 0]
        if non_new_cards:
            collisions.append({
                "old_nid": old_nid,
                "new_nid": new_nid,
                "reason": "multi_card_note_has_non_new_cards",
                "card_ids": non_new_cards,
            })
            continue

        if new_nid != old_nid:
            existing_note = mw.col.db.scalar("select id from notes where id = ?", new_nid)
            if existing_note is not None:
                collisions.append({
                    "old_nid": old_nid,
                    "new_nid": new_nid,
                    "reason": "notes_id_exists",
                })
            if new_nid in old_nids:
                collisions.append({
                    "old_nid": old_nid,
                    "new_nid": new_nid,
                    "reason": "planned_nid_matches_existing_old_nid",
                })

        for card in item.get("cards", []):
            old_cid = int(card["old_cid"])
            new_cid = int(card["new_cid"])
            if new_cid == old_cid:
                continue
            if is_used_card_id(new_cid, ignored_card_ids={old_cid}):
                collisions.append({
                    "old_cid": old_cid,
                    "new_cid": new_cid,
                    "old_nid": old_nid,
                    "new_nid": new_nid,
                    "reason": "cards_id_exists",
                })
            if new_cid in old_cids and new_cid != old_cid:
                collisions.append({
                    "old_cid": old_cid,
                    "new_cid": new_cid,
                    "old_nid": old_nid,
                    "new_nid": new_nid,
                    "reason": "planned_cid_matches_existing_eligible_old_cid",
                })
            if is_used_revlog_cid(new_cid):
                collisions.append({
                    "old_cid": old_cid,
                    "new_cid": new_cid,
                    "old_nid": old_nid,
                    "new_nid": new_nid,
                    "reason": "revlog_cid_exists",
                })

    if collisions:
        raise OperationFailedWithResult("planned_new_cid_collision", {
            **preview,
            "errors": ["planned_new_cid_collision"],
            "collisions": collisions,
        })


def assert_reorder_apply_safe(preview: dict, expected_eligible_count=None, expected_eligible_card_ids=None) -> None:
    if preview.get("target_created_column") == "note" and preview.get("ordering_unit") == "note":
        assert_reorder_note_group_apply_safe(preview, expected_eligible_count, expected_eligible_card_ids)
        return

    eligible_ids = list(preview.get("eligible_card_ids", []))
    target_created_column = preview.get("target_created_column", "card")
    if expected_eligible_count is not None and int(expected_eligible_count) != len(eligible_ids):
        raise OperationFailedWithResult("eligible_count_changed_since_dry_run", {
            **preview,
            "errors": ["eligible_count_changed_since_dry_run"],
            "expected_eligible_count": int(expected_eligible_count),
            "current_eligible_count": len(eligible_ids),
        })

    if expected_eligible_card_ids:
        expected = sorted(int(cid) for cid in expected_eligible_card_ids)
        current = sorted(int(cid) for cid in eligible_ids)
        if expected != current:
            raise OperationFailedWithResult("eligible_card_ids_changed_since_dry_run", {
                **preview,
                "errors": ["eligible_card_ids_changed_since_dry_run"],
                "expected_eligible_card_ids": expected,
                "current_eligible_card_ids": current,
            })

    new_cids = [int(item["new_cid"]) for item in preview["proposed_order"] if item.get("eligible")]
    if len(new_cids) != len(set(new_cids)):
        raise ValueError("planned_new_cid_collision")

    old_cids = {int(item["cid"]) for item in preview["proposed_order"] if item.get("eligible")}
    old_nids = {int(item["old_nid"]) for item in preview["proposed_order"] if item.get("eligible")}
    new_nids = [
        int(item["new_nid"])
        for item in preview["proposed_order"]
        if item.get("eligible") and target_created_column == "note"
    ]
    if len(new_nids) != len(set(new_nids)):
        raise ValueError("planned_new_nid_collision")

    collisions = []
    for item in preview["proposed_order"]:
        if not item.get("eligible"):
            continue
        old_cid = int(item["cid"])
        new_cid = int(item["new_cid"])
        old_nid = int(item["old_nid"])
        new_nid = int(item["new_nid"])
        current_row = mw.col.db.first("select id, type, queue from cards where id = ?", old_cid)
        if current_row is None:
            collisions.append({
                "old_cid": old_cid,
                "new_cid": new_cid,
                "reason": "old_cid_missing",
            })
            continue
        _current_id, current_type, _current_queue = current_row
        if int(current_type) != 0:
            collisions.append({
                "old_cid": old_cid,
                "new_cid": new_cid,
                "reason": "card_no_longer_new",
            })
            continue
        if target_created_column == "note":
            note_row = mw.col.db.first("select id from notes where id = ?", old_nid)
            if note_row is None:
                collisions.append({
                    "old_cid": old_cid,
                    "new_cid": new_cid,
                    "old_nid": old_nid,
                    "new_nid": new_nid,
                    "reason": "old_nid_missing",
                })
                continue
            note_card_ids = [
                int(cid)
                for cid in mw.col.db.list(
                    """
                    select id
                    from cards
                    where nid = ?
                    order by ord, id
                    """,
                    old_nid,
                )
            ]
            if note_card_ids != [old_cid]:
                collisions.append({
                    "old_cid": old_cid,
                    "new_cid": new_cid,
                    "old_nid": old_nid,
                    "new_nid": new_nid,
                    "reason": "note_has_multiple_cards_requires_policy",
                    "note_card_ids": note_card_ids,
                })
                continue
            if new_nid != old_nid:
                existing_note = mw.col.db.scalar("select id from notes where id = ?", new_nid)
                if existing_note is not None:
                    collisions.append({
                        "old_cid": old_cid,
                        "new_cid": new_cid,
                        "old_nid": old_nid,
                        "new_nid": new_nid,
                        "reason": "notes_id_exists",
                    })
                if new_nid in old_nids:
                    collisions.append({
                        "old_cid": old_cid,
                        "new_cid": new_cid,
                        "old_nid": old_nid,
                        "new_nid": new_nid,
                        "reason": "planned_nid_matches_existing_old_nid",
                    })
        if new_cid == old_cid:
            continue
        if is_used_card_id(new_cid, ignored_card_ids={old_cid}):
            collisions.append({
                "old_cid": old_cid,
                "new_cid": new_cid,
                "reason": "cards_id_exists",
            })
        if new_cid in old_cids and new_cid != old_cid:
            collisions.append({
                "old_cid": old_cid,
                "new_cid": new_cid,
                "reason": "planned_cid_matches_existing_eligible_old_cid",
            })
        if is_used_revlog_cid(new_cid):
            collisions.append({
                "old_cid": old_cid,
                "new_cid": new_cid,
                "reason": "revlog_cid_exists",
            })

    if collisions:
        raise OperationFailedWithResult("planned_new_cid_collision", {
            **preview,
            "errors": ["planned_new_cid_collision"],
            "collisions": collisions,
        })


def reorder_log_entry(
    *,
    preview: dict,
    item: dict,
    status: str,
    dry_run: bool,
    backup_path: str = "",
    error: str = "",
) -> dict:
    target_created_column = preview.get("target_created_column", "card")
    old_created = (
        item.get("note_created_current")
        if target_created_column == "note"
        else item.get("created_current")
    )
    return {
        "event_type": "reorder_created",
        "status": status,
        "batch_id": preview.get("batch_id", ""),
        "operation_id": preview.get("operation_id", ""),
        "operation_type": "reorder_cards_by_material",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "deck": preview.get("deck", ""),
        "dry_run": bool(dry_run),
        "target_created_column": target_created_column,
        "multi_card_note_policy": preview.get("multi_card_note_policy"),
        "requested_base_datetime": preview.get("requested_base_datetime"),
        "effective_base_datetime": preview.get("effective_base_datetime"),
        "auto_shifted_base_datetime": preview.get("auto_shifted_base_datetime"),
        "collision_avoidance_reason": preview.get("collision_avoidance_reason"),
        "spacing_ms": preview.get("spacing_ms"),
        "card_id": item.get("new_cid") if not dry_run and item.get("new_cid") is not None else item.get("cid"),
        "note_id": item.get("nid"),
        "old_cid": item.get("cid"),
        "new_cid": item.get("new_cid"),
        "old_nid": item.get("old_nid", item.get("nid")),
        "new_nid": item.get("new_nid", item.get("nid")),
        "note_card_count": item.get("note_card_count"),
        "note_card_ids": item.get("note_card_ids"),
        "card_ord": item.get("card_ord", item.get("ord")),
        "rowcount_notes": item.get("rowcount_notes"),
        "rowcount_cards": item.get("rowcount_cards"),
        "old_created": old_created,
        "new_created": item.get("created_new"),
        "card_state": item.get("card_state"),
        "warnings": item.get("warnings", []),
        "backup_path": backup_path,
        "revlog_cid_updated": False,
        "error": error,
    }


def reorder_group_card_items(preview_item: dict) -> list[dict]:
    if preview_item.get("unit") != "note":
        return [preview_item]
    items = []
    for card in preview_item.get("cards", []):
        item = {
            **card,
            "nid": preview_item.get("old_nid"),
            "old_nid": preview_item.get("old_nid"),
            "new_nid": preview_item.get("new_nid"),
            "note_created_current": preview_item.get("note_created_current"),
            "old_created": preview_item.get("old_created"),
            "summary": preview_item.get("summary", ""),
            "note_type": preview_item.get("note_type", ""),
            "template_name": preview_item.get("template_name", ""),
            "note_card_count": preview_item.get("note_card_count"),
            "note_card_ids": preview_item.get("note_card_ids"),
            "target_created_column": preview_item.get("target_created_column"),
            "multi_card_note_policy": preview_item.get("multi_card_note_policy"),
        }
        items.append(item)
    return items


def log_reorder_preview(preview: dict) -> None:
    for item in preview.get("proposed_order", []):
        for log_item in reorder_group_card_items(item):
            entry = reorder_log_entry(
                preview=preview,
                item=log_item,
                status="preview" if item.get("eligible") and log_item.get("eligible", True) else "skipped",
                dry_run=True,
            )
            append_reorganization_log(entry)
            log(
                "organization reorder dry_run entry "
                + json.dumps(entry, ensure_ascii=False, sort_keys=True)
            )
    log(
        "organization reorder dry_run preview "
        f"batch_id={preview.get('batch_id')} deck={preview.get('deck')} "
        f"total_cards={preview.get('total_cards')} eligible={preview.get('total_eligible_new')} "
        f"not_eligible={preview.get('total_not_eligible')} "
        f"notes_reordered={preview.get('total_notes_reordered')} cards_reordered={preview.get('total_cards_reordered')} "
        f"notes_skipped={preview.get('total_notes_skipped')} cards_skipped={preview.get('total_cards_skipped')} "
        f"skipped_by_reason={json.dumps(preview.get('skipped_by_reason', {}), ensure_ascii=False, sort_keys=True)}"
    )


def short_reorder_sort_field(value: str, max_chars: int = 100) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(value) > max_chars:
        return value[: max_chars - 3].rstrip() + "..."
    return value


def reorder_item_card_count(item: dict) -> int:
    if item.get("unit") == "note":
        return len(item.get("cards", []))
    return 1 if item.get("cid") is not None else 0


def reorder_item_cids(item: dict) -> list[int]:
    if item.get("unit") == "note":
        return [int(card["old_cid"]) for card in item.get("cards", []) if card.get("old_cid") is not None]
    return [int(item["cid"])] if item.get("cid") is not None else []


def reorder_item_card_states(item: dict) -> list[dict]:
    if item.get("unit") == "note":
        return [
            {
                "cid": int(card["old_cid"]),
                "type": int(card.get("type", -999)),
                "queue": int(card.get("queue", -999)),
                "state": card.get("card_state", ""),
            }
            for card in item.get("cards", [])
            if card.get("old_cid") is not None
        ]
    return [{
        "cid": int(item["cid"]),
        "type": int(item.get("type", -999)),
        "queue": int(item.get("queue", -999)),
        "state": item.get("card_state", ""),
    }] if item.get("cid") is not None else []


def reorder_skip_reason(item: dict) -> str:
    warnings = set(item.get("warnings", []))
    if item.get("eligible"):
        return ""
    if "multi_card_note_has_non_new_cards" in warnings:
        return "multi_card_note_has_non_new_cards"
    if "skipped_not_new" in warnings:
        return "card_not_currently_new"
    if "multi_card_note_cards_outside_scope" in warnings:
        return "note_not_in_deck"
    if "note_has_multiple_cards_requires_policy" in warnings:
        return "other"
    if "note_has_no_cards" in warnings:
        return "missing_note_or_card"
    if "possible_duplicate_position" in warnings:
        return "collision"
    return "other"


def reorder_diagnostic_summary(preview: dict) -> dict:
    items = list(preview.get("proposed_order", []))
    total_notes_reordered = 0
    total_cards_reordered = 0
    total_notes_skipped = 0
    total_cards_skipped = 0
    skipped_by_reason = {}
    skipped_examples = []

    for item in items:
        note_count = 1
        card_count = reorder_item_card_count(item)
        if item.get("eligible"):
            total_notes_reordered += note_count
            total_cards_reordered += card_count
            continue

        reason = reorder_skip_reason(item)
        total_notes_skipped += note_count
        total_cards_skipped += card_count
        entry = skipped_by_reason.setdefault(reason, {"notes": 0, "cards": 0})
        entry["notes"] += note_count
        entry["cards"] += card_count
        if len(skipped_examples) < 20:
            skipped_examples.append({
                "nid": item.get("old_nid", item.get("nid")),
                "cids": reorder_item_cids(item),
                "card_states": reorder_item_card_states(item),
                "reason": reason,
                "sort_field": short_reorder_sort_field(item.get("summary", "")),
            })

    return {
        "total_notes_in_deck": preview.get("total_notes"),
        "total_cards_in_deck": preview.get("total_cards"),
        "total_notes_reordered": total_notes_reordered,
        "total_cards_reordered": total_cards_reordered,
        "total_notes_skipped": total_notes_skipped,
        "total_cards_skipped": total_cards_skipped,
        "skipped_by_reason": skipped_by_reason,
        "skipped_examples": skipped_examples,
    }


def attach_reorder_diagnostic_summary(preview: dict) -> dict:
    preview.update(reorder_diagnostic_summary(preview))
    return preview


def post_apply_reorder_created_audit(preview: dict, skipped_items: list[dict]) -> dict:
    if preview.get("target_created_column") != "note":
        return {
            "notes_still_with_created_outside_new_block": 0,
            "examples": [],
            "note": "post_apply_created_audit_only_for_target_created_column_note",
        }

    applied_nids = {
        int(item["new_nid"])
        for item in preview.get("proposed_order", [])
        if item.get("eligible") and item.get("new_nid") is not None
    }
    skipped_by_nid = {
        int(item.get("old_nid", item.get("nid"))): item
        for item in skipped_items
        if item.get("old_nid", item.get("nid")) is not None
    }
    current_cards = deck_reorder_cards(preview.get("deck", ""))
    groups = {}
    for card in current_cards:
        nid = int(card["nid"])
        group = groups.setdefault(nid, {
            "nid": nid,
            "cids": [],
            "card_states": [],
            "summary": card.get("summary", ""),
        })
        group["cids"].append(int(card["cid"]))
        group["card_states"].append({
            "cid": int(card["cid"]),
            "type": int(card["type"]),
            "queue": int(card["queue"]),
            "state": card_state_label(int(card["type"]), int(card["queue"])),
        })

    examples = []
    outside_count = 0
    for nid, group in sorted(groups.items()):
        if nid in applied_nids:
            continue
        outside_count += 1
        skipped_item = skipped_by_nid.get(nid)
        reason = reorder_skip_reason(skipped_item) if skipped_item else "other"
        if len(examples) < 20:
            examples.append({
                "nid": nid,
                "cids": sorted(group["cids"]),
                "card_states": group["card_states"],
                "reason": reason,
                "sort_field": short_reorder_sort_field(group.get("summary", "")),
                "created": datetime_string_from_cid(nid),
            })

    return {
        "notes_still_with_created_outside_new_block": outside_count,
        "examples": examples,
        "reason_hint": (
            "Notes listed here kept their old Browser Created value because they were not reordered. "
            "Most commonly, at least one card in the note was not currently New."
        ),
    }


def sqlite_changes_count() -> int:
    try:
        return int(mw.col.db.scalar("select changes()") or 0)
    except Exception as e:
        log(f"organization reorder changes() warning {type(e).__name__}: {e}")
        return -1


def raise_reorder_rowcount_zero(preview: dict, detail: dict) -> None:
    result = {
        **preview,
        "errors": ["reorder_update_rowcount_zero"],
        "rowcount_failure": detail,
    }
    raise OperationFailedWithResult("reorder_update_rowcount_zero", result)


def mark_reorder_collection_modified() -> None:
    for attr in ("set_schema_modified", "set_schema_mod"):
        func = getattr(mw.col, attr, None)
        if callable(func):
            try:
                func()
                log(f"organization reorder marked collection modified via {attr}")
                return
            except Exception as e:
                log(f"organization reorder set schema modified warning {attr} {type(e).__name__}: {e}")


def flush_reorder_collection() -> None:
    save_collection()
    try:
        mw.col.db.execute("pragma wal_checkpoint(full)")
    except Exception as e:
        log(f"organization reorder checkpoint warning {type(e).__name__}: {e}")


def expected_reorder_changes(preview: dict) -> list[dict]:
    changes = []
    for item in preview.get("proposed_order", []):
        if not item.get("eligible"):
            continue
        old_nid = int(item.get("old_nid", item.get("nid")))
        new_nid = int(item.get("new_nid", old_nid))
        for card_item in reorder_group_card_items(item):
            old_cid = int(card_item.get("old_cid", card_item.get("cid")))
            new_cid = int(card_item.get("new_cid", old_cid))
            changes.append({
                "old_nid": old_nid,
                "new_nid": new_nid,
                "old_cid": old_cid,
                "new_cid": new_cid,
            })
    return changes


def verify_reorder_post_apply(preview: dict) -> dict:
    target_created_column = preview.get("target_created_column", "card")
    changes = expected_reorder_changes(preview)
    missing_new_nids = []
    missing_new_cids = []
    nid_mismatches = []
    lingering_old_nids = []
    lingering_old_cids = []

    for change in changes:
        old_nid = int(change["old_nid"])
        new_nid = int(change["new_nid"])
        old_cid = int(change["old_cid"])
        new_cid = int(change["new_cid"])
        if target_created_column == "note":
            if mw.col.db.scalar("select id from notes where id = ?", new_nid) is None:
                missing_new_nids.append({"old_nid": old_nid, "new_nid": new_nid})
            current_nid = mw.col.db.scalar("select nid from cards where id = ?", new_cid)
            if current_nid is None:
                missing_new_cids.append({"old_cid": old_cid, "new_cid": new_cid, "new_nid": new_nid})
            elif int(current_nid) != new_nid:
                nid_mismatches.append({
                    "new_cid": new_cid,
                    "expected_nid": new_nid,
                    "current_nid": int(current_nid),
                })
            if old_nid != new_nid and mw.col.db.scalar("select id from notes where id = ?", old_nid) is not None:
                lingering_old_nids.append({"old_nid": old_nid, "new_nid": new_nid})
        else:
            if mw.col.db.scalar("select id from cards where id = ?", new_cid) is None:
                missing_new_cids.append({"old_cid": old_cid, "new_cid": new_cid})
        if old_cid != new_cid and mw.col.db.scalar("select id from cards where id = ?", old_cid) is not None:
            lingering_old_cids.append({"old_cid": old_cid, "new_cid": new_cid})

    failures = {
        "missing_new_nids": missing_new_nids[:20],
        "missing_new_cids": missing_new_cids[:20],
        "nid_mismatches": nid_mismatches[:20],
        "lingering_old_nids": lingering_old_nids[:20],
        "lingering_old_cids": lingering_old_cids[:20],
    }
    failed = any(failures.values())
    verification = {
        "ok": not failed,
        "planned_count": len(changes),
        "verified_new_notes_count": len({change["new_nid"] for change in changes}) if target_created_column == "note" else 0,
        "verified_new_cards_count": len({change["new_cid"] for change in changes}),
        **failures,
    }
    if failed:
        result = {
            **preview,
            "errors": ["reorder_post_apply_verification_failed"],
            "post_apply_verification": verification,
        }
        raise OperationFailedWithResult("reorder_post_apply_verification_failed", result)
    return verification


def reorder_cards_by_material(
    deck: str,
    dry_run: bool = True,
    scope: str = "currently_new_cards",
    apply_created_date: bool = True,
    target_created_column: str = "card",
    apply_note_created_date: bool = False,
    multi_card_note_policy=None,
    base_datetime=None,
    spacing_ms: int = 60000,
    ordered_card_ids=None,
    ordered_note_ids=None,
    expected_eligible_count=None,
    expected_eligible_card_ids=None,
    operation_id: str = "",
    batch_id: str = "",
) -> dict:
    dry_run = normalize_bool(dry_run, True)
    apply_created_date = normalize_bool(apply_created_date, True)
    apply_note_created_date = normalize_bool(apply_note_created_date, False)
    target_created_column = normalize_target_created_column(
        target_created_column,
        apply_note_created_date=apply_note_created_date,
    )
    multi_card_note_policy = normalize_multi_card_note_policy(
        multi_card_note_policy,
        target_created_column=target_created_column,
    )
    if scope != "currently_new_cards":
        raise ValueError("unsupported_scope")
    if not apply_created_date:
        raise ValueError("apply_created_date_false_not_supported")
    if spacing_ms is None:
        spacing_ms = 60000

    preview = build_reorder_preview(
        deck=deck,
        dry_run=dry_run,
        base_datetime=base_datetime,
        spacing_ms=spacing_ms,
        target_created_column=target_created_column,
        multi_card_note_policy=multi_card_note_policy,
        ordered_card_ids=ordered_card_ids,
        ordered_note_ids=ordered_note_ids,
        operation_id=operation_id,
        batch_id=batch_id,
    )
    attach_reorder_diagnostic_summary(preview)

    if dry_run:
        log_reorder_preview(preview)
        return preview

    assert_reorder_apply_safe(preview, expected_eligible_count, expected_eligible_card_ids)
    backup = create_reorder_backup(preview["batch_id"])
    backup_path = backup["backup_path"]
    preview["backup"] = backup
    preview["preview_generated_before_apply"] = True

    applied = []
    skipped = []
    now_seconds = int(datetime.now().timestamp())
    target_created_column = preview.get("target_created_column", "card")
    planned_count = len(expected_reorder_changes(preview))
    preview["planned_count"] = planned_count
    log(
        "organization reorder apply starting "
        f"batch_id={preview.get('batch_id')} deck={preview.get('deck')} "
        f"target_created_column={target_created_column} planned_count={planned_count}"
    )
    if target_created_column == "note":
        transaction_started = False
        try:
            mw.col.db.execute("begin")
            transaction_started = True
            for item in preview["proposed_order"]:
                if not item.get("eligible"):
                    skipped.append(item)
                    continue
                old_nid = int(item["old_nid"])
                new_nid = int(item["new_nid"])
                card_items = reorder_group_card_items(item)
                for card_item in card_items:
                    old_cid = int(card_item["old_cid"])
                    current_row = mw.col.db.first("select id, type, queue from cards where id = ?", old_cid)
                    if current_row is None:
                        raise RuntimeError(f"card_missing_before_reorder: {old_cid}")
                    _, current_type, _current_queue = current_row
                    if int(current_type) != 0:
                        raise RuntimeError(f"card_no_longer_new: {old_cid}")
                mw.col.db.execute(
                    """
                    update notes
                    set id = ?, mod = ?, usn = -1
                    where id = ?
                    """,
                    new_nid,
                    now_seconds,
                    old_nid,
                )
                rowcount_notes = sqlite_changes_count()
                log(
                    "organization reorder apply note update "
                    f"batch_id={preview.get('batch_id')} old_nid={old_nid} new_nid={new_nid} "
                    f"rowcount_notes={rowcount_notes}"
                )
                if rowcount_notes <= 0:
                    raise_reorder_rowcount_zero(preview, {
                        "table": "notes",
                        "old_nid": old_nid,
                        "new_nid": new_nid,
                        "rowcount_notes": rowcount_notes,
                    })
                for card_item in card_items:
                    old_cid = int(card_item["old_cid"])
                    new_cid = int(card_item["new_cid"])
                    mw.col.db.execute(
                        """
                        update cards
                        set id = ?, nid = ?, mod = ?, usn = -1
                        where id = ?
                        """,
                        new_cid,
                        new_nid,
                        now_seconds,
                        old_cid,
                    )
                    rowcount_cards = sqlite_changes_count()
                    log(
                        "organization reorder apply card update "
                        f"batch_id={preview.get('batch_id')} old_cid={old_cid} new_cid={new_cid} "
                        f"old_nid={old_nid} new_nid={new_nid} rowcount_cards={rowcount_cards}"
                    )
                    if rowcount_cards <= 0:
                        raise_reorder_rowcount_zero(preview, {
                            "table": "cards",
                            "old_cid": old_cid,
                            "new_cid": new_cid,
                            "old_nid": old_nid,
                            "new_nid": new_nid,
                            "rowcount_cards": rowcount_cards,
                        })
                    card_item["rowcount_notes"] = rowcount_notes
                    card_item["rowcount_cards"] = rowcount_cards
                    applied.append(reorder_log_entry(
                        preview=preview,
                        item=card_item,
                        status="reordered",
                        dry_run=False,
                        backup_path=backup_path,
                    ))
            mw.col.db.execute("commit")
            transaction_started = False
            mark_reorder_collection_modified()
            flush_reorder_collection()
            preview["post_apply_verification"] = verify_reorder_post_apply(preview)
        except Exception:
            if transaction_started:
                try:
                    mw.col.db.execute("rollback")
                except Exception as rollback_error:
                    log(
                        "organization reorder note rollback warning "
                        f"batch_id={preview.get('batch_id')} {type(rollback_error).__name__}: {rollback_error}"
                    )
            raise
        for entry in applied:
            append_reorganization_log(entry)
            log(
                "organization reorder apply entry "
                + json.dumps(entry, ensure_ascii=False, sort_keys=True)
            )
    else:
        transaction_started = False
        try:
            mw.col.db.execute("begin")
            transaction_started = True
            for item in preview["proposed_order"]:
                if not item.get("eligible"):
                    skipped.append(item)
                    continue
                old_cid = int(item["cid"])
                new_cid = int(item["new_cid"])
                current_row = mw.col.db.first("select id, type, queue from cards where id = ?", old_cid)
                if current_row is None:
                    raise RuntimeError(f"card_missing_before_reorder: {old_cid}")
                _, current_type, _current_queue = current_row
                if int(current_type) != 0:
                    raise RuntimeError(f"card_no_longer_new: {old_cid}")
                mw.col.db.execute(
                    """
                    update cards
                    set id = ?, mod = ?, usn = -1
                    where id = ?
                    """,
                    new_cid,
                    now_seconds,
                    old_cid,
                )
                rowcount_cards = sqlite_changes_count()
                log(
                    "organization reorder apply card update "
                    f"batch_id={preview.get('batch_id')} old_cid={old_cid} new_cid={new_cid} "
                    f"rowcount_cards={rowcount_cards}"
                )
                if rowcount_cards <= 0:
                    raise_reorder_rowcount_zero(preview, {
                        "table": "cards",
                        "old_cid": old_cid,
                        "new_cid": new_cid,
                        "rowcount_cards": rowcount_cards,
                    })
                item["rowcount_cards"] = rowcount_cards
                applied.append(reorder_log_entry(
                    preview=preview,
                    item=item,
                    status="reordered",
                    dry_run=False,
                    backup_path=backup_path,
                ))
            mw.col.db.execute("commit")
            transaction_started = False
            flush_reorder_collection()
            preview["post_apply_verification"] = verify_reorder_post_apply(preview)
        except Exception:
            if transaction_started:
                try:
                    mw.col.db.execute("rollback")
                except Exception as rollback_error:
                    log(
                        "organization reorder card rollback warning "
                        f"batch_id={preview.get('batch_id')} {type(rollback_error).__name__}: {rollback_error}"
                    )
            raise
        for entry in applied:
            append_reorganization_log(entry)
            log(
                "organization reorder apply entry "
                + json.dumps(entry, ensure_ascii=False, sort_keys=True)
            )

    preview["post_apply_created_audit"] = post_apply_reorder_created_audit(preview, skipped)
    preview["dry_run"] = False
    preview["applied_count"] = len(applied)
    preview["skipped_count"] = len(skipped)
    preview["reorder_log_path"] = str(REORGANIZATION_LOG_FILE)
    preview["reorder_log_entries"] = applied
    log(
        "organization reorder apply finished "
        f"batch_id={preview.get('batch_id')} deck={preview.get('deck')} "
        f"applied_count={len(applied)} skipped_count={len(skipped)} backup={backup_path} "
        f"notes_reordered={preview.get('total_notes_reordered')} cards_reordered={preview.get('total_cards_reordered')} "
        f"notes_skipped={preview.get('total_notes_skipped')} cards_skipped={preview.get('total_cards_skipped')} "
        f"post_apply_created_audit={json.dumps(preview.get('post_apply_created_audit', {}), ensure_ascii=False, sort_keys=True)}"
    )
    return preview


def move_cards_to_deck(
    card_ids: list[int],
    target_deck: str,
    dry_run: bool = True,
    add_tags=None,
    respect_ideal_deck: bool = True,
    force: bool = False,
    mark_ideal: bool = False,
    operation_id: str = "",
    batch_id: str = "",
    operation_type: str = "move_cards_to_deck",
) -> dict:
    card_ids = normalize_int_ids(card_ids, "card_ids")
    tags = normalize_tags(add_tags)
    dry_run = True if dry_run is None else bool(dry_run)
    respect_ideal_deck = normalize_bool(respect_ideal_deck, True)
    force = normalize_bool(force, False)
    mark_ideal = normalize_bool(mark_ideal, False)
    target_deck = (target_deck or "").strip()
    if not target_deck:
        raise ValueError("target_deck_empty")

    deck_name_map = get_deck_name_map()
    cards = [get_card(card_id) for card_id in card_ids]
    found_card_ids = [int(card_attr(card, "id")) for card in cards]
    missing_card_ids = [card_id for card_id in card_ids if card_id not in set(found_card_ids)]
    if missing_card_ids:
        raise ValueError(f"cards_not_found: {missing_card_ids}")

    note_cache = {}
    preview = [card_preview(card, target_deck, deck_name_map, note_cache) for card in cards]
    note_ids = sorted({item["note_id"] for item in preview})
    deck_result = ensure_deck(target_deck, dry_run=True)
    batch_id = (batch_id or operation_id or f"local-{datetime.utcnow().strftime('%Y%m%dT%H%M%S%fZ')}").strip()
    ideal_tags_to_add = ideal_deck_tags(target_deck) if mark_ideal else []

    movable_cards = []
    skipped_ideal_deck = []
    for card in cards:
        card_id = int(card_attr(card, "id"))
        note_id = int(card_attr(card, "nid"))
        note = note_cache.get(note_id)
        if note is None:
            note = get_note(note_id)
            note_cache[note_id] = note
        current_deck = deck_name_map.get(int(card_attr(card, "did")), "")
        status = note_ideal_status(note, [current_deck], note_id=note_id)
        if respect_ideal_deck and not force and status["has_ideal_deck_tag"]:
            skipped_ideal_deck.append({
                "card_id": card_id,
                "note_id": note_id,
                "current_deck": current_deck,
                "target_deck": target_deck,
                "destination_tags": status["destination_tags"],
                "expected_deck_slug": status["expected_deck_slug"],
                "parece_bater_com_deck_atual": status["parece_bater_com_deck_atual"],
                "relevant_tags": status["relevant_tags"],
                "reason": "ideal_deck_protected",
            })
        else:
            movable_cards.append(card)
    movable_card_ids = [int(card_attr(card, "id")) for card in movable_cards]
    movable_note_ids = sorted({int(card_attr(card, "nid")) for card in movable_cards})
    ideal_tag_preview = (
        replace_ideal_destination_tags(movable_note_ids, target_deck, dry_run=True)
        if mark_ideal and movable_note_ids
        else {
            "tags": ideal_tags_to_add if mark_ideal else [],
            "destination_tag_to_add": ideal_destination_tag(target_deck) if mark_ideal else None,
            "destination_tags_to_remove": [],
            "removals_by_note": {},
            "additions_by_note": {},
            "tags_to_add": [],
            "changed_note_ids": [],
            "changed_count": 0,
            "dry_run": True,
        }
    )

    result = {
        "dry_run": dry_run,
        "operation": operation_type,
        "batch_id": batch_id,
        "operation_id": operation_id,
        "card_ids": card_ids,
        "note_ids": note_ids,
        "movable_card_ids": movable_card_ids,
        "movable_note_ids": movable_note_ids,
        "target_deck": target_deck,
        "target_deck_id": deck_result.get("deck_id"),
        "created_target_deck": False if dry_run else deck_result.get("created", False),
        "would_create_target_deck": deck_result.get("would_create", False),
        "preview_count": len(preview),
        "moved_count": 0,
        "skipped_count": len(skipped_ideal_deck),
        "skipped_ideal_deck": skipped_ideal_deck,
        "respect_ideal_deck": respect_ideal_deck,
        "force": force,
        "forced": force,
        "mark_ideal": mark_ideal,
        "would_add_ideal_tags": ideal_tags_to_add if mark_ideal else [],
        "destination_tag_to_add": ideal_tag_preview.get("destination_tag_to_add"),
        "destination_tags_to_remove": ideal_tag_preview.get("destination_tags_to_remove", []),
        "preview": preview,
        "add_tags": tags,
        "tag_result": {
            "tags": tags,
            "changed_note_ids": [],
            "changed_count": 0,
        },
        "ideal_tag_result": {
            **ideal_tag_preview,
            "changed_note_ids": [],
            "changed_count": 0,
        },
        "preserved": {
            "card_id": True,
            "note_id": True,
            "scheduling": True,
            "review_history": True,
            "fields": True,
            "media": True,
        },
        "errors": [],
    }

    if dry_run:
        return result

    if not movable_cards:
        result["after"] = []
        result["ideal_tag_result"] = {
            **ideal_tag_preview,
            "changed_note_ids": [],
            "changed_count": 0,
        }
        return result

    deck_result = ensure_deck(target_deck, dry_run=False)
    result["target_deck_id"] = deck_result.get("deck_id")
    result["created_target_deck"] = deck_result.get("created", False)
    result["would_create_target_deck"] = deck_result.get("would_create", False)

    before_by_card_id = {}
    for card in movable_cards:
        card_id = int(card_attr(card, "id"))
        note = note_cache.get(int(card_attr(card, "nid"))) or get_note(int(card_attr(card, "nid")))
        before_by_card_id[card_id] = {
            "card": card,
            "old_deck_id": int(card_attr(card, "did")),
            "old_deck": deck_name_map.get(int(card_attr(card, "did")), ""),
            "template_name": template_name_for_card(card, note),
            "note_type": note_type_name(note),
            "scheduling": scheduling_snapshot(card),
        }

    update_cards_deck(movable_cards, int(deck_result["deck_id"]))
    result["tag_result"] = add_tags_to_notes(movable_note_ids, tags)
    result["ideal_tag_result"] = replace_ideal_destination_tags(movable_note_ids, target_deck, dry_run=False) if mark_ideal else {
        "tags": [],
        "destination_tag_to_add": None,
        "destination_tags_to_remove": [],
        "removals_by_note": {},
        "additions_by_note": {},
        "tags_to_add": [],
        "changed_note_ids": [],
        "changed_count": 0,
        "dry_run": False,
    }
    save_collection()

    deck_name_map_after = get_deck_name_map()
    moved_cards = [get_card(card_id) for card_id in movable_card_ids]
    result["after"] = [
        card_preview(card, target_deck, deck_name_map_after, {})
        for card in moved_cards
    ]
    result["moved_count"] = sum(
        1 for item in result["after"]
        if item.get("current_deck") == target_deck
    )
    changed_note_ids = set(result["tag_result"].get("changed_note_ids", []))
    ideal_additions_by_note = result["ideal_tag_result"].get("additions_by_note", {})
    result["move_log_path"] = str(REORGANIZATION_LOG_FILE)
    result["move_log_entries"] = []
    target_deck_id = int(deck_result["deck_id"])
    after_by_card_id = {int(card_attr(card, "id")): card for card in moved_cards}
    for card_id in movable_card_ids:
        before = before_by_card_id[card_id]
        after_card = after_by_card_id[card_id]
        after_deck_id = int(card_attr(after_card, "did"))
        status = "moved" if after_deck_id == target_deck_id else "failed"
        error = "" if status == "moved" else "card_not_in_target_deck_after_move"
        entry = move_log_entry(
            batch_id=batch_id,
            operation_id=operation_id,
            operation_type=operation_type,
            card_before=before["card"],
            card_after=after_card,
            old_deck=before["old_deck"],
            old_deck_id=before["old_deck_id"],
            new_deck=target_deck,
            new_deck_id=target_deck_id,
            template_name=before["template_name"],
            note_type=before["note_type"],
            tags_added=(
                (tags if int(card_attr(after_card, "nid")) in changed_note_ids else [])
                + ideal_additions_by_note.get(str(int(card_attr(after_card, "nid"))), [])
            ),
            scheduling_before=before["scheduling"],
            status=status,
            error=error,
        )
        append_reorganization_log(entry)
        result["move_log_entries"].append(entry)
        log(
            "organization move log entry "
            f"batch_id={batch_id} operation_id={operation_id} "
            f"card_id={card_id} status={status}"
        )

    if result["moved_count"] != len(movable_card_ids):
        raise RuntimeError(
            f"move_incomplete: moved={result['moved_count']} expected={len(movable_card_ids)}"
        )

    return result


def move_notes_to_deck(
    note_ids: list[int],
    target_deck: str,
    dry_run: bool = True,
    add_tags=None,
    respect_ideal_deck: bool = True,
    force: bool = False,
    mark_ideal: bool = False,
    operation_id: str = "",
    batch_id: str = "",
) -> dict:
    note_ids = normalize_int_ids(note_ids, "note_ids")
    cards = []
    missing_note_ids = []
    for note_id in note_ids:
        try:
            get_note(note_id)
            cards.extend(note_cards(note_id))
        except ValueError:
            missing_note_ids.append(note_id)

    if missing_note_ids:
        raise ValueError(f"notes_not_found: {missing_note_ids}")
    if not cards:
        raise ValueError("notes_resolved_to_zero_cards")

    card_ids = [int(card_attr(card, "id")) for card in cards]
    result = move_cards_to_deck(
        card_ids=card_ids,
        target_deck=target_deck,
        dry_run=dry_run,
        add_tags=add_tags,
        respect_ideal_deck=respect_ideal_deck,
        force=force,
        mark_ideal=mark_ideal,
        operation_id=operation_id,
        batch_id=batch_id,
        operation_type="move_notes_to_deck",
    )
    result["operation"] = "move_notes_to_deck"
    result["requested_note_ids"] = note_ids
    return result


def execute_organization_operation(operation: dict) -> dict:
    operation_id = operation.get("operation_id", "")
    operation_type = operation.get("operation_type")
    payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
    try:
        execution_mode = operation_execution_mode(operation)
    except ValueError as e:
        return {
            "ok": False,
            "operation_id": operation_id,
            "operation_type": operation_type,
            "execution_mode": None,
            "status": "failed",
            "addon_profile": mw.pm.name,
            "result": None,
            "errors": [str(e)],
        }
    dry_run = execution_mode == "preview"
    payload = dict(payload)
    payload["execution_mode"] = execution_mode
    payload["dry_run"] = dry_run

    log(
        "organization execute dispatcher "
        f"module_file={__file__} operation_id={operation_id} "
        f"operation_type={operation_type} execution_mode={execution_mode} "
        f"dry_run={dry_run}"
    )

    if operation.get("status") != "pending":
        log(
            "organization execute skipped "
            f"operation_id={operation_id} operation_type={operation_type} "
            f"status=skipped reason=operation_not_pending current_status={operation.get('status')}"
        )
        return {
            "ok": False,
            "operation_id": operation_id,
            "operation_type": operation_type,
            "execution_mode": execution_mode,
            "status": "skipped",
            "addon_profile": mw.pm.name,
            "result": None,
            "errors": [f"operation_not_pending: {operation.get('status')}"],
        }

    if operation_type not in ORGANIZATION_OPERATION_TYPES:
        log(
            "organization execute failed "
            f"operation_id={operation_id} operation_type={operation_type} "
            "status=failed reason=unsupported_operation_type"
        )
        return {
            "ok": False,
            "operation_id": operation_id,
            "operation_type": operation_type,
            "execution_mode": execution_mode,
            "status": "failed",
            "addon_profile": mw.pm.name,
            "result": None,
            "errors": [f"unsupported_operation_type: {operation_type}"],
        }

    try:
        if not isinstance(payload, dict):
            raise ValueError("invalid_payload")

        if operation_type == "create_deck":
            result = create_deck(payload.get("deck_name"))
        elif operation_type == "move_cards_to_deck":
            result = move_cards_to_deck(
                card_ids=payload.get("card_ids"),
                target_deck=payload.get("target_deck"),
                dry_run=dry_run,
                add_tags=payload.get("add_tags", []),
                respect_ideal_deck=payload.get("respect_ideal_deck", True),
                force=payload.get("force", False),
                mark_ideal=payload.get("mark_ideal", False),
                operation_id=operation_id,
                batch_id=operation_id,
                operation_type=operation_type,
            )
        elif operation_type == "move_notes_to_deck":
            result = move_notes_to_deck(
                note_ids=payload.get("note_ids"),
                target_deck=payload.get("target_deck"),
                dry_run=dry_run,
                add_tags=payload.get("add_tags", []),
                respect_ideal_deck=payload.get("respect_ideal_deck", True),
                force=payload.get("force", False),
                mark_ideal=payload.get("mark_ideal", False),
                operation_id=operation_id,
                batch_id=operation_id,
            )
        elif operation_type == "get_reorganization_log":
            result = get_reorganization_log(limit=payload.get("limit", 20))
        elif operation_type == "undo_reorganization":
            result = undo_reorganization(batch_id=payload.get("batch_id", ""))
        elif operation_type == "undo_last_reorganization":
            result = undo_last_reorganization()
        elif operation_type == "mark_notes_as_ideal_deck":
            result = mark_notes_as_ideal_deck(
                note_ids=payload.get("note_ids"),
                deck_name=payload.get("deck_name"),
            )
        elif operation_type == "mark_cards_as_ideal_deck":
            result = mark_cards_as_ideal_deck(
                card_ids=payload.get("card_ids"),
                deck_name=payload.get("deck_name"),
            )
        elif operation_type == "check_ideal_deck_status":
            result = check_ideal_deck_status(
                card_ids=payload.get("card_ids", []),
                note_ids=payload.get("note_ids", []),
            )
        elif operation_type == "list_note_types":
            result = list_note_types()
        elif operation_type == "get_note_type_fields":
            result = get_note_type_fields(model_name=payload.get("model_name"))
        elif operation_type == "create_note":
            result = create_note(
                deck_name=payload.get("deck_name"),
                model_name=payload.get("model_name"),
                fields=payload.get("fields"),
                tags=payload.get("tags"),
                allow_duplicate=payload.get("allow_duplicate", False),
                dry_run=dry_run,
            )
        elif operation_type == "create_notes":
            result = create_notes(
                notes=payload.get("notes"),
                dry_run=dry_run,
            )
        elif operation_type == "replace_note_tags":
            result = replace_note_tags(
                note_ids=payload.get("note_ids"),
                remove_tags=payload.get("remove_tags", []),
                add_tags=payload.get("add_tags", []),
                dry_run=dry_run,
            )
        elif operation_type == "update_note_fields":
            update_payload = hydrate_update_note_fields_payload_from_updates_id(payload)
            operation_schema_version = int(operation.get("operation_schema_version") or 1)
            validate_operation_age(
                operation,
                operation_schema_version,
                dry_run=dry_run,
            )
            result = update_note_fields(
                note_updates=update_payload.get("note_updates"),
                dry_run=dry_run,
                requested_by=operation.get("requested_by", ""),
                reason=operation.get("reason", ""),
                require_preconditions=operation_schema_version >= 2,
            )
            if payload.get("updates_id"):
                result["updates_id"] = payload.get("updates_id")
                result["updates_sha256"] = update_payload.get("updates_sha256")
                result["note_updates_count"] = payload.get("note_updates_count")
                result["note_ids_count"] = payload.get("note_ids_count")
            if result.get("errors"):
                raise OperationFailedWithResult("update_note_fields_atomic_failure", result)
        elif operation_type == "reorder_cards_by_material":
            reorder_payload = hydrate_reorder_payload_from_order_id(payload)
            result = reorder_cards_by_material(
                deck=reorder_payload.get("deck"),
                dry_run=dry_run,
                scope=reorder_payload.get("scope", "currently_new_cards"),
                apply_created_date=reorder_payload.get("apply_created_date", True),
                target_created_column=reorder_payload.get("target_created_column", "card"),
                apply_note_created_date=reorder_payload.get("apply_note_created_date", False),
                multi_card_note_policy=reorder_payload.get("multi_card_note_policy"),
                base_datetime=reorder_payload.get("base_datetime"),
                spacing_ms=reorder_payload.get("spacing_ms", 60000),
                ordered_card_ids=reorder_payload.get("ordered_card_ids", []),
                ordered_note_ids=reorder_payload.get("ordered_note_ids", []),
                expected_eligible_count=reorder_payload.get("expected_eligible_count"),
                expected_eligible_card_ids=reorder_payload.get("expected_eligible_card_ids", []),
                operation_id=operation_id,
                batch_id=operation_id,
            )
            if payload.get("order_id"):
                result["order_id"] = payload.get("order_id")
                result["order_sha256"] = reorder_payload.get("order_sha256")
                result["ordered_note_ids_count"] = payload.get("ordered_note_ids_count")
                result["expected_eligible_card_ids_count"] = payload.get("expected_eligible_card_ids_count")
        else:
            raise ValueError(f"unsupported_operation_type: {operation_type}")

        if isinstance(result, dict):
            result["execution_mode"] = execution_mode
            result.setdefault("operation_id", operation_id)
            if not dry_run:
                result.setdefault("applied_at", now_iso())
        response = {
            "ok": True,
            "operation_id": operation_id,
            "operation_type": operation_type,
            "execution_mode": execution_mode,
            "status": "done",
            "addon_profile": mw.pm.name,
            "result": result,
            "errors": [],
        }
        log(
            "organization execute finished "
            f"operation_id={operation_id} operation_type={operation_type} "
            f"dry_run={dry_run} status=done"
        )
        return response
    except OperationFailedWithResult as e:
        failure_result = e.result
        if operation_type == "reorder_cards_by_material" and payload.get("order_id") and isinstance(failure_result, dict):
            failure_result = dict(failure_result)
            failure_result["order_id"] = payload.get("order_id")
            failure_result["order_sha256"] = payload.get("order_sha256")
            failure_result["ordered_note_ids_count"] = payload.get("ordered_note_ids_count")
            failure_result["expected_eligible_card_ids_count"] = payload.get("expected_eligible_card_ids_count")
        if operation_type == "update_note_fields" and payload.get("updates_id") and isinstance(failure_result, dict):
            failure_result = dict(failure_result)
            failure_result["updates_id"] = payload.get("updates_id")
            failure_result["updates_sha256"] = payload.get("updates_sha256")
            failure_result["note_updates_count"] = payload.get("note_updates_count")
            failure_result["note_ids_count"] = payload.get("note_ids_count")
        failure_status = "partially_applied" if (
            isinstance(failure_result, dict)
            and (
                failure_result.get("rollback_errors")
                or failure_result.get("affected_note_ids")
            )
        ) else "failed"
        response = {
            "ok": False,
            "operation_id": operation_id,
            "operation_type": operation_type,
            "execution_mode": execution_mode,
            "status": failure_status,
            "addon_profile": mw.pm.name,
            "result": failure_result,
            "errors": [str(e)],
        }
        log(
            "organization execute finished "
            f"operation_id={operation_id} operation_type={operation_type} "
            f"dry_run={dry_run} status=failed error={type(e).__name__}: {e}"
        )
        return response
    except Exception as e:
        response = {
            "ok": False,
            "operation_id": operation_id,
            "operation_type": operation_type,
            "execution_mode": execution_mode,
            "status": "failed",
            "addon_profile": mw.pm.name,
            "result": None,
            "errors": [f"{type(e).__name__}: {e}"],
        }
        log(
            "organization execute finished "
            f"operation_id={operation_id} operation_type={operation_type} "
            f"dry_run={dry_run} status=failed error={type(e).__name__}: {e}"
        )
        return response


def report_organization_operation_result(
    result: dict,
    receipt: dict | None = None,
) -> bool:
    operation_id = result.get("operation_id", "")
    payload = compact_organization_result_for_storage(result)
    receipt_metadata = receipt_confirmation_metadata(receipt)
    if receipt_metadata is not None:
        payload["receipt"] = receipt_metadata
    response = organization_api_request(
        "/organization/operations/result",
        method="POST",
        payload=payload,
    )
    if response and response.get("ok"):
        confirmed_operation = response.get("operation")
        if isinstance(confirmed_operation, dict):
            upsert_operation_index(
                confirmed_operation,
                result=result,
                status=operation_status(confirmed_operation),
                phase="confirmation_persisted",
                receipt=receipt,
            )
        log(
            f"organization result report ok operation_id={operation_id} "
            f"status={result.get('status')}"
        )
        return True

    log(
        "organization result report failed "
        f"operation_id={operation_id} status={result.get('status')}"
    )
    return False


def confirm_organization_operation(result: dict, receipt: dict | None = None) -> bool:
    """Compatibility alias for callers using the former confirmation name."""
    return report_organization_operation_result(result, receipt)


def collect_int_values(value) -> set[int]:
    values = set()
    if isinstance(value, bool):
        return values
    if isinstance(value, int):
        values.add(value)
    elif isinstance(value, list):
        for item in value:
            values.update(collect_int_values(item))
    elif isinstance(value, dict):
        for item in value.values():
            values.update(collect_int_values(item))
    return values


def organization_result_change_summary(result: dict) -> dict:
    if not isinstance(result, dict) or not result.get("ok") or result.get("status") != "done":
        return {
            "changed": False,
            "changed_note_ids": [],
            "changed_note_count": 0,
            "changed_card_count": 0,
        }

    operation_result = result.get("result")
    if not isinstance(operation_result, dict) or operation_result.get("dry_run") is True:
        return {
            "changed": False,
            "changed_note_ids": [],
            "changed_note_count": 0,
            "changed_card_count": 0,
        }

    note_ids = set()
    for key in ("changed_note_ids", "affected_note_ids", "created_note_ids", "note_ids", "movable_note_ids", "note_id"):
        note_ids.update(collect_int_values(operation_result.get(key)))

    for entry in operation_result.get("move_log_entries", []):
        if isinstance(entry, dict):
            note_ids.update(collect_int_values(entry.get("note_id")))
    for entry in operation_result.get("reorder_log_entries", []):
        if isinstance(entry, dict):
            note_ids.update(collect_int_values(entry.get("old_nid")))
            note_ids.update(collect_int_values(entry.get("new_nid")))

    card_count = 0
    for key in (
        "moved_count",
        "restored_count",
        "applied_count",
        "created_card_ids",
        "card_ids",
        "movable_card_ids",
        "card_id",
    ):
        value = operation_result.get(key)
        if key.endswith("_ids") or key == "card_ids":
            card_count += len(collect_int_values(value))
        elif isinstance(value, int) and not isinstance(value, bool):
            card_count += max(value, 0)

    scalar_change = any(
        bool(operation_result.get(key))
        for key in ("created", "created_deck", "created_target_deck", "undone")
    )
    count_change = any(
        isinstance(operation_result.get(key), int)
        and not isinstance(operation_result.get(key), bool)
        and operation_result.get(key) > 0
        for key in ("changed_count", "created_count", "moved_count", "restored_count", "applied_count")
    )
    nested_change = False
    for key in ("tag_result", "ideal_tag_result"):
        nested = operation_result.get(key)
        if isinstance(nested, dict) and int(nested.get("changed_count") or 0) > 0:
            nested_change = True
            note_ids.update(collect_int_values(nested.get("changed_note_ids")))

    changed = bool(note_ids or scalar_change or count_change or nested_change)
    return {
        "changed": changed,
        "changed_note_ids": sorted(note_ids),
        "changed_note_count": len(note_ids),
        "changed_card_count": card_count,
    }


def process_organization_queue() -> None:
    summary = {
        "fetched": 0,
        "processed": 0,
        "succeeded": 0,
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
    changed_note_ids = set()

    try:
        operations = fetch_remote_organization_operations(ORGANIZATION_MAX_OPERATIONS_PER_RUN)
        if operations is None:
            summary["errors"].append("organization_api_no_response")
            return summary
        if not operations:
            log("organization queue empty")
            return summary

        summary["fetched"] = len(operations)
        log(f"organization queue fetched count={len(operations)}")

        for operation in operations:
            operation_id = operation.get("operation_id", "")
            operation_type = operation.get("operation_type", "")
            dry_run = operation_dry_run(operation)
            execution_mode = "preview" if dry_run else "direct"
            if operation.get("status") != "pending":
                summary["skipped"] += 1
                log(
                    f"organization skip non-pending operation_id={operation_id} "
                    f"operation_type={operation_type} execution_mode={execution_mode} "
                    f"dry_run={dry_run} "
                    f"status={operation.get('status')}"
                )
                continue

            receipt_state = local_operation_receipt_state(operation)
            if receipt_state["state"] == "valid":
                result = receipt_state["result"]
                receipt = receipt_state["receipt"]
                upsert_operation_index(operation, result=result, phase="receipt_replayed", receipt=receipt)
                summary["processed"] += 1
                summary["succeeded"] += 1
                summary["receipt_replays"] += 1
                if report_organization_operation_result(result, receipt):
                    summary["confirmed"] += 1
                else:
                    summary["confirmation_failed"] += 1
                log(f"organization receipt replay operation_id={operation_id} status=confirmed_without_apply")
                continue
            if receipt_state["state"] in {"expired", "collision", "invalid"}:
                reason = receipt_state.get("reason", "receipt_blocked")
                summary["failed"] += 1
                summary["receipt_blocked"] += 1
                summary["errors"].append(reason)
                log(f"organization receipt blocked operation_id={operation_id} reason={reason}")
                continue

            log(
                "organization queue processing "
                f"module_file={__file__} operation_id={operation_id} "
                f"operation_type={operation_type} execution_mode={execution_mode} "
                f"dry_run={dry_run}"
            )
            upsert_operation_index(operation, status="running", phase="processing_started")
            result = execute_organization_operation(operation)
            receipt = build_operation_receipt(operation, result)
            upsert_operation_index(operation, result=result, phase="processing_finished", receipt=receipt)
            summary["processed"] += 1
            if result.get("ok") and result.get("status") == "done":
                summary["succeeded"] += 1
                change_summary = organization_result_change_summary(result)
                if change_summary["changed"]:
                    summary["changed"] = True
                    summary["changed_operations"] += 1
                    changed_note_ids.update(change_summary["changed_note_ids"])
                    summary["changed_card_count"] += change_summary["changed_card_count"]
            elif result.get("status") == "skipped":
                summary["skipped"] += 1
            else:
                summary["failed"] += 1
                summary["errors"].extend(result.get("errors", []))

            if report_organization_operation_result(result, receipt):
                summary["confirmed"] += 1
            else:
                summary["confirmation_failed"] += 1

            log(
                "organization queue processed "
                f"operation_id={operation_id} operation_type={operation_type} "
                f"execution_mode={execution_mode} dry_run={dry_run} "
                f"status={result.get('status')} ok={result.get('ok')} "
                f"changed={organization_result_change_summary(result)['changed']}"
            )

        summary["changed_note_ids"] = sorted(changed_note_ids)
        summary["changed_note_count"] = len(changed_note_ids)
        log(
            "organization queue summary "
            f"fetched={summary['fetched']} processed={summary['processed']} "
            f"succeeded={summary['succeeded']} failed={summary['failed']} "
            f"skipped={summary['skipped']} confirmed={summary['confirmed']} "
            f"confirmation_failed={summary['confirmation_failed']} "
            f"receipt_replays={summary['receipt_replays']} receipt_blocked={summary['receipt_blocked']} "
            f"changed={summary['changed']} changed_operations={summary['changed_operations']} "
            f"changed_note_count={summary['changed_note_count']} "
            f"changed_card_count={summary['changed_card_count']}"
        )
    except Exception as e:
        log(f"organization queue exception {type(e).__name__}: {e}")
        summary["errors"].append(f"{type(e).__name__}: {e}")

    return summary
