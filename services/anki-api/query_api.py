from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from datetime import datetime, timezone
from pathlib import Path
from html.parser import HTMLParser
import hashlib
import html
import hmac
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import threading
import time
import unicodedata
import uuid

from fo_contracts import ContractError, validate_aulas_index_header
from state_store import (
    GENERATION_FILES,
    INDEX_SCHEMA_VERSION,
    GenerationStateCache,
    atomic_write_bytes as state_atomic_write_bytes,
    atomic_write_json as state_atomic_write_json,
    publish_generation,
)

API_VERSION = "3.0.0"
PROCESS_STARTED_AT = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
PROCESS_STARTED_MONOTONIC = time.monotonic()
PROCESS_MODULE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
PUBLIC_READ_PATHS = {
    "/health",
    "/version",
    "/openapi.json",
    "/anki_gpt_full_schema_30ops_stable.openapi.json",
    "/gpt_builder_organization_wrappers.openapi.json",
}

def env_int(name, default, minimum=None, maximum=None):
    try:
        value = int(os.environ.get(name, str(default)))
    except Exception:
        value = default
    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    return value.strip().casefold() in {"1", "true", "yes", "on"}


BASE_DIR = Path(os.environ.get("ANKI_GPT_BASE_DIR", "/home/ubuntu/anki-gpt-sync"))
DATA_DIR = BASE_DIR / "data"
STATE_DIR = Path(os.environ.get("ANKI_GPT_STATE_DIR", str(BASE_DIR / "state")))
SCRIPTS_DIR = Path(os.environ.get("ANKI_GPT_SCRIPTS_DIR", str(BASE_DIR / "scripts")))
NOTES_INDEX_PATH = STATE_DIR / "notes_index.json"
DECKS_INDEX_PATH = STATE_DIR / "decks_index.json"
SNAPSHOT_STATUS_PATH = STATE_DIR / "snapshot_status.json"
NOTE_MEDIA_INDEX_PATH = STATE_DIR / "note_media_index.json"
OPENAPI_SCHEMA_PATH = SCRIPTS_DIR / "anki_gpt_full_schema_30ops_stable.openapi.json"
ORGANIZATION_WRAPPER_SCHEMA_PATH = SCRIPTS_DIR / "gpt_builder_organization_wrappers.openapi.json"
MEDIA_DIR = STATE_DIR / "media"
FO_STATE_DIR = STATE_DIR / "federal_online"
FO_MATERIALS_DIR = FO_STATE_DIR / "materiais_fo"
FO_MANIFEST_PATH = FO_MATERIALS_DIR / "manifest.json"
FO_TRANSCRIPTS_DB_PATH = Path(os.environ.get("FO_TRANSCRIPTS_DB_PATH", "/home/ubuntu/fo-transcricoes-system/queue.sqlite"))
FO_TRANSCRIPTS_ROOT = Path(os.environ.get("FO_TRANSCRIPTS_ROOT", "/home/ubuntu/fo-transcricoes/Federal Online Transcrições"))
FO_SEARCH_INDEX_PATH = Path(os.environ.get("FO_SEARCH_INDEX_PATH", str(FO_STATE_DIR / "fo_transcripts_fts.sqlite")))
FO_TRANSCRIPTS_OUTPUT_PREFIX = "Federal Online Transcrições/"
FO_AULAS_INDEX_PATH = Path(os.environ.get("FO_AULAS_INDEX_PATH", "/opt/cronograma-fo/state/federal_online/cronograma_fo/aulas_index.tsv"))
UFPR_2027_BASE_DIR = Path(os.environ.get("ANKI_GPT_UFPR_2027_DIR", str(BASE_DIR / "knowledge" / "ufpr_2027")))
RECURRENCE_DIR = STATE_DIR / "recurrence"
RECURRENCE_INDEX_PATH = RECURRENCE_DIR / "recurrence_index.json"
TAGGING_DIR = STATE_DIR / "tagging"
TAGGING_OPERATIONS_DIR = TAGGING_DIR / "operations"
ORGANIZATION_DIR = STATE_DIR / "organization"
ORGANIZATION_OPERATIONS_DIR = ORGANIZATION_DIR / "operations"
ORGANIZATION_REORDER_ORDERS_DIR = ORGANIZATION_DIR / "reorder_orders"
ORGANIZATION_NOTE_FIELD_UPDATES_DIR = ORGANIZATION_DIR / "note_field_updates"
ACTION_LOG_PATH = STATE_DIR / "debug" / "action_log.jsonl"
ACTION_LOG_MAX_BYTES = env_int("ANKI_GPT_ACTION_LOG_MAX_BYTES", 5 * 1024 * 1024, minimum=1024)
ACTION_LOG_BACKUP_COUNT = env_int("ANKI_GPT_ACTION_LOG_BACKUP_COUNT", 5, minimum=1, maximum=20)
ACTION_LOG_LOCK = threading.Lock()
TAGGING_TOKEN_ENV = "ANKI_GPT_TAGGING_TOKEN"
TAGGING_TOKEN_FILE = Path(os.environ.get("ANKI_GPT_TAGGING_TOKEN_FILE", str(BASE_DIR / "tagging_token.txt")))
DEFAULT_CARD_BATCH_SIZE = env_int("ANKI_GPT_CARD_BATCH_SIZE", 40, minimum=1, maximum=500)
MAX_MATERIALIZE_BATCH_BYTES = env_int("ANKI_GPT_MAX_BATCH_BYTES", 900000, minimum=10000)
ALLOWED_TAGS = {
    "prio:alta",
    "prio:media",
    "prio:baixa",
    "avaliar",
    "precisa_melhorar",
    "overkill",
    "duplicado",
    "bom_card",
}
TAGGING_OPERATION_TYPES = {"add_tags", "remove_tags"}
TAGGING_FINAL_STATUSES = {"applied", "partially_applied", "failed"}
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
ORGANIZATION_FINAL_STATUSES = {"done", "failed", "partially_applied", "skipped"}
OPERATION_RECEIPT_SCHEMA_VERSION = 1
DEFAULT_CREATE_NOTE_TAGS = ["GPT"]
EXECUTION_MODES = {"preview", "direct"}
DEFAULT_EXECUTION_MODE = os.environ.get(
    "ANKI_GPT_DEFAULT_EXECUTION_MODE",
    "direct",
).strip().lower()
if DEFAULT_EXECUTION_MODE not in EXECUTION_MODES:
    DEFAULT_EXECUTION_MODE = "direct"
DRY_RUN_CAPABLE_OPERATION_TYPES = {
    "move_cards_to_deck",
    "move_notes_to_deck",
    "create_note",
    "create_notes",
    "replace_note_tags",
    "update_note_fields",
    "reorder_cards_by_material",
}
DESTRUCTIVE_OR_STRUCTURAL_OPERATION_TYPES = {
    "move_cards_to_deck",
    "move_notes_to_deck",
    "undo_reorganization",
    "undo_last_reorganization",
    "reorder_cards_by_material",
}
UFPR_2027_CATEGORIES = {
    "literatura": {
        "dir": "Literatura",
        "index": "00_INDEX_LITERATURA.md",
        "regra": "00_REGRA_EDITAL_LITERATURA.md",
    },
    "filosofia": {
        "dir": "Filosofia",
        "index": "00_INDEX_FILOSOFIA.md",
        "regra": "00_REGRA_EDITAL_FILOSOFIA.md",
    },
    "sociologia": {
        "dir": "Sociologia",
        "index": "00_INDEX_SOCIOLOGIA.md",
        "regra": "00_REGRA_EDITAL_SOCIOLOGIA.md",
    },
}
UFPR_2027_GENERAL_INDEX = "00_INDEX_GERAL_UFPR_2027.md"
UFPR_2027_STOPWORDS = {
    "a", "ao", "aos", "as", "com", "da", "das", "de", "do", "dos", "e", "em",
    "na", "nas", "no", "nos", "o", "os", "para", "por", "que", "se", "um", "uma",
}

HOST = os.environ.get("ANKI_GPT_HOST", "127.0.0.1")
PORT = env_int("ANKI_GPT_PORT", 8767, minimum=1, maximum=65535)
REQUEST_SOCKET_TIMEOUT_SECONDS = int(os.environ.get("ANKI_GPT_REQUEST_SOCKET_TIMEOUT_SECONDS", "240"))
MAX_SYNC_BODY_BYTES = env_int(
    "ANKI_GPT_MAX_SYNC_BODY_BYTES",
    60 * 1024 * 1024,
    minimum=1024,
    maximum=100 * 1024 * 1024,
)
MAX_JSON_BODY_BYTES = env_int(
    "ANKI_GPT_MAX_JSON_BODY_BYTES",
    2 * 1024 * 1024,
    minimum=1024,
    maximum=10 * 1024 * 1024,
)
REQUIRE_READ_AUTH = env_bool("ANKI_GPT_REQUIRE_READ_AUTH", False)

STATE_CACHE = GenerationStateCache(STATE_DIR, {
    "notes_index.json": NOTES_INDEX_PATH,
    "decks_index.json": DECKS_INDEX_PATH,
    "note_media_index.json": NOTE_MEDIA_INDEX_PATH,
    "snapshot_status.json": SNAPSHOT_STATUS_PATH,
})
DERIVED_STATE_LOCK = threading.RLock()
DERIVED_STATE_CACHE = {"key": None, "value": None, "hits": 0, "misses": 0}
NORMAL_SEARCH_CACHE_LOCK = threading.RLock()
NORMAL_SEARCH_CACHE = {"key": None, "value": None, "hits": 0, "misses": 0}
OBSERVABILITY_LOCK = threading.RLock()
OBSERVABILITY = {"last_error": None, "request_count": 0}


def log_server_event(event, **fields):
    record = {
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)


def record_sanitized_error(where, error):
    sanitized = {
        "at": utc_now_iso(),
        "where": str(where),
        "error_type": type(error).__name__,
    }
    with OBSERVABILITY_LOCK:
        OBSERVABILITY["last_error"] = sanitized
    return sanitized


def valid_trace_identifier(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
        return None
    return value


def request_content_length(handler):
    try:
        return int(handler.headers.get("Content-Length", "0") or "0")
    except Exception:
        return None


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    objects, _manifest = STATE_CACHE.snapshot()
    notes_index = objects["notes_index.json"]
    note_media_index = objects["note_media_index.json"]
    return notes_index, note_media_index


def load_optional_json(path: Path, default):
    try:
        return load_json(path)
    except FileNotFoundError:
        return default


def load_decks_index():
    try:
        objects, _manifest = STATE_CACHE.snapshot()
        return objects["decks_index.json"]
    except FileNotFoundError:
        return {"decks": []}


def load_snapshot_status():
    try:
        objects, _manifest = STATE_CACHE.snapshot()
        return objects["snapshot_status.json"]
    except FileNotFoundError:
        return {}


def load_fo_manifest():
    return load_json(FO_MANIFEST_PATH)


def load_recurrence_index():
    return load_json(RECURRENCE_INDEX_PATH)


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_sha256(value):
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def organization_operation_receipt_payload(operation):
    payload = (
        dict(operation.get("payload"))
        if isinstance(operation.get("payload"), dict)
        else {}
    )
    schema_version = int(operation.get("operation_schema_version") or 1)
    if schema_version < 3:
        payload.pop("execution_mode", None)
    receipt_payload = {
        "operation_id": operation.get("operation_id") or operation.get("op_id") or "",
        "operation_type": operation.get("operation_type") or operation.get("type") or "",
        "operation_schema_version": schema_version,
        "confirmed_by_user": operation.get("confirmed_by_user") is True,
        "payload": payload,
    }
    if schema_version >= 3:
        receipt_payload["execution_mode"] = (
            operation.get("execution_mode") or payload.get("execution_mode")
        )
    return receipt_payload


def validate_organization_confirmation_receipt(operation, confirmation_payload):
    receipt = confirmation_payload.get("receipt")
    if receipt is None:
        return None
    if not isinstance(receipt, dict):
        raise ValueError("invalid_receipt")

    required_strings = (
        "receipt_id",
        "operation_id",
        "operation_type",
        "applied_at",
        "expires_at",
        "operation_hash",
        "result_hash",
        "preconditions_hash",
    )
    if receipt.get("schema_version") != OPERATION_RECEIPT_SCHEMA_VERSION:
        raise ValueError("receipt_schema_mismatch")
    if any(not isinstance(receipt.get(key), str) or not receipt.get(key) for key in required_strings):
        raise ValueError("invalid_receipt_fields")
    if any(not re.fullmatch(r"[0-9a-f]{64}", receipt[key]) for key in (
        "receipt_id", "operation_hash", "result_hash", "preconditions_hash"
    )):
        raise ValueError("invalid_receipt_hash")

    operation_id = operation.get("operation_id") or operation.get("op_id") or ""
    operation_type = operation.get("operation_type") or operation.get("type") or ""
    if receipt["operation_id"] != operation_id or receipt["operation_type"] != operation_type:
        raise ValueError("receipt_operation_mismatch")
    expected_operation_hash = canonical_sha256(organization_operation_receipt_payload(operation))
    if not hmac.compare_digest(receipt["operation_hash"], expected_operation_hash):
        raise ValueError("receipt_operation_hash_mismatch")

    result_without_receipt = {
        key: value for key, value in confirmation_payload.items() if key != "receipt"
    }
    expected_result_hash = canonical_sha256(result_without_receipt)
    if not hmac.compare_digest(receipt["result_hash"], expected_result_hash):
        raise ValueError("receipt_result_hash_mismatch")

    try:
        expires_at = datetime.fromisoformat(receipt["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("receipt_expiry_invalid") from exc
    if datetime.now(timezone.utc) >= expires_at.astimezone(timezone.utc):
        raise ValueError("receipt_expired")

    return {key: receipt.get(key) for key in (
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
    )}


def organization_confirmation_is_receipt_replay(operation, payload, receipt):
    if receipt is None:
        return False
    confirmation = operation.get("execution_confirmation")
    stored = confirmation.get("receipt") if isinstance(confirmation, dict) else None
    if not isinstance(stored, dict):
        return False
    return all(hmac.compare_digest(str(stored.get(key, "")), str(receipt.get(key, ""))) for key in (
        "receipt_id", "operation_hash", "result_hash"
    )) and payload.get("status") == operation.get("status")


def tagging_operation_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"tagop-{stamp}-{uuid.uuid4().hex[:8]}"


def organization_operation_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"orgop-{stamp}-{uuid.uuid4().hex[:8]}"


def reorder_order_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"rord-{stamp}-{uuid.uuid4().hex[:8]}"


def note_field_updates_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"nfupd-{stamp}-{uuid.uuid4().hex[:8]}"


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


def send_json(handler, obj, status=200, headers=None):
    body = json_bytes(obj)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Cache-Control", "no-store")
    response_headers = {
        **(getattr(handler, "_extra_response_headers", None) or {}),
        **(headers or {}),
    }
    for name, value in response_headers.items():
        handler.send_header(str(name), str(value))
    handler.end_headers()
    handler.wfile.write(body)


def send_json_bytes(handler, body: bytes, status=200):
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def configure_tagging_token_from_file():
    if os.environ.get(TAGGING_TOKEN_ENV):
        return
    try:
        token = TAGGING_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    if token:
        os.environ[TAGGING_TOKEN_ENV] = token


def require_tagging_token(handler):
    configure_tagging_token_from_file()
    expected_token = os.environ.get(TAGGING_TOKEN_ENV, "")
    if not expected_token:
        send_json(handler, {
            "error": "tagging_token_not_configured",
            "env": TAGGING_TOKEN_ENV,
        }, status=503)
        return False

    get_all = getattr(handler.headers, "get_all", None)
    received_tokens = get_all("X-Tagging-Token") if callable(get_all) else None
    if received_tokens is None:
        received_tokens = [handler.headers.get("X-Tagging-Token", "")]
    if len(received_tokens) != 1:
        send_json(handler, {"error": "unauthorized"}, status=401)
        return False
    received_token = received_tokens[0]
    if not hmac.compare_digest(received_token, expected_token):
        send_json(handler, {"error": "unauthorized"}, status=401)
        return False

    return True


def read_json_body(handler, max_bytes=None):
    length = parse_int(handler.headers.get("Content-Length", "0"), 0)
    if length <= 0:
        return {}
    content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
    if content_type != "application/json":
        raise ValueError("unsupported_content_type")
    if max_bytes is None:
        max_bytes = MAX_JSON_BODY_BYTES
    if max_bytes is not None and length > max_bytes:
        raise ValueError("request_body_too_large")
    raw_body = handler.rfile.read(length).decode("utf-8", errors="replace")
    return json.loads(raw_body)


def safe_action_headers(handler):
    return {
        "content-type": handler.headers.get("Content-Type"),
        "content-length": handler.headers.get("Content-Length"),
        "user-agent": handler.headers.get("User-Agent"),
        "host": handler.headers.get("Host"),
    }


def redact_json_shape(value, depth=0):
    if depth > 8:
        return {"type": "truncated"}
    if isinstance(value, dict):
        return {"type": "object", "length": len(value)}
    if isinstance(value, list):
        return {
            "type": "array",
            "length": len(value),
            "items": [redact_json_shape(item, depth + 1) for item in value[:10]],
            "truncated": len(value) > 10,
        }
    if isinstance(value, str):
        return {"type": "string", "length": len(value)}
    if isinstance(value, bool):
        return {"type": "boolean", "value": value}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "integer", "value": value}
    if isinstance(value, float):
        return {"type": "number", "value": value}
    if value is None:
        return {"type": "null"}
    return {"type": type(value).__name__}


def rotate_file_if_needed(path: Path, max_bytes: int, backup_count: int) -> None:
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    oldest = path.with_name(f"{path.name}.{backup_count}")
    oldest.unlink(missing_ok=True)
    for index in range(backup_count - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))


def append_action_log(event):
    ACTION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        ACTION_LOG_PATH.parent.chmod(0o700)
    except Exception:
        pass
    record = {"timestamp": utc_now_iso(), **event}
    with ACTION_LOG_LOCK:
        rotate_file_if_needed(ACTION_LOG_PATH, ACTION_LOG_MAX_BYTES, ACTION_LOG_BACKUP_COUNT)
        with ACTION_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        try:
            ACTION_LOG_PATH.chmod(0o600)
            for index in range(1, ACTION_LOG_BACKUP_COUNT + 1):
                rotated = ACTION_LOG_PATH.with_name(f"{ACTION_LOG_PATH.name}.{index}")
                if rotated.exists():
                    rotated.chmod(0o600)
        except Exception:
            pass


def log_action_event(handler, path, stage, **extra):
    event = {
        "stage": stage,
        "path": path,
        "method": getattr(handler, "command", ""),
        "headers": safe_action_headers(handler),
        "token_received": bool(handler.headers.get("X-Tagging-Token")),
        "request_id": getattr(handler, "_request_id", None),
        "correlation_id": getattr(handler, "_correlation_id", None),
        "generation_id": STATE_CACHE.metrics().get("generation_id"),
    }
    operation_id = extra.get("operation_id")
    if isinstance(operation_id, str) and operation_id:
        handler._operation_id = operation_id
    event.update(extra)
    append_action_log(event)


def mark_deprecated_get(handler, path, successor_path):
    handler._extra_response_headers = {
        "Deprecation": "true",
        "Sunset": "Thu, 01 Oct 2026 00:00:00 GMT",
        "Link": f'<{successor_path}>; rel="successor-version"',
        "Warning": '299 - "Deprecated GET mutation; use POST"',
    }
    log_action_event(
        handler,
        path,
        "legacy_get_deprecated",
        successor_method="POST",
        successor_path=successor_path,
    )


def read_json_body_for_action(handler, path):
    try:
        payload = read_json_body(handler)
    except json.JSONDecodeError as exc:
        log_action_event(
            handler,
            path,
            "json_parse_failed",
            raw_body_size=request_content_length(handler),
            json_parse_ok=False,
            exception=f"{type(exc).__name__}: {exc}",
        )
        raise
    log_action_event(
        handler,
        path,
        "json_parsed",
        raw_body_size=request_content_length(handler),
        json_parse_ok=True,
        body=redact_json_shape(payload),
    )
    return payload


def read_action_log_tail(limit):
    if not ACTION_LOG_PATH.exists():
        return []
    lines = ACTION_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    events = []
    for line in lines[-limit:]:
        try:
            events.append(json.loads(line))
        except Exception:
            events.append({"malformed": line})
    return events


def handle_action_logged_organization_post(handler, path, build_operation):
    log_action_event(handler, path, "request_received")
    try:
        if not require_tagging_token(handler):
            log_action_event(handler, path, "auth_failed")
            return
        log_action_event(handler, path, "auth_ok")

        try:
            payload = read_json_body_for_action(handler, path)
        except json.JSONDecodeError:
            log_action_event(handler, path, "response_sent", status=400, error="invalid_json")
            return send_json(handler, {"error": "invalid_json"}, status=400)
        except ValueError as exc:
            log_action_event(handler, path, "response_sent", status=400, error=str(exc))
            return send_json(handler, {"error": str(exc)}, status=400)

        try:
            operation = build_operation(payload)
        except ValueError as exc:
            log_action_event(handler, path, "validation_failed", error=str(exc), body=redact_json_shape(payload))
            log_action_event(handler, path, "response_sent", status=400, error=str(exc))
            return send_json(handler, {"error": str(exc)}, status=400)

        action_log_extra = {}
        if isinstance(operation, dict):
            action_log_extra = operation.pop("_action_log", {}) or {}
        log_action_event(
            handler,
            path,
            "validation_ok",
            operation_type=operation.get("operation_type"),
            **action_log_extra,
        )
        response = persist_organization_operation(operation)
        log_action_event(
            handler,
            path,
            "operation_created",
            operation_id=operation.get("operation_id"),
            operation_type=operation.get("operation_type"),
        )
        log_action_event(handler, path, "response_sent", status=200, operation_id=operation.get("operation_id"))
        return send_json(handler, response)
    except Exception as exc:
        error = record_sanitized_error("organization_action", exc)
        log_action_event(
            handler,
            path,
            "exception",
            **error,
        )
        return send_json(handler, {"error": "internal_error"}, status=500)


def atomic_write_json(path: Path, obj):
    state_atomic_write_json(path, obj)


def atomic_write_text(path: Path, text: str):
    state_atomic_write_bytes(path, text.encode("utf-8"))


def snapshot_filename_stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S_%f")


class SnapshotImageSrcParser(HTMLParser):
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


def extract_snapshot_media_refs_from_html(value):
    parser = SnapshotImageSrcParser()
    parser.feed(value)
    parser.close()
    return [ref for ref in parser.refs if ref]


def normalize_snapshot_media_refs(note):
    refs = []
    fields = note.get("fields", {})
    if isinstance(fields, dict):
        for value in fields.values():
            if isinstance(value, str) and value:
                refs.extend(extract_snapshot_media_refs_from_html(value))

    if not refs:
        image_refs = note.get("image_refs", [])
        if isinstance(image_refs, list):
            refs = [
                html.unescape(ref).strip()
                for ref in image_refs
                if isinstance(ref, str) and ref.strip()
            ]

    seen = set()
    ordered = []
    for ref in refs:
        if ref and ref not in seen:
            seen.add(ref)
            ordered.append(ref)
    return ordered


def build_snapshot_media_index(notes_by_id: dict) -> dict:
    media_files = {path.name for path in MEDIA_DIR.iterdir() if path.is_file()} if MEDIA_DIR.exists() else set()
    index = {}
    for note_id, note in notes_by_id.items():
        refs = normalize_snapshot_media_refs(note) if isinstance(note, dict) else []
        resolved = []
        external = []
        broken = []
        for ref in refs:
            if ref.startswith(("http://", "https://")):
                external.append(ref)
            elif ref.startswith("blob:"):
                broken.append(ref)
            elif ref in media_files:
                resolved.append(ref)
            else:
                broken.append(ref)
        index[str(note_id)] = {
            "has_images": bool(refs),
            "resolved_media": resolved,
            "external_media": external,
            "broken_media": broken,
        }
    return index


def publish_full_snapshot_payload(payload, request_path="/sync/full"):
    if not isinstance(payload, dict):
        raise ValueError("invalid_snapshot_payload")

    notes = payload.get("notes", [])
    if not isinstance(notes, list):
        raise ValueError("invalid_snapshot_notes")

    by_id = {
        str(note["note_id"]): note
        for note in notes
        if isinstance(note, dict) and "note_id" in note
    }
    if len(by_id) != len(notes):
        raise ValueError("snapshot_note_ids_invalid_or_duplicate")

    decks = payload.get("decks", [])
    if not isinstance(decks, list):
        decks = []

    total_cards = payload.get("total_cards")
    if not isinstance(total_cards, int):
        total_cards = sum(len(note.get("cards", [])) or 1 for note in by_id.values())

    total_notes = payload.get("total_notes")
    if not isinstance(total_notes, int):
        total_notes = len(by_id)

    total_decks = payload.get("total_decks")
    if not isinstance(total_decks, int):
        total_decks = len(decks) if decks else len({
            note.get("deck", "")
            for note in by_id.values()
            if note.get("deck")
        })

    generated_at = payload.get("generated_at") or payload.get("timestamp") or utc_now_iso()
    if total_notes != len(by_id):
        raise ValueError("snapshot_total_notes_mismatch")
    if decks and total_decks != len(decks):
        raise ValueError("snapshot_total_decks_mismatch")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = DATA_DIR / f"{snapshot_filename_stamp()}.json"
    atomic_write_json(snapshot_path, payload)

    decks_index = {
        "generated_at": generated_at,
        "profile": payload.get("profile"),
        "snapshot_version": payload.get("snapshot_version"),
        "total_decks": total_decks,
        "total_cards": total_cards,
        "total_notes": total_notes,
        "decks": decks,
    }
    snapshot_status = {
        "generated_at": generated_at,
        "timestamp": payload.get("timestamp"),
        "source_snapshot": str(snapshot_path),
        "source": payload.get("source"),
        "event": payload.get("event"),
        "profile": payload.get("profile"),
        "snapshot_version": payload.get("snapshot_version"),
        "snapshot_note_count": len(by_id),
        "notes_with_images": payload.get("notes_with_images"),
        "total_decks": total_decks,
        "total_cards": total_cards,
        "total_notes": total_notes,
        "ingested_at": utc_now_iso(),
        "ingested_via": request_path,
    }

    seen_refs = set()
    media_refs = []
    for note in by_id.values():
        if not isinstance(note, dict):
            continue
        for ref in normalize_snapshot_media_refs(note):
            if ref and ref not in seen_refs:
                seen_refs.add(ref)
                media_refs.append(ref)
    note_media_index = build_snapshot_media_index(by_id)
    manifest = publish_generation(
        STATE_DIR,
        {
            "notes_index.json": by_id,
            "decks_index.json": decks_index,
            "note_media_index.json": note_media_index,
            "snapshot_status.json": snapshot_status,
        },
        metadata={
            "generated_at": generated_at,
            "snapshot_version": payload.get("snapshot_version"),
            "addon_version": payload.get("addon_version"),
            "api_version": API_VERSION,
            "total_notes": total_notes,
            "total_cards": total_cards,
            "total_decks": total_decks,
        },
    )

    # Legacy mirrors remain for scripts not yet generation-aware. They are
    # written only after the new generation is active and are never used by
    # this backend while current.json exists.
    atomic_write_json(NOTES_INDEX_PATH, by_id)
    atomic_write_json(DECKS_INDEX_PATH, decks_index)
    atomic_write_json(NOTE_MEDIA_INDEX_PATH, note_media_index)
    atomic_write_json(SNAPSHOT_STATUS_PATH, snapshot_status)
    atomic_write_text(STATE_DIR / "media_refs.txt", "\n".join(media_refs) + ("\n" if media_refs else ""))

    return {
        "ok": True,
        "generated_at": generated_at,
        "note_count": len(by_id),
        "total_decks": total_decks,
        "total_cards": total_cards,
        "total_notes": total_notes,
        "media_refs": len(media_refs),
        "generation_id": manifest["generation_id"],
        "index_schema_version": manifest["index_schema_version"],
    }


def send_file(handler, path: Path):
    if not path.exists() or not path.is_file():
        send_json(handler, {"error": "media_not_found"}, status=404)
        return

    ctype, _ = mimetypes.guess_type(str(path))
    if not ctype:
        ctype = "application/octet-stream"

    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def get_first(params, key, default=None):
    vals = params.get(key)
    if not vals:
        return default
    return vals[0]


def get_all(params, key):
    return [v for v in params.get(key, []) if v is not None]


def get_optional_query_bool(params, key):
    value = get_first(params, key)
    if value is None or value == "":
        return None
    value = value.strip().casefold()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"invalid_{key}")


def get_optional_query_int(params, key):
    value = get_first(params, key)
    if value is None or value == "":
        return None
    try:
        return int(value.strip())
    except Exception:
        raise ValueError(f"invalid_{key}") from None


def parse_int(value, default):
    try:
        return int(value)
    except Exception:
        return default


def limit_offset(params):
    limit = parse_int(get_first(params, "limit", 50), 50)
    offset = parse_int(get_first(params, "offset", 0), 0)

    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500
    if offset < 0:
        offset = 0

    return limit, offset


def normalize_search_text(value):
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text.casefold()


def ufpr_base_dir():
    return UFPR_2027_BASE_DIR.resolve(strict=False)


def ufpr_path_inside_base(path: Path):
    try:
        path.resolve(strict=False).relative_to(ufpr_base_dir())
        return True
    except ValueError:
        return False


def ufpr_safe_path(*parts):
    path = ufpr_base_dir().joinpath(*parts).resolve(strict=False)
    if not ufpr_path_inside_base(path):
        raise ValueError("invalid_ufpr_path")
    return path


def resolve_ufpr_category(category, required=True):
    if category is None or str(category).strip() == "":
        if required:
            raise ValueError("missing_categoria")
        return None, None
    normalized = normalize_search_text(str(category).strip())
    if normalized not in UFPR_2027_CATEGORIES:
        raise ValueError("invalid_categoria")
    return normalized, UFPR_2027_CATEGORIES[normalized]


def ufpr_category_dir(category):
    category, spec = resolve_ufpr_category(category)
    return category, ufpr_safe_path(spec["dir"])


def ufpr_read_text_file(path: Path):
    resolved = path.resolve(strict=False)
    if not ufpr_path_inside_base(resolved):
        raise ValueError("invalid_ufpr_path")
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(str(resolved))
    return resolved.read_text(encoding="utf-8", errors="replace")


def ufpr_manifest_path(category):
    category, category_path = ufpr_category_dir(category)
    return category_path / "manifest.json"


def ufpr_category_index_path(category):
    category, spec = resolve_ufpr_category(category)
    return ufpr_safe_path(spec["dir"], spec["index"])


def ufpr_category_regra_path(category):
    category, spec = resolve_ufpr_category(category)
    return ufpr_safe_path(spec["dir"], spec["regra"])


def ufpr_manifest_records(manifest):
    if isinstance(manifest, list):
        return manifest
    if not isinstance(manifest, dict):
        return []
    for key in ("items", "obras", "textos", "works", "entries"):
        value = manifest.get(key)
        if isinstance(value, list):
            return value
    return []


def ufpr_int_or_none(value):
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def ufpr_item_file_name(record):
    for key in ("arquivo", "file", "filename", "path", "markdown", "markdown_path"):
        value = record.get(key) if isinstance(record, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def ufpr_item_id_from_record(record):
    if isinstance(record, dict):
        for key in ("obra_id", "id", "slug"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    filename = ufpr_item_file_name(record)
    if filename:
        stem = Path(filename).stem
        return re.sub(r"^\d+[_-]+", "", stem).strip()
    return ""


def ufpr_normalize_manifest_item(category, record):
    if not isinstance(record, dict):
        return None

    item_id = ufpr_item_id_from_record(record)
    arquivo = ufpr_item_file_name(record)
    if not item_id or not arquivo:
        return None

    category, category_path = ufpr_category_dir(category)
    file_path = (category_path / arquivo).resolve(strict=False)
    if not ufpr_path_inside_base(file_path):
        raise ValueError("invalid_ufpr_manifest_file")

    chars = ufpr_int_or_none(record.get("chars"))
    if chars is None and file_path.exists() and file_path.is_file():
        chars = len(file_path.read_text(encoding="utf-8", errors="replace"))

    paginas_pdf = None
    for key in ("paginas_pdf", "paginas", "pages", "pdf_pages"):
        paginas_pdf = ufpr_int_or_none(record.get(key))
        if paginas_pdf is not None:
            break

    return {
        "id": str(item_id),
        "titulo": str(record.get("titulo") or record.get("title") or ""),
        "autor": str(record.get("autor") or record.get("author") or ""),
        "arquivo": arquivo,
        "chars": chars,
        "paginas_pdf": paginas_pdf,
        "_path": file_path,
        "_raw": record,
    }


def ufpr_public_item(item):
    return {
        "id": item.get("id"),
        "titulo": item.get("titulo"),
        "autor": item.get("autor"),
        "arquivo": item.get("arquivo"),
        "chars": item.get("chars"),
        "paginas_pdf": item.get("paginas_pdf"),
    }


def load_ufpr_manifest(category):
    manifest_path = ufpr_manifest_path(category)
    manifest = load_json(manifest_path)
    items = []
    for record in ufpr_manifest_records(manifest):
        item = ufpr_normalize_manifest_item(category, record)
        if item is not None:
            items.append(item)
    return manifest_path, items


def resolve_ufpr_item(category, item_id):
    if item_id is None or str(item_id).strip() == "":
        raise ValueError("missing_id")
    requested = normalize_search_text(str(item_id).strip())
    manifest_path, items = load_ufpr_manifest(category)
    matches = [
        item for item in items
        if normalize_search_text(item.get("id")) == requested
    ]
    if not matches:
        raise LookupError("ufpr_item_not_found")
    if len(matches) > 1:
        raise ValueError("ufpr_id_ambiguous_in_categoria")
    return matches[0]


def resolve_ufpr_item_across_categories(item_id):
    if item_id is None or str(item_id).strip() == "":
        raise ValueError("missing_id")
    matches = []
    for category in UFPR_2027_CATEGORIES:
        try:
            item = resolve_ufpr_item(category, item_id)
        except (FileNotFoundError, LookupError):
            continue
        matches.append((category, item))
    if not matches:
        raise LookupError("ufpr_item_not_found")
    if len(matches) > 1:
        return None, {
            "error": "ufpr_id_ambiguous",
            "message": "Informe categoria para desambiguar o id.",
            "matches": [
                {
                    "categoria": category,
                    "id": item.get("id"),
                    "titulo": item.get("titulo"),
                    "autor": item.get("autor"),
                    "arquivo": item.get("arquivo"),
                }
                for category, item in matches
            ],
        }
    return matches[0], None


def ufpr_obras_response():
    categorias = []
    for category, spec in UFPR_2027_CATEGORIES.items():
        manifest_path, items = load_ufpr_manifest(category)
        categorias.append({
            "categoria": category,
            "manifest_available": manifest_path.exists(),
            "index_file": spec["index"],
            "regra_file": spec["regra"],
            "items": [ufpr_public_item(item) for item in items],
        })
    return {
        "ok": True,
        "base_dir": str(ufpr_base_dir()),
        "categorias": categorias,
    }


def ufpr_index_response(category):
    category, spec = resolve_ufpr_category(category, required=False)
    if category is None:
        path = ufpr_safe_path(UFPR_2027_GENERAL_INDEX)
        label = "geral"
    else:
        path = ufpr_safe_path(spec["dir"], spec["index"])
        label = category
    return {
        "ok": True,
        "categoria": label,
        "content": ufpr_read_text_file(path),
    }


def ufpr_regra_response(category):
    category, spec = resolve_ufpr_category(category)
    path = ufpr_safe_path(spec["dir"], spec["regra"])
    return {
        "ok": True,
        "categoria": category,
        "content": ufpr_read_text_file(path),
    }


def ufpr_detect_page(text, start):
    prefix = text[:max(0, start)]
    page = None
    for match in re.finditer(r"(?im)^##\s*P[áa]gina\s+(\d+)\b", prefix):
        page = ufpr_int_or_none(match.group(1))
    return page


def ufpr_query_terms(query):
    normalized = normalize_search_text(query)
    terms = []
    seen = set()
    for term in re.findall(r"[a-z0-9_]+", normalized):
        if len(term) < 2 or term in UFPR_2027_STOPWORDS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return normalized.strip(), terms


def ufpr_paragraph_spans(text):
    for match in re.finditer(r"(?s)\S.*?(?=\n\s*\n|\Z)", text):
        yield match.start(), match.end(), match.group(0)


def ufpr_result_window(text, paragraph_start, paragraph_end, paragraph, first_hit, context_chars):
    if paragraph_end - paragraph_start <= context_chars:
        start = paragraph_start
        end = paragraph_end
    else:
        hit = max(0, min(len(paragraph), first_hit if first_hit is not None else 0))
        half = context_chars // 2
        rel_start = max(0, hit - half)
        rel_end = min(len(paragraph), rel_start + context_chars)
        rel_start = max(0, rel_end - context_chars)
        start = paragraph_start + rel_start
        end = paragraph_start + rel_end
    if start == 0 and end >= len(text) and len(text) > 1:
        max_len = min(context_chars, len(text) - 1)
        hit = max(0, min(len(paragraph), first_hit if first_hit is not None else 0))
        center = paragraph_start + hit
        start = max(0, center - (max_len // 2))
        end = min(len(text), start + max_len)
        start = max(0, end - max_len)
    return start, end, text[start:end].strip()


def ufpr_search_item(category, item, query_normalized, terms, context_chars):
    text = ufpr_read_text_file(item["_path"])
    metadata_normalized = normalize_search_text(
        " ".join([
            str(item.get("id") or ""),
            str(item.get("titulo") or ""),
            str(item.get("autor") or ""),
            str(item.get("arquivo") or ""),
        ])
    )
    metadata_boost = 0
    if query_normalized and query_normalized in metadata_normalized:
        metadata_boost += 5
    metadata_boost += sum(1 for term in terms if term in metadata_normalized)

    results = []
    for paragraph_start, paragraph_end, paragraph in ufpr_paragraph_spans(text):
        paragraph_normalized = normalize_search_text(paragraph)
        literal_count = paragraph_normalized.count(query_normalized) if query_normalized else 0
        term_counts = [paragraph_normalized.count(term) for term in terms]
        term_score = sum(min(count, 5) for count in term_counts)
        if literal_count <= 0 and term_score <= 0:
            continue

        first_hits = []
        if literal_count > 0:
            first_hits.append(paragraph_normalized.find(query_normalized))
        first_hits.extend(paragraph_normalized.find(term) for term, count in zip(terms, term_counts) if count > 0)
        first_hits = [hit for hit in first_hits if hit >= 0]
        first_hit = min(first_hits) if first_hits else 0
        start, end, trecho = ufpr_result_window(
            text,
            paragraph_start,
            paragraph_end,
            paragraph,
            first_hit,
            context_chars,
        )
        score = (literal_count * 10) + (term_score * 2) + metadata_boost
        results.append({
            "categoria": category,
            "id": item.get("id"),
            "titulo": item.get("titulo"),
            "autor": item.get("autor"),
            "arquivo": item.get("arquivo"),
            "score": score,
            "pagina_detectada": ufpr_detect_page(text, start),
            "start_char": start,
            "end_char": end,
            "trecho": trecho,
        })
    return results


def ufpr_query_response(payload):
    if not isinstance(payload, dict):
        raise ValueError("invalid_payload")
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("missing_query")

    limit = parse_int(payload.get("limit", 8), 8)
    if limit < 1:
        limit = 1
    if limit > 20:
        limit = 20

    context_chars = parse_int(payload.get("context_chars", 1200), 1200)
    if context_chars < 200:
        context_chars = 200
    if context_chars > 4000:
        context_chars = 4000

    category = payload.get("categoria")
    item_id = payload.get("id") if payload.get("id") is not None else payload.get("obra_id")
    query_normalized, terms = ufpr_query_terms(query)
    if not terms and not query_normalized:
        raise ValueError("missing_query")

    targets = []
    if category:
        category, _spec = resolve_ufpr_category(category)
        if item_id:
            targets = [(category, resolve_ufpr_item(category, item_id))]
        else:
            _manifest_path, items = load_ufpr_manifest(category)
            targets = [(category, item) for item in items]
    elif item_id:
        match, error = resolve_ufpr_item_across_categories(item_id)
        if error:
            return {**error, "ok": False}
        targets = [match]
    else:
        for category in UFPR_2027_CATEGORIES:
            _manifest_path, items = load_ufpr_manifest(category)
            targets.extend((category, item) for item in items)

    results = []
    for category, item in targets:
        results.extend(ufpr_search_item(category, item, query_normalized, terms, context_chars))

    results.sort(key=lambda item: (-item["score"], item["categoria"], item["id"], item["start_char"]))
    results = results[:limit]
    response = {
        "ok": True,
        "query": query,
        "limit": limit,
        "context_chars": context_chars,
        "results": results,
    }
    if not results:
        response["message"] = "Nenhum trecho encontrado."
    return response


def ufpr_trecho_response(params):
    category = get_first(params, "categoria")
    item_id = get_first(params, "id") or get_first(params, "obra_id")
    category, _spec = resolve_ufpr_category(category)
    item = resolve_ufpr_item(category, item_id)
    start = parse_int(get_first(params, "start", 0), 0)
    if start < 0:
        start = 0
    chars = parse_int(get_first(params, "chars", 4000), 4000)
    if chars < 1:
        chars = 1
    if chars > 10000:
        chars = 10000

    content = ufpr_read_text_file(item["_path"])
    end = min(len(content), start + chars)
    return {
        "ok": True,
        "categoria": category,
        "id": item.get("id"),
        "titulo": item.get("titulo"),
        "autor": item.get("autor"),
        "arquivo": item.get("arquivo"),
        "start": start,
        "end": end,
        "chars_requested": chars,
        "content": content[start:end],
    }


def ufpr_status_response():
    base_dir = ufpr_base_dir()
    total_size_bytes = 0
    if base_dir.exists():
        for path in base_dir.rglob("*"):
            if path.is_file() and ufpr_path_inside_base(path):
                try:
                    total_size_bytes += path.stat().st_size
                except OSError:
                    pass

    categorias = {}
    for category in UFPR_2027_CATEGORIES:
        info = {
            "items": 0,
            "missing_files": [],
            "small_files": [],
        }
        try:
            _manifest_path, items = load_ufpr_manifest(category)
            info["items"] = len(items)
            for item in items:
                path = item["_path"]
                public_item = ufpr_public_item(item)
                if not path.exists() or not path.is_file():
                    info["missing_files"].append(public_item)
                    continue
                chars = item.get("chars")
                if chars is None:
                    chars = len(ufpr_read_text_file(path))
                if chars < 2000:
                    info["small_files"].append(public_item)
        except FileNotFoundError as exc:
            info["manifest_missing"] = True
            info["manifest_error"] = str(exc)
        categorias[category] = info

    return {
        "ok": True,
        "base_dir": str(base_dir),
        "base_dir_exists": base_dir.exists() and base_dir.is_dir(),
        "categorias": categorias,
        "total_size_bytes": total_size_bytes,
    }


class CleanTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"br", "div", "p", "li", "tr", "td", "th"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag.lower() in {"div", "p", "li", "tr", "td", "th"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if data:
            self.parts.append(data)


def html_to_clean_text(value):
    if value is None:
        return ""
    raw = str(value)
    if not raw:
        return ""
    parser = CleanTextParser()
    try:
        parser.feed(raw)
        parser.close()
        text = " ".join("".join(parser.parts).split())
    except Exception:
        text = " ".join(html.unescape(raw).split())
    return text


def clean_note_text(note):
    fields = note.get("fields", {})
    if not isinstance(fields, dict):
        return str(note.get("compare_text", "") or "")
    text = " ".join(
        html_to_clean_text(value)
        for value in fields.values()
        if isinstance(value, str) and value.strip()
    )
    return " ".join(text.split())


def datetime_string_from_ms_id(value):
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat()
    except Exception:
        return None


def card_status_label(card):
    try:
        card_type = int(card.get("type", -999))
        queue = int(card.get("queue", -999))
    except Exception:
        return "Unknown"
    if card_type == 0:
        if queue == 0:
            return "New"
        if queue == -1:
            return "New suspended"
        if queue in (-2, -3):
            return "New buried"
        return f"New queue {queue}"
    if card_type == 1:
        return "Learning"
    if card_type == 2:
        return "Review"
    if card_type == 3:
        return "Relearning"
    return f"Type {card_type} queue {queue}"


def normalize_deck_argument_for_snapshot(deck):
    if not isinstance(deck, str):
        return ""
    value = deck.strip()
    if value.casefold().startswith("deck:"):
        return unquote_deck_value(value[5:])
    return value


def normalize_note(note_id, note):
    note = dict(note or {})
    note.setdefault("note_id", note_id)
    note.setdefault("deck", "")
    note.setdefault("root_deck", "")
    note.setdefault("note_type", "")
    note.setdefault("kind", "")
    note.setdefault("tags", [])
    note.setdefault("field_names", [])
    note.setdefault("fields", {})
    note.setdefault("compare_text", "")
    return note


def tagging_operation_path(operation_id):
    if not isinstance(operation_id, str) or not operation_id.startswith("tagop-"):
        return None
    if any(ch in operation_id for ch in "/\\"):
        return None
    return TAGGING_OPERATIONS_DIR / f"{operation_id}.json"


def organization_operation_path(operation_id):
    if not isinstance(operation_id, str) or not operation_id.startswith("orgop-"):
        return None
    if any(ch in operation_id for ch in "/\\"):
        return None
    return ORGANIZATION_OPERATIONS_DIR / f"{operation_id}.json"


def reorder_order_path(order_id):
    if not isinstance(order_id, str) or not order_id.startswith("rord-"):
        return None
    if any(ch in order_id for ch in "/\\"):
        return None
    return ORGANIZATION_REORDER_ORDERS_DIR / f"{order_id}.json"


def note_field_updates_path(updates_id):
    if not isinstance(updates_id, str) or not updates_id.startswith("nfupd-"):
        return None
    if any(ch in updates_id for ch in "/\\"):
        return None
    return ORGANIZATION_NOTE_FIELD_UPDATES_DIR / f"{updates_id}.json"


def normalize_tags(raw_tags):
    if not isinstance(raw_tags, list) or not raw_tags:
        raise ValueError("invalid_tags")
    tags = []
    for tag in raw_tags:
        if not isinstance(tag, str) or tag not in ALLOWED_TAGS:
            raise ValueError("unsupported_tag")
        if tag not in tags:
            tags.append(tag)
    return tags


def normalize_tagging_target(raw_target):
    if not isinstance(raw_target, dict):
        raise ValueError("invalid_target")

    target_type = raw_target.get("type")
    if target_type == "deck":
        deck = raw_target.get("deck")
        if not isinstance(deck, str) or not deck.strip():
            raise ValueError("invalid_deck_target")
        return {"type": "deck", "deck": deck.strip()}

    if target_type == "note_ids":
        raw_note_ids = raw_target.get("note_ids")
        if not isinstance(raw_note_ids, list) or not raw_note_ids:
            raise ValueError("invalid_note_ids_target")
        note_ids = []
        for note_id in raw_note_ids:
            if isinstance(note_id, bool) or not isinstance(note_id, int):
                raise ValueError("invalid_note_id")
            if note_id not in note_ids:
                note_ids.append(note_id)
        return {"type": "note_ids", "note_ids": note_ids}

    raise ValueError("unsupported_target_type")


def optional_string(payload, key, default=""):
    value = payload.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"invalid_{key}")
    return value


def build_tagging_operation(payload):
    if payload.get("confirmed_by_user") is not True:
        raise ValueError("missing_explicit_confirmation")

    operation_type = payload.get("operation_type")
    if operation_type not in TAGGING_OPERATION_TYPES:
        raise ValueError("unsupported_operation_type")

    operation_id = tagging_operation_id()
    timestamp = utc_now_iso()
    return {
        "operation_id": operation_id,
        "timestamp": timestamp,
        "created_at": timestamp,
        "operation_type": operation_type,
        "target": normalize_tagging_target(payload.get("target")),
        "tags": normalize_tags(payload.get("tags")),
        "status": "pending_addon_execution",
        "origin": optional_string(payload, "origin", "gpt_action") or "gpt_action",
        "confirmed_by_user": True,
        "confirmation_message_id": optional_string(payload, "confirmation_message_id"),
        "reason": optional_string(payload, "reason"),
    }


def normalize_create_deck_payload(raw_payload):
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")

    deck_name = raw_payload.get("deck_name")
    if not isinstance(deck_name, str) or not deck_name.strip():
        raise ValueError("invalid_deck_name")

    return {"deck_name": deck_name.strip()}


def normalize_id_list(raw_ids, key):
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError(f"invalid_{key}")
    ids = []
    for raw_id in raw_ids:
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            raise ValueError(f"invalid_{key}")
        if raw_id not in ids:
            ids.append(raw_id)
    return ids


def normalize_add_tags(raw_tags):
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


def normalize_optional_bool(raw_payload, key, default):
    value = raw_payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"invalid_{key}")
    return value


def normalize_execution_mode(*sources, default=DEFAULT_EXECUTION_MODE):
    """Normalize execution intent once; processing status is handled separately."""
    normalized_default = str(default or "").strip().lower()
    if normalized_default not in EXECUTION_MODES:
        raise ValueError("invalid_default_execution_mode")

    candidates = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        if "execution_mode" in source:
            mode = source.get("execution_mode")
            if not isinstance(mode, str) or mode.strip().lower() not in EXECUTION_MODES:
                raise ValueError("invalid_execution_mode: expected preview or direct")
            candidates.append(("execution_mode", mode.strip().lower()))
        if "dry_run" in source:
            dry_run = source.get("dry_run")
            if not isinstance(dry_run, bool):
                raise ValueError("invalid_dry_run")
            candidates.append(("dry_run", "preview" if dry_run else "direct"))

    modes = {mode for _source, mode in candidates}
    if len(modes) > 1:
        raise ValueError(
            "execution_mode_dry_run_mismatch: preview requires dry_run=true "
            "and direct requires dry_run=false"
        )
    return next(iter(modes), normalized_default)


def execution_mode_dry_run(execution_mode):
    if execution_mode not in EXECUTION_MODES:
        raise ValueError("invalid_execution_mode")
    return execution_mode == "preview"


def operation_risk_level(operation_type):
    if operation_type in DESTRUCTIVE_OR_STRUCTURAL_OPERATION_TYPES:
        return "structural"
    return "standard"


def normalize_move_payload(raw_payload, id_key):
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")

    target_deck = raw_payload.get("target_deck")
    if not isinstance(target_deck, str) or not target_deck.strip():
        raise ValueError("invalid_target_deck")

    dry_run = raw_payload.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise ValueError("invalid_dry_run")

    return {
        id_key: normalize_id_list(raw_payload.get(id_key), id_key),
        "target_deck": target_deck.strip(),
        "dry_run": dry_run,
        "respect_ideal_deck": normalize_optional_bool(raw_payload, "respect_ideal_deck", True),
        "force": normalize_optional_bool(raw_payload, "force", False),
        "mark_ideal": normalize_optional_bool(raw_payload, "mark_ideal", False),
        "add_tags": normalize_add_tags(raw_payload.get("add_tags", [])),
    }


def normalize_mark_ideal_payload(raw_payload, id_key):
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")

    deck_name = raw_payload.get("deck_name")
    if not isinstance(deck_name, str) or not deck_name.strip():
        raise ValueError("invalid_deck_name")

    return {
        id_key: normalize_id_list(raw_payload.get(id_key), id_key),
        "deck_name": deck_name.strip(),
    }


def normalize_check_ideal_payload(raw_payload):
    if raw_payload is None:
        raw_payload = {}
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")

    card_ids = raw_payload.get("card_ids", [])
    note_ids = raw_payload.get("note_ids", [])
    if not isinstance(card_ids, list) or not isinstance(note_ids, list):
        raise ValueError("invalid_ideal_status_ids")
    if not card_ids and not note_ids:
        raise ValueError("ideal_status_ids_empty")

    normalized = {
        "card_ids": normalize_id_list(card_ids, "card_ids") if card_ids else [],
        "note_ids": normalize_id_list(note_ids, "note_ids") if note_ids else [],
    }
    return normalized


def normalize_string_field(raw_payload, key):
    value = raw_payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid_{key}")
    return value.strip()


def normalize_create_note_fields(raw_fields):
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise ValueError("invalid_fields")
    fields = {}
    for key, value in raw_fields.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("invalid_field_name")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError("invalid_field_value")
        fields[key.strip()] = value
    return fields


def normalize_create_note_tags(raw_tags):
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


def normalize_create_note_payload(raw_payload, include_dry_run=True):
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")
    raw_tags = raw_payload["tags"] if "tags" in raw_payload else None
    result = {
        "deck_name": normalize_string_field(raw_payload, "deck_name"),
        "model_name": normalize_string_field(raw_payload, "model_name"),
        "fields": normalize_create_note_fields(raw_payload.get("fields")),
        "tags": normalize_create_note_tags(raw_tags),
        "allow_duplicate": normalize_optional_bool(raw_payload, "allow_duplicate", False),
    }
    if include_dry_run:
        result["dry_run"] = normalize_optional_bool(raw_payload, "dry_run", True)
    return result


def normalize_create_notes_payload(raw_payload):
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")
    raw_notes = raw_payload.get("notes")
    if not isinstance(raw_notes, list) or not raw_notes:
        raise ValueError("invalid_notes")
    if len(raw_notes) > 20:
        raise ValueError("too_many_notes")
    return {
        "notes": [
            normalize_create_note_payload(raw_note, include_dry_run=False)
            for raw_note in raw_notes
        ],
        "dry_run": normalize_optional_bool(raw_payload, "dry_run", True),
    }


def normalize_replace_note_tags_payload(raw_payload):
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")
    remove_tags = normalize_add_tags(raw_payload.get("remove_tags", []))
    add_tags = normalize_add_tags(raw_payload.get("add_tags", []))
    if not remove_tags and not add_tags:
        raise ValueError("replace_note_tags_noop")
    return {
        "note_ids": normalize_id_list(raw_payload.get("note_ids"), "note_ids"),
        "remove_tags": remove_tags,
        "add_tags": add_tags,
        "dry_run": normalize_optional_bool(raw_payload, "dry_run", True),
    }


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


def normalize_update_note_fields_payload(raw_payload):
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")
    updates_id = raw_payload.get("updates_id")
    if updates_id:
        updates_id = normalize_note_field_updates_id(updates_id, required=True)
        updates = load_note_field_updates(updates_id)
        if updates is None:
            raise ValueError("note_field_updates_not_found")
        dry_run = normalize_optional_bool(raw_payload, "dry_run", True)
        if not dry_run:
            missing_note_ids = [
                item.get("note_id")
                for item in updates.get("note_updates", [])
                if not isinstance(item, dict) or not item.get("expected_content_hash")
            ]
            if missing_note_ids:
                raise ValueError(
                    "missing_note_precondition: every note update in apply v2 "
                    "requires top-level expected_content_hash (optional companions: "
                    "expected_mod, expected_usn, expected_model_id); "
                    f"note_ids={missing_note_ids}"
                )
        return {
            "updates_id": updates_id,
            "updates_sha256": updates.get("sha256", ""),
            "note_updates_count": int(updates.get("note_updates_count") or 0),
            "note_ids_count": int(updates.get("note_ids_count") or 0),
            "dry_run": dry_run,
        }
    raw_updates = raw_payload.get("note_updates")
    if not isinstance(raw_updates, list) or not raw_updates:
        raise ValueError("invalid_note_updates")
    if len(raw_updates) > 50:
        raise ValueError("too_many_note_updates")

    updates = []
    seen = set()
    for index, raw_update in enumerate(raw_updates):
        if not isinstance(raw_update, dict):
            raise ValueError("invalid_note_update")
        note_id = raw_update.get("note_id")
        if isinstance(note_id, bool) or not isinstance(note_id, int):
            raise ValueError("invalid_note_id")
        if note_id in seen:
            raise ValueError("duplicate_note_id")
        seen.add(note_id)

        raw_fields = raw_update.get("fields")
        if not isinstance(raw_fields, dict) or not raw_fields:
            raise ValueError("invalid_fields")
        fields = {}
        field_normalization = {}
        for field_name, value in raw_fields.items():
            if not isinstance(field_name, str) or not field_name.strip():
                raise ValueError("invalid_field_name")
            if not isinstance(value, str):
                raise ValueError("invalid_field_value")
            normalized_value, normalization_stats = normalize_visual_html(value)
            normalized_field_name = field_name.strip()
            fields[normalized_field_name] = normalized_value
            field_normalization[normalized_field_name] = normalization_stats

        nested_precondition_keys = (
            "precondition",
            "note_precondition",
            "preconditions",
            "expected_fields",
            "expected_field_values",
            "before_fields",
        )
        supplied_nested_key = next(
            (key for key in nested_precondition_keys if key in raw_update),
            None,
        )
        if supplied_nested_key is not None:
            raise ValueError(
                "invalid_note_precondition_format: do not use "
                f"{supplied_nested_key}; expected top-level keys "
                "expected_content_hash, expected_mod, expected_usn, "
                "expected_model_id"
            )

        expected_content_hash = raw_update.get("expected_content_hash")
        if expected_content_hash is not None and (
            not isinstance(expected_content_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_content_hash)
        ):
            raise ValueError("invalid_expected_content_hash")
        for key in ("expected_mod", "expected_usn", "expected_model_id"):
            value = raw_update.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"invalid_{key}")

        updates.append({
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

    dry_run = normalize_optional_bool(raw_payload, "dry_run", True)
    if not dry_run:
        missing_note_ids = [
            item["note_id"] for item in updates if not item.get("expected_content_hash")
        ]
        if missing_note_ids:
            raise ValueError(
                "missing_note_precondition: every note update in apply v2 "
                "requires top-level expected_content_hash (optional companions: "
                "expected_mod, expected_usn, expected_model_id); "
                f"note_ids={missing_note_ids}"
            )
    return {
        "note_updates": updates,
        "dry_run": dry_run,
    }


def normalize_note_field_updates_id(raw_updates_id, required=False):
    if raw_updates_id is None or raw_updates_id == "":
        if required:
            raise ValueError("invalid_updates_id")
        return None
    if not isinstance(raw_updates_id, str) or not raw_updates_id.strip():
        raise ValueError("invalid_updates_id")
    updates_id = raw_updates_id.strip()
    if note_field_updates_path(updates_id) is None:
        raise ValueError("invalid_updates_id")
    return updates_id


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


def load_note_field_updates(updates_id):
    path = note_field_updates_path(updates_id)
    if path is None or not path.exists() or not path.is_file():
        return None
    return load_json(path)


def normalize_note_field_updates_create_payload(raw_payload):
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")
    dry_run_operation_id = raw_payload.get("dry_run_operation_id")
    raw_json = raw_payload.get("note_updates_json")
    if dry_run_operation_id and raw_json:
        raise ValueError("choose_note_updates_json_or_dry_run_operation_id")
    if dry_run_operation_id:
        return note_field_updates_from_dry_run_operation(
            dry_run_operation_id,
            requested_by=optional_string(raw_payload, "requested_by"),
            reason=optional_string(raw_payload, "reason"),
        )
    if not isinstance(raw_json, str) or not raw_json.strip():
        raise ValueError("invalid_note_updates_json")
    try:
        note_updates = json.loads(raw_json)
    except json.JSONDecodeError:
        raise ValueError("invalid_note_updates_json") from None
    normalized = normalize_update_note_fields_payload({
        "note_updates": note_updates,
        "dry_run": True,
    })
    updates = {
        "updates_id": note_field_updates_id(),
        "created_at": utc_now_iso(),
        "note_updates": normalized["note_updates"],
        "note_updates_count": len(normalized["note_updates"]),
        "note_ids_count": len({item["note_id"] for item in normalized["note_updates"]}),
        "requested_by": optional_string(raw_payload, "requested_by", "gpt") or "gpt",
        "reason": optional_string(raw_payload, "reason"),
    }
    updates["sha256"] = note_field_updates_sha256(updates)
    return updates


def note_field_updates_from_dry_run_operation(
    operation_id,
    requested_by=None,
    reason=None,
):
    _operation_path, operation = load_organization_operation(operation_id)
    if operation is None:
        raise ValueError("dry_run_operation_not_found")
    if operation.get("operation_type") != "update_note_fields":
        raise ValueError("dry_run_operation_type_mismatch")
    if operation.get("status") != "done":
        raise ValueError("dry_run_operation_not_successful")

    operation_payload = operation.get("payload")
    result = operation.get("result")
    if not isinstance(operation_payload, dict) or operation_payload.get("dry_run") is not True:
        raise ValueError("operation_is_not_update_note_fields_dry_run")
    if not isinstance(result, dict) or result.get("dry_run") is not True or result.get("errors"):
        raise ValueError("dry_run_result_not_reusable")

    source_updates_id = normalize_note_field_updates_id(
        operation_payload.get("updates_id"),
        required=False,
    )
    if source_updates_id:
        source_updates = load_note_field_updates(source_updates_id)
        if source_updates is None:
            raise ValueError("source_note_field_updates_not_found")
        source_sha256 = note_field_updates_sha256(source_updates)
        if source_updates.get("sha256") not in (None, "", source_sha256):
            raise ValueError("source_note_field_updates_sha256_mismatch")
        if operation_payload.get("updates_sha256") not in (None, "", source_sha256):
            raise ValueError("source_note_field_updates_sha256_mismatch")
        source_note_updates = source_updates.get("note_updates")
    else:
        source_note_updates = operation_payload.get("note_updates")

    raw_preconditions = result.get("apply_preconditions")
    if not isinstance(raw_preconditions, list) or not raw_preconditions:
        raise ValueError("dry_run_apply_preconditions_missing")
    preconditions_by_note_id = {}
    for item in raw_preconditions:
        if not isinstance(item, dict):
            raise ValueError("invalid_dry_run_apply_precondition")
        note_id = item.get("note_id")
        expected_hash = item.get("expected_content_hash")
        if (
            isinstance(note_id, bool)
            or not isinstance(note_id, int)
            or not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            or note_id in preconditions_by_note_id
        ):
            raise ValueError("invalid_dry_run_apply_precondition")
        preconditions_by_note_id[note_id] = {
            key: item[key]
            for key in (
                "expected_content_hash",
                "expected_mod",
                "expected_usn",
                "expected_model_id",
            )
            if key in item
        }

    if not isinstance(source_note_updates, list) or not source_note_updates:
        raise ValueError("source_note_field_updates_missing")
    source_note_ids = [
        item.get("note_id") for item in source_note_updates if isinstance(item, dict)
    ]
    if set(source_note_ids) != set(preconditions_by_note_id):
        raise ValueError("dry_run_apply_preconditions_note_ids_mismatch")

    conditioned_updates = [
        {**item, **preconditions_by_note_id[item["note_id"]]}
        for item in source_note_updates
    ]
    normalized = normalize_update_note_fields_payload({
        "note_updates": conditioned_updates,
        "dry_run": False,
    })
    updates = {
        "updates_id": note_field_updates_id(),
        "created_at": utc_now_iso(),
        "note_updates": normalized["note_updates"],
        "note_updates_count": len(normalized["note_updates"]),
        "note_ids_count": len({item["note_id"] for item in normalized["note_updates"]}),
        "requested_by": requested_by or operation.get("requested_by") or "gpt",
        "reason": reason if reason is not None else operation.get("reason"),
        "dry_run_operation_id": operation_id,
        "ready_for_apply_v2": True,
    }
    if source_updates_id:
        updates["source_updates_id"] = source_updates_id
    updates["sha256"] = note_field_updates_sha256(updates)
    return updates


def persist_note_field_updates(updates):
    path = note_field_updates_path(updates["updates_id"])
    if path is None:
        raise ValueError("invalid_updates_id")
    atomic_write_json(path, updates)
    note_ids = [item["note_id"] for item in updates.get("note_updates", [])]
    response = {
        "ok": True,
        "updates_id": updates["updates_id"],
        "note_updates_count": updates["note_updates_count"],
        "note_ids_count": updates["note_ids_count"],
        "sha256": updates["sha256"],
        "first_note_ids": note_ids[:5],
        "last_note_ids": note_ids[-5:],
    }
    for key in (
        "source_updates_id",
        "dry_run_operation_id",
        "ready_for_apply_v2",
    ):
        if key in updates:
            response[key] = updates[key]
    if updates.get("ready_for_apply_v2") is True:
        response["apply_updates_id"] = updates["updates_id"]
    return response


def normalize_get_note_type_fields_payload(raw_payload):
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")
    return {"model_name": normalize_string_field(raw_payload, "model_name")}


def normalize_get_reorganization_log_payload(raw_payload):
    if raw_payload is None:
        raw_payload = {}
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")

    limit = raw_payload.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("invalid_limit")
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    return {"limit": limit}


def normalize_undo_reorganization_payload(raw_payload):
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")

    batch_id = raw_payload.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ValueError("invalid_batch_id")
    if any(ch in batch_id for ch in "/\\"):
        raise ValueError("invalid_batch_id")
    return {"batch_id": batch_id.strip()}


def normalize_undo_last_reorganization_payload(raw_payload):
    if raw_payload is None:
        raw_payload = {}
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")
    return {}


def normalize_optional_id_list(raw_ids, key):
    if raw_ids is None:
        return []
    if isinstance(raw_ids, list) and not raw_ids:
        return []
    return normalize_id_list(raw_ids, key)


def normalize_unique_int_list(raw_ids, key, required=False):
    if raw_ids is None:
        if required:
            raise ValueError(f"invalid_{key}")
        return []
    if not isinstance(raw_ids, list) or (required and not raw_ids):
        raise ValueError(f"invalid_{key}")
    ids = []
    seen = set()
    duplicates = []
    for raw_id in raw_ids:
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            raise ValueError(f"invalid_{key}")
        if raw_id in seen:
            duplicates.append(raw_id)
        seen.add(raw_id)
        ids.append(raw_id)
    if duplicates:
        raise ValueError(f"duplicate_{key}: {duplicates[:20]}")
    return ids


def normalize_unique_int_list_or_csv(raw_ids, key, required=False):
    if isinstance(raw_ids, str):
        if not raw_ids.strip():
            if required:
                raise ValueError(f"invalid_{key}")
            return []
        raw_ids = [part.strip() for part in raw_ids.split(",")]
        parsed_ids = []
        for raw_id in raw_ids:
            if not raw_id:
                raise ValueError(f"invalid_{key}")
            try:
                parsed_ids.append(int(raw_id))
            except ValueError:
                raise ValueError(f"invalid_{key}") from None
        raw_ids = parsed_ids
    return normalize_unique_int_list(raw_ids, key, required=required)


def normalize_optional_positive_int(raw_payload, key, default=None, minimum=1, maximum=None):
    value = raw_payload.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid_{key}")
    if value < minimum:
        raise ValueError(f"invalid_{key}")
    if maximum is not None and value > maximum:
        raise ValueError(f"invalid_{key}")
    return value


def normalize_spacing_ms(raw_payload):
    value = raw_payload.get("spacing_ms", 60000)
    if value is None:
        value = 60000
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid_spacing_ms")
    if value < 1:
        raise ValueError("invalid_spacing_ms")
    if value > 60000:
        raise ValueError("invalid_spacing_ms")
    return value


def normalize_reorder_target_created_column(raw_payload):
    apply_note_created_date = normalize_optional_bool(raw_payload, "apply_note_created_date", False)
    if apply_note_created_date:
        return "note", True

    target_created_column = raw_payload.get("target_created_column", "card")
    if not isinstance(target_created_column, str):
        raise ValueError("unsupported_target_created_column")
    target_created_column = target_created_column.strip().casefold()
    if target_created_column not in {"card", "note"}:
        raise ValueError("unsupported_target_created_column")
    return target_created_column, target_created_column == "note"


def normalize_reorder_multi_card_note_policy(raw_payload, target_created_column):
    policy = raw_payload.get("multi_card_note_policy")
    if policy is None:
        return "group_if_all_new" if target_created_column == "note" else "single_card_only"
    if not isinstance(policy, str):
        raise ValueError("unsupported_multi_card_note_policy")
    policy = policy.strip().casefold()
    if policy not in {"single_card_only", "group_if_all_new"}:
        raise ValueError("unsupported_multi_card_note_policy")
    return policy


def normalize_reorder_order_id(raw_order_id, required=False):
    if raw_order_id is None or raw_order_id == "":
        if required:
            raise ValueError("invalid_order_id")
        return ""
    if not isinstance(raw_order_id, str) or not raw_order_id.strip():
        raise ValueError("invalid_order_id")
    order_id = raw_order_id.strip()
    if reorder_order_path(order_id) is None:
        raise ValueError("invalid_order_id")
    return order_id


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


def load_reorder_order(order_id):
    path = reorder_order_path(order_id)
    if path is None or not path.exists() or not path.is_file():
        return None
    return load_json(path)


def normalize_reorder_order_payload(raw_payload):
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")

    deck = raw_payload.get("deck")
    if not isinstance(deck, str) or not deck.strip():
        raise ValueError("deck_empty")

    target_created_column = raw_payload.get("target_created_column", "note")
    if not isinstance(target_created_column, str):
        raise ValueError("unsupported_target_created_column")
    target_created_column = target_created_column.strip().casefold()
    if target_created_column != "note":
        raise ValueError("unsupported_target_created_column")

    ordered_note_ids = normalize_unique_int_list_or_csv(
        raw_payload.get("ordered_note_ids"),
        "ordered_note_ids",
        required=True,
    )
    expected_eligible_card_ids = normalize_unique_int_list_or_csv(
        raw_payload.get("expected_eligible_card_ids", []),
        "expected_eligible_card_ids",
        required=False,
    )

    order = {
        "order_id": reorder_order_id(),
        "created_at": utc_now_iso(),
        "deck": deck.strip(),
        "target_created_column": target_created_column,
        "ordered_note_ids": ordered_note_ids,
        "ordered_note_ids_count": len(ordered_note_ids),
        "expected_eligible_card_ids": expected_eligible_card_ids,
        "expected_eligible_card_ids_count": len(expected_eligible_card_ids),
        "requested_by": optional_string(raw_payload, "requested_by", "gpt") or "gpt",
        "reason": optional_string(raw_payload, "reason"),
    }
    order["sha256"] = reorder_order_sha256(order)
    return order


def persist_reorder_order(order):
    path = reorder_order_path(order["order_id"])
    atomic_write_json(path, order)
    return {
        "ok": True,
        "order_id": order["order_id"],
        "deck": order["deck"],
        "target_created_column": order["target_created_column"],
        "ordered_note_ids_count": order["ordered_note_ids_count"],
        "expected_eligible_card_ids_count": order["expected_eligible_card_ids_count"],
        "sha256": order["sha256"],
        "first_ordered_note_ids": order["ordered_note_ids"][:5],
        "last_ordered_note_ids": order["ordered_note_ids"][-5:],
    }


def normalize_reorder_cards_by_material_payload(raw_payload):
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")

    deck = raw_payload.get("deck")
    if not isinstance(deck, str) or not deck.strip():
        raise ValueError("deck_empty")

    scope = raw_payload.get("scope", "currently_new_cards")
    if scope != "currently_new_cards":
        raise ValueError("unsupported_scope")

    base_datetime = raw_payload.get("base_datetime")
    if base_datetime is not None:
        if not isinstance(base_datetime, str):
            raise ValueError("invalid_base_datetime")
        base_datetime = base_datetime.strip() or None

    material_context = raw_payload.get("material_context", "")
    if material_context is not None and not isinstance(material_context, str):
        raise ValueError("invalid_material_context")

    ordering_notes = raw_payload.get("ordering_notes", "")
    if ordering_notes is not None and not isinstance(ordering_notes, str):
        raise ValueError("invalid_ordering_notes")

    order_id = normalize_reorder_order_id(raw_payload.get("order_id"))
    target_payload = raw_payload
    if order_id and "target_created_column" not in raw_payload:
        target_payload = {**raw_payload, "target_created_column": "note"}
    target_created_column, apply_note_created_date = normalize_reorder_target_created_column(target_payload)
    multi_card_note_policy = normalize_reorder_multi_card_note_policy(raw_payload, target_created_column)
    ordered_card_ids = normalize_optional_id_list(raw_payload.get("ordered_card_ids"), "ordered_card_ids")
    ordered_note_ids = normalize_optional_id_list(raw_payload.get("ordered_note_ids"), "ordered_note_ids")

    payload = {
        "deck": deck.strip(),
        "dry_run": normalize_optional_bool(raw_payload, "dry_run", True),
        "scope": scope,
        "apply_created_date": normalize_optional_bool(raw_payload, "apply_created_date", True),
        "target_created_column": target_created_column,
        "apply_note_created_date": apply_note_created_date,
        "multi_card_note_policy": multi_card_note_policy,
        "base_datetime": base_datetime,
        "spacing_ms": normalize_spacing_ms(raw_payload),
        "ordered_card_ids": ordered_card_ids,
        "ordered_note_ids": ordered_note_ids,
        "expected_eligible_count": normalize_optional_positive_int(
            raw_payload, "expected_eligible_count", None, minimum=0
        ),
        "expected_eligible_card_ids": normalize_optional_id_list(
            raw_payload.get("expected_eligible_card_ids"), "expected_eligible_card_ids"
        ),
        "require_global_order": normalize_optional_bool(raw_payload, "require_global_order", True),
        "material_context": material_context or "",
        "ordering_notes": ordering_notes or "",
    }
    if order_id:
        order = load_reorder_order(order_id)
        if order is None:
            raise ValueError("reorder_order_not_found")
        if order.get("deck") != payload["deck"]:
            raise ValueError("reorder_order_deck_mismatch")
        if order.get("target_created_column") != target_created_column:
            raise ValueError("reorder_order_target_created_column_mismatch")
        order_sha256 = reorder_order_sha256(order)
        if order.get("sha256") and order.get("sha256") != order_sha256:
            raise ValueError("reorder_order_sha256_mismatch")
        validation_payload = {
            **payload,
            "ordered_note_ids": order.get("ordered_note_ids", []),
            "expected_eligible_card_ids": order.get("expected_eligible_card_ids", []),
        }
        validate_global_note_reorder_payload(validation_payload)
        payload.pop("ordered_note_ids", None)
        payload.pop("expected_eligible_card_ids", None)
        payload["order_id"] = order_id
        payload["order_sha256"] = order_sha256
        payload["ordered_note_ids_count"] = int(order.get("ordered_note_ids_count") or len(order.get("ordered_note_ids", [])))
        payload["expected_eligible_card_ids_count"] = int(
            order.get("expected_eligible_card_ids_count") or len(order.get("expected_eligible_card_ids", []))
        )
        payload["global_order_validation"] = validation_payload.get("global_order_validation", {})
    else:
        validate_global_note_reorder_payload(payload)
    return payload


def normalize_organization_payload(operation_type, raw_payload):
    if operation_type == "create_deck":
        return normalize_create_deck_payload(raw_payload)
    if operation_type == "move_cards_to_deck":
        return normalize_move_payload(raw_payload, "card_ids")
    if operation_type == "move_notes_to_deck":
        return normalize_move_payload(raw_payload, "note_ids")
    if operation_type == "get_reorganization_log":
        return normalize_get_reorganization_log_payload(raw_payload)
    if operation_type == "undo_reorganization":
        return normalize_undo_reorganization_payload(raw_payload)
    if operation_type == "undo_last_reorganization":
        return normalize_undo_last_reorganization_payload(raw_payload)
    if operation_type == "mark_notes_as_ideal_deck":
        return normalize_mark_ideal_payload(raw_payload, "note_ids")
    if operation_type == "mark_cards_as_ideal_deck":
        return normalize_mark_ideal_payload(raw_payload, "card_ids")
    if operation_type == "check_ideal_deck_status":
        return normalize_check_ideal_payload(raw_payload)
    if operation_type == "list_note_types":
        return {}
    if operation_type == "get_note_type_fields":
        return normalize_get_note_type_fields_payload(raw_payload)
    if operation_type == "create_note":
        return normalize_create_note_payload(raw_payload, include_dry_run=True)
    if operation_type == "create_notes":
        return normalize_create_notes_payload(raw_payload)
    if operation_type == "replace_note_tags":
        return normalize_replace_note_tags_payload(raw_payload)
    if operation_type == "update_note_fields":
        return normalize_update_note_fields_payload(raw_payload)
    if operation_type == "reorder_cards_by_material":
        return normalize_reorder_cards_by_material_payload(raw_payload)
    raise ValueError("unsupported_operation_type")


def build_organization_operation(payload):
    if not isinstance(payload, dict):
        raise ValueError("invalid_payload")

    operation_type = payload.get("operation_type") or payload.get("operation")
    if operation_type not in ORGANIZATION_OPERATION_TYPES:
        raise ValueError("unsupported_operation_type")

    raw_operation_payload = payload.get("payload")
    if operation_type == "reorder_cards_by_material" and raw_operation_payload is None:
        raw_operation_payload = payload
    if raw_operation_payload is None:
        raw_operation_payload = {}
    if not isinstance(raw_operation_payload, dict):
        raise ValueError("invalid_payload")

    execution_mode = normalize_execution_mode(
        payload,
        raw_operation_payload,
        default=DEFAULT_EXECUTION_MODE,
    )
    if (
        execution_mode == "preview"
        and operation_type not in DRY_RUN_CAPABLE_OPERATION_TYPES
    ):
        raise ValueError(
            f"preview_not_supported_for_operation_type: {operation_type}"
        )
    normalized_input = dict(raw_operation_payload)
    normalized_input["execution_mode"] = execution_mode
    normalized_input["dry_run"] = execution_mode_dry_run(execution_mode)
    normalized_payload = normalize_organization_payload(
        operation_type,
        normalized_input,
    )
    normalized_payload["execution_mode"] = execution_mode
    normalized_payload["dry_run"] = execution_mode_dry_run(execution_mode)

    operation_id = organization_operation_id()
    timestamp = utc_now_iso()
    return {
        "operation_id": operation_id,
        "operation_type": operation_type,
        "operation_schema_version": 3,
        "execution_mode": execution_mode,
        "dry_run": execution_mode_dry_run(execution_mode),
        "risk_level": operation_risk_level(operation_type),
        "payload": normalized_payload,
        "requested_by": optional_string(payload, "requested_by", "gpt") or "gpt",
        "reason": optional_string(payload, "reason"),
        "created_at": timestamp,
        "status": "pending",
        "result": None,
        # Compatibility metadata only. Authorization is the authenticated
        # creation request itself; execution does not wait on this flag.
        "confirmed_by_user": payload.get("confirmed_by_user") is True,
    }


def build_organization_wrapper_operation(body, operation_type, payload_keys):
    if not isinstance(body, dict):
        raise ValueError("invalid_payload")

    payload = {}
    for key in payload_keys:
        if key in body:
            payload[key] = body[key]

    operation_request = {
        "operation_type": operation_type,
        "confirmed_by_user": body.get("confirmed_by_user"),
        "payload": payload,
        "requested_by": optional_string(body, "requested_by", "gpt") or "gpt",
        "reason": optional_string(body, "reason"),
    }
    for key in ("execution_mode", "dry_run"):
        if key in body:
            operation_request[key] = body[key]
    return build_organization_operation(operation_request)


REORDER_CARDS_BY_MATERIAL_PAYLOAD_KEYS = [
    "deck",
    "dry_run",
    "scope",
    "apply_created_date",
    "target_created_column",
    "apply_note_created_date",
    "multi_card_note_policy",
    "base_datetime",
    "spacing_ms",
    "order_id",
    "ordered_card_ids",
    "ordered_note_ids",
    "expected_eligible_count",
    "expected_eligible_card_ids",
    "require_global_order",
    "material_context",
    "ordering_notes",
]


def replace_safe_markup_token(text, token, html_class):
    open_marker = f"[{token}]"
    close_marker = f"[/{token}]"
    parts = []
    index = 0
    converted = False
    while True:
        start = text.find(open_marker, index)
        if start < 0:
            parts.append(text[index:])
            break
        end = text.find(close_marker, start + len(open_marker))
        if end < 0:
            parts.append(text[index:])
            break
        parts.append(text[index:start])
        inner = text[start + len(open_marker):end]
        parts.append(f'<span class="{html_class}">{inner}</span>')
        index = end + len(close_marker)
        converted = True
    return "".join(parts), converted


def convert_safe_cloze_markup(text):
    converted_any = False
    for token, html_class in (("kw", "kw"), ("hint", "hint")):
        text, converted = replace_safe_markup_token(text, token, html_class)
        converted_any = converted_any or converted
    return text, converted_any


def format_alternate_cloze_part(part):
    if part.startswith("kw:"):
        return f'<span class="kw">{part[3:]}</span>'
    if part.startswith("hint:"):
        return f'<span class="hint">{part[5:]}</span>'
    return part


def convert_alternate_cloze_markup(text):
    parts = []
    index = 0
    converted = False
    while True:
        start = text.find("[[", index)
        if start < 0:
            parts.append(text[index:])
            break
        end = text.find("]]", start + 2)
        if end < 0:
            parts.append(text[index:])
            break

        parts.append(text[index:start])
        raw = text[start + 2:end]
        fields = raw.split("|")
        cloze_id = fields[0] if fields else ""
        is_valid = (
            len(fields) in (2, 3)
            and len(cloze_id) > 1
            and cloze_id[0] == "c"
            and cloze_id[1:].isdigit()
            and fields[1] != ""
        )
        if not is_valid:
            parts.append(text[start:end + 2])
            index = end + 2
            continue

        answer = format_alternate_cloze_part(fields[1])
        replacement = f"{{{{{cloze_id}::{answer}"
        if len(fields) == 3 and fields[2] != "":
            replacement += f"::{format_alternate_cloze_part(fields[2])}"
        replacement += "}}"
        parts.append(replacement)
        index = end + 2
        converted = True

    return "".join(parts), converted


def build_create_cloze_note_operation(body):
    if not isinstance(body, dict):
        raise ValueError("invalid_payload")
    text = body.get("text")
    if not isinstance(text, str):
        raise ValueError("invalid_text")
    back_extra = body.get("back_extra", "")
    if not isinstance(back_extra, str):
        raise ValueError("invalid_back_extra")
    text, text_alternate_converted = convert_alternate_cloze_markup(text)
    back_extra, back_extra_alternate_converted = convert_alternate_cloze_markup(back_extra)
    alternate_cloze_converted = text_alternate_converted or back_extra_alternate_converted
    text, text_converted = convert_safe_cloze_markup(text)
    back_extra, back_extra_converted = convert_safe_cloze_markup(back_extra)
    safe_markup_converted = text_converted or back_extra_converted

    payload = {
        "deck_name": body.get("deck_name"),
        "model_name": "prettify-minimal-cloze",
        "fields": {
            "Text": text,
            "Back Extra": back_extra,
        },
    }
    for key in ("tags", "allow_duplicate", "execution_mode", "dry_run"):
        if key in body:
            payload[key] = body[key]

    operation = build_organization_operation({
        "operation_type": "create_note",
        "confirmed_by_user": body.get("confirmed_by_user"),
        "payload": payload,
        "requested_by": optional_string(body, "requested_by", "gpt") or "gpt",
        "reason": optional_string(body, "reason"),
    })
    operation["_action_log"] = {
        "safe_markup_converted": safe_markup_converted,
        "alternate_cloze_converted": alternate_cloze_converted,
    }
    return operation


def normalize_persisted_organization_operation(operation):
    if not isinstance(operation, dict):
        raise ValueError("invalid_operation")
    normalized = dict(operation)
    payload = dict(operation.get("payload") or {})
    operation_type = (
        operation.get("operation_type")
        or operation.get("type")
        or payload.get("operation_type")
    )
    schema_version = int(operation.get("operation_schema_version") or 1)
    legacy_default = (
        "preview"
        if schema_version < 3 and operation_type in DRY_RUN_CAPABLE_OPERATION_TYPES
        else "direct"
    )
    execution_mode = normalize_execution_mode(
        operation,
        payload,
        default=legacy_default,
    )
    normalized["execution_mode"] = execution_mode
    normalized["dry_run"] = execution_mode_dry_run(execution_mode)
    normalized.setdefault("risk_level", operation_risk_level(operation_type))
    payload["execution_mode"] = execution_mode
    payload["dry_run"] = execution_mode_dry_run(execution_mode)
    normalized["payload"] = payload
    return normalized


def persist_organization_operation(operation):
    operation = normalize_persisted_organization_operation(operation)
    operation_path = organization_operation_path(operation["operation_id"])
    atomic_write_json(operation_path, operation)
    return {
        "ok": True,
        "operation": operation,
        "operation_id": operation.get("operation_id"),
        "status": operation.get("status"),
        "default_execution_mode": DEFAULT_EXECUTION_MODE,
    }


def load_tagging_operations():
    if not TAGGING_OPERATIONS_DIR.exists():
        return []
    operations = []
    for path in sorted(TAGGING_OPERATIONS_DIR.glob("tagop-*.json")):
        if path.is_file():
            try:
                operations.append(load_json(path))
            except Exception:
                continue
    return operations


def load_tagging_operation(operation_id):
    path = tagging_operation_path(operation_id)
    if path is None or not path.exists() or not path.is_file():
        return None, None
    return path, load_json(path)


def load_organization_operations():
    if not ORGANIZATION_OPERATIONS_DIR.exists():
        return []
    operations = []
    for path in sorted(ORGANIZATION_OPERATIONS_DIR.glob("orgop-*.json")):
        if path.is_file():
            try:
                operations.append(
                    normalize_persisted_organization_operation(load_json(path))
                )
            except Exception:
                continue
    return operations


def load_organization_operation(operation_id):
    path = organization_operation_path(operation_id)
    if path is None or not path.exists() or not path.is_file():
        return None, None
    return path, normalize_persisted_organization_operation(load_json(path))


def normalize_recurrence_item(item):
    normalized = dict(item or {})
    normalized.setdefault("disciplina", "")
    normalized.setdefault("fase", "")
    normalized.setdefault("fonte", "")
    normalized.setdefault("tema", "")
    normalized.setdefault("peso_recorrencia", 0)
    normalized.setdefault("subtemas", [])
    normalized.setdefault("estrelas", 0)
    normalized.setdefault("aliases", [])
    return normalized


def iter_recurrence_items(index):
    items = index.get("items", []) if isinstance(index, dict) else []
    for item in items:
        if isinstance(item, dict):
            yield normalize_recurrence_item(item)


def recurrence_subtema_text(subtema):
    if isinstance(subtema, dict):
        return str(subtema.get("nome", "") or "")
    return str(subtema or "")


def recurrence_search_blob(item):
    parts = [
        str(item.get("disciplina", "") or ""),
        str(item.get("fase", "") or ""),
        str(item.get("tema", "") or ""),
        str(item.get("fonte", "") or ""),
    ]
    parts.extend(str(alias or "") for alias in item.get("aliases", []))
    parts.extend(recurrence_subtema_text(subtema) for subtema in item.get("subtemas", []))
    return " ".join(parts)


def filter_recurrence_items(items, disciplina=None, fase=None, tema=None):
    filtered = list(items)
    if disciplina:
        disciplina_norm = normalize_search_text(disciplina)
        filtered = [
            item for item in filtered
            if normalize_search_text(item.get("disciplina", "")) == disciplina_norm
        ]
    if fase:
        fase_norm = normalize_search_text(fase)
        filtered = [
            item for item in filtered
            if normalize_search_text(item.get("fase", "")) == fase_norm
        ]
    if tema:
        tema_norm = normalize_search_text(tema)
        filtered = [
            item for item in filtered
            if normalize_search_text(item.get("tema", "")) == tema_norm
        ]
    return filtered


def fo_area_from_relative_path(relative_path):
    path = Path(str(relative_path or ""))
    parts = path.parts
    return parts[0] if parts else ""


def normalize_fo_item(item):
    normalized = dict(item or {})
    normalized.setdefault("target_key", "")
    normalized.setdefault("group", "")
    normalized.setdefault("portal_subject", "")
    normalized.setdefault("output_subject", "")
    normalized.setdefault("material_type", "")
    normalized.setdefault("status", "")
    normalized.setdefault("file_name", "")
    normalized.setdefault("relative_path", "")
    normalized.setdefault("sha256", "")
    normalized.setdefault("byte_size", 0)
    normalized.setdefault("source_url", "")
    normalized.setdefault("source_label", "")
    normalized.setdefault("started_at", "")
    normalized.setdefault("finished_at", "")
    normalized.setdefault("error", "")
    normalized["area"] = fo_area_from_relative_path(normalized.get("relative_path"))
    return normalized


def iter_fo_items(manifest):
    items = manifest.get("items", []) if isinstance(manifest, dict) else []
    for item in items:
        if isinstance(item, dict):
            yield normalize_fo_item(item)


def fo_search_blob(item):
    return " ".join([
        str(item.get("target_key", "") or ""),
        str(item.get("group", "") or ""),
        str(item.get("area", "") or ""),
        str(item.get("portal_subject", "") or ""),
        str(item.get("output_subject", "") or ""),
        str(item.get("material_type", "") or ""),
        str(item.get("status", "") or ""),
        str(item.get("file_name", "") or ""),
        str(item.get("relative_path", "") or ""),
        str(item.get("source_label", "") or ""),
    ])


def validate_fo_relative_path(relative_path):
    rel = Path(str(relative_path or ""))
    if (
        not relative_path
        or rel.is_absolute()
        or any(part in ("", ".", "..") for part in rel.parts)
    ):
        raise ValueError("invalid_relative_path")
    return rel


def find_fo_item_by_relative_path(items, relative_path):
    for item in items:
        if item.get("relative_path") == relative_path:
            return item
    return None


def resolve_fo_pdf_path(item):
    relative_path = item.get("relative_path", "")
    rel = validate_fo_relative_path(relative_path)
    root = FO_MATERIALS_DIR.resolve()
    candidate = (root / rel).resolve()
    candidate.relative_to(root)
    return candidate


def parse_positive_int_param(params, key, default, maximum=None):
    value = parse_int(get_first(params, key, default), default)
    if value < 1:
        value = 1
    if maximum is not None and value > maximum:
        value = maximum
    return value


def parse_positive_int_param_alias(params, keys, default, maximum=None):
    for key in keys:
        value = get_first(params, key)
        if value is not None and value != "":
            return parse_positive_int_param(params, key, default, maximum=maximum)
    return parse_positive_int_param({keys[0]: [str(default)]}, keys[0], default, maximum=maximum)


def fo_material_metadata(item):
    return {
        "relative_path": item.get("relative_path", ""),
        "area": item.get("area", ""),
        "disciplina": item.get("output_subject") or item.get("portal_subject", ""),
        "portal_subject": item.get("portal_subject", ""),
        "output_subject": item.get("output_subject", ""),
        "tipo": item.get("material_type", ""),
        "arquivo": item.get("file_name", ""),
        "byte_size": item.get("byte_size", 0),
        "sha256": item.get("sha256", ""),
        "status": item.get("status", ""),
    }


def fo_transcript_compact_text(value):
    text = normalize_search_text(value)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def fo_transcript_compact_terms(value):
    return set(fo_transcript_compact_text(value).split())


def fo_fts_query(value):
    raw_value = str(value or "")
    parts = []
    for match in re.finditer(r'"([^"]+)"', raw_value):
        phrase_terms = [term for term in fo_transcript_compact_text(match.group(1)).split() if term]
        if phrase_terms:
            parts.append('"' + " ".join(phrase_terms) + '"')
    remainder = re.sub(r'"[^"]+"', " ", raw_value)
    terms = [term for term in fo_transcript_compact_text(remainder).split() if term]
    parts.extend(f'"{term}"*' for term in terms)
    if not parts:
        raise ValueError("missing_q")
    return " AND ".join(parts)


def search_fo_transcript_index(q, materia=None, frente=None, tipo=None, limit=50, offset=0):
    if not FO_SEARCH_INDEX_PATH.exists():
        return None
    connection = sqlite3.connect(f"file:{FO_SEARCH_INDEX_PATH.resolve()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("pragma integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("fo_search_index_invalid")
        where = ["transcripts_fts match ?"]
        values = [fo_fts_query(q)]
        for column, value in (("disciplina", materia), ("frente", frente), ("tipo", tipo)):
            if value:
                where.append(f"d.{column} = ?")
                values.append(value)
        where_sql = " and ".join(where)
        total = connection.execute(
            f"select count(*) from transcripts_fts join documents d using(path) where {where_sql}",
            values,
        ).fetchone()[0]
        rows = connection.execute(
            f"""
            select f.path, d.disciplina, d.frente, d.aula, d.title, d.tipo,
                   d.mtime_ns, d.size, d.sha256,
                   snippet(transcripts_fts, 6, '[', ']', ' … ', 32) as snippet_text,
                   bm25(transcripts_fts) as rank
            from transcripts_fts f join documents d using(path)
            where {where_sql}
            order by rank, f.path
            limit ? offset ?
            """,
            [*values, limit, offset],
        ).fetchall()
        items = []
        for row in rows:
            items.append({
                "metadata": {
                    "relative_path": row["path"],
                    "materia": row["disciplina"],
                    "frente": row["frente"],
                    "aula_number": int(row["aula"]) if str(row["aula"]).isdigit() else None,
                    "aula_title": row["title"],
                    "tipo": row["tipo"],
                    "chars": row["size"],
                    "exists": True,
                    "sha256": row["sha256"],
                },
                "rank": row["rank"],
                "matches": [{"snippet": " ".join(str(row["snippet_text"] or "").split())}],
            })
        return {"count": int(total), "items": items}
    finally:
        connection.close()


def connect_fo_transcripts_db():
    if not FO_TRANSCRIPTS_DB_PATH.exists():
        raise FileNotFoundError(str(FO_TRANSCRIPTS_DB_PATH))
    conn = sqlite3.connect(str(FO_TRANSCRIPTS_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def strip_fo_transcripts_prefix(relative_path):
    value = str(relative_path or "")
    if value.startswith(FO_TRANSCRIPTS_OUTPUT_PREFIX):
        return value[len(FO_TRANSCRIPTS_OUTPUT_PREFIX):]
    return value


def validate_fo_transcript_relative_path(relative_path):
    rel = Path(strip_fo_transcripts_prefix(relative_path))
    if (
        not str(relative_path or "")
        or rel.is_absolute()
        or any(part in ("", ".", "..") for part in rel.parts)
    ):
        raise ValueError("invalid_relative_path")
    return rel


def resolve_fo_transcript_path(relative_path):
    rel = validate_fo_transcript_relative_path(relative_path)
    root = FO_TRANSCRIPTS_ROOT.resolve()
    candidate = (root / rel).resolve()
    candidate.relative_to(root)
    return candidate


def fo_transcript_metadata(row):
    output_relative_path = row["output_relative_path"] or ""
    relative_path = strip_fo_transcripts_prefix(output_relative_path)
    try:
        transcript_path = resolve_fo_transcript_path(output_relative_path)
    except Exception:
        transcript_path = None
    exists = bool(transcript_path and transcript_path.exists() and transcript_path.is_file())
    chars = transcript_path.stat().st_size if exists else 0
    return {
        "id": row["id"],
        "relative_path": relative_path,
        "tipo": row["tipo"],
        "materia": row["materia"],
        "frente": row["frente"],
        "aula_number": row["aula_number"],
        "aula_title": row["aula_title"],
        "status": row["status"],
        "output_relative_path": output_relative_path,
        "remote_path": row["remote_path"],
        "updated_at": row["updated_at"],
        "chars": chars,
        "exists": exists,
    }


def fo_transcript_search_blob(row):
    return " ".join([
        str(row["id"] or ""),
        str(row["remote_path"] or ""),
        str(row["video_name"] or ""),
        str(row["aula_section"] or ""),
        str(row["aula_number"] or ""),
        str(row["aula_title"] or ""),
        str(row["tipo"] or ""),
        str(row["materia"] or ""),
        str(row["frente"] or ""),
        str(row["output_relative_path"] or ""),
        str(row["status"] or ""),
    ])


def iter_fo_transcript_rows(conn):
    return conn.execute(
        """
        SELECT *
        FROM transcription_queue
        ORDER BY
          CASE tipo
            WHEN 'Aulas Gerais' THEN 1
            WHEN 'Conteúdos Extras' THEN 2
            WHEN 'Específicas' THEN 3
            ELSE 4
          END,
          COALESCE(aula_number, 999999),
          COALESCE(materia, ''),
          COALESCE(frente, ''),
          COALESCE(aula_title, ''),
          id
        """
    ).fetchall()


def filter_fo_transcript_rows(rows, *, status_filter=None, exists_filter=None, materia=None, frente=None, tipo=None, aula_number=None, q=None):
    filtered = list(rows)
    if status_filter:
        status_norm = normalize_search_text(status_filter)
        filtered = [row for row in filtered if normalize_search_text(row["status"]) == status_norm]
    if materia:
        materia_norm = normalize_search_text(materia)
        filtered = [row for row in filtered if normalize_search_text(row["materia"]) == materia_norm]
    if frente:
        frente_norm = normalize_search_text(frente)
        filtered = [row for row in filtered if normalize_search_text(row["frente"]) == frente_norm]
    if tipo:
        tipo_norm = normalize_search_text(tipo)
        filtered = [row for row in filtered if normalize_search_text(row["tipo"]) == tipo_norm]
    if aula_number is not None:
        filtered = [row for row in filtered if row["aula_number"] == aula_number]
    if q:
        q_norm = normalize_search_text(q)
        q_compact = fo_transcript_compact_text(q)
        q_terms = [term for term in q_compact.split() if term]
        filtered = [
            row for row in filtered
            if (
                q_norm in normalize_search_text(fo_transcript_search_blob(row))
                or all(term in fo_transcript_compact_terms(fo_transcript_search_blob(row)) for term in q_terms)
            )
        ]
    if exists_filter is not None:
        filtered = [row for row in filtered if fo_transcript_metadata(row)["exists"] is exists_filter]
    return filtered


def find_fo_transcript_row(rows, *, transcript_id=None, relative_path=None, remote_path=None, q=None):
    if transcript_id is not None:
        for row in rows:
            if row["id"] == transcript_id:
                return row
        return None
    if relative_path:
        target = strip_fo_transcripts_prefix(relative_path)
        for row in rows:
            if strip_fo_transcripts_prefix(row["output_relative_path"]) == target:
                return row
            if row["remote_path"] == target:
                return row
        return None
    if remote_path:
        for row in rows:
            if row["remote_path"] == remote_path:
                return row
        return None
    if q:
        matches = filter_fo_transcript_rows(rows, status_filter=None, exists_filter=None, q=q)
        return matches[0] if matches else None
    return None


def read_fo_aulas_index_match(query):
    if not query or not FO_AULAS_INDEX_PATH.exists():
        return None
    q_compact = fo_transcript_compact_text(query)
    q_terms = [term for term in q_compact.split() if term]
    if not q_terms:
        return None
    try:
        import csv
        with FO_AULAS_INDEX_PATH.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            validate_aulas_index_header(reader.fieldnames)
            rows = list(reader)
    except (ContractError, OSError, UnicodeError, csv.Error):
        return None
    for row in rows:
        blob = " ".join(str(row.get(key, "")) for key in [
            "area", "portal_root", "portal_subject", "disciplina", "ordem",
            "nome_real_aula", "titulo_original", "source_ref", "status",
            "video_stream_url",
        ])
        blob_compact = fo_transcript_compact_text(blob)
        blob_terms = set(blob_compact.split())
        if all(term in blob_terms for term in q_terms):
            return row
    return None


def transcript_unavailable_response(query, *, relative_path=None, remote_path=None):
    lesson = read_fo_aulas_index_match(query or remote_path or relative_path)
    if lesson:
        return {
            "error": "fo_transcript_not_available",
            "availability": "lesson_indexed_but_transcript_not_in_queue",
            "query": query,
            "relative_path": relative_path,
            "remote_path": remote_path,
            "lesson": {
                "source_ref": lesson.get("source_ref"),
                "disciplina": lesson.get("disciplina"),
                "aula_number": lesson.get("ordem"),
                "aula_title": lesson.get("nome_real_aula"),
                "lesson_status": lesson.get("status"),
                "video_stream_url": lesson.get("video_stream_url"),
            },
        }
    return {
        "error": "fo_transcript_not_available",
        "availability": "not_indexed_in_transcription_queue",
        "query": query,
        "relative_path": relative_path,
        "remote_path": remote_path,
    }


def fo_pdf_watermarks():
    configured = [
        watermark.strip()
        for watermark in os.environ.get("FO_PDF_WATERMARKS", "").split("|")
        if watermark.strip()
    ]
    fallback = "LEONARDO CHAPIEWSKY 06935714974"
    if fallback not in configured:
        configured.append(fallback)
    return configured


def clean_extracted_pdf_text(text):
    cleaned = "" if text is None else str(text)
    removed_count = 0

    for watermark in fo_pdf_watermarks():
        tokens = [re.escape(token) for token in watermark.split() if token]
        if not tokens:
            continue
        pattern = r"\s+".join(tokens)
        cleaned, count = re.subn(pattern, " ", cleaned, flags=re.IGNORECASE)
        removed_count += count

    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(), removed_count


def extract_fo_pdf_text(pdf_path, start_page=1, page_limit=5, max_chars=20000):
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:
        raise RuntimeError("pypdf_not_installed") from exc

    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)
    start_index = max(start_page - 1, 0)
    end_index = min(start_index + page_limit, total_pages)

    pages = []
    chars_used = 0
    truncated = False
    removed_watermark_count = 0

    for index in range(start_index, end_index):
        raw_text = reader.pages[index].extract_text() or ""
        text = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
        text, page_removed_watermark_count = clean_extracted_pdf_text(text)
        removed_watermark_count += page_removed_watermark_count

        remaining = max_chars - chars_used
        if remaining <= 0:
            truncated = True
            break
        if len(text) > remaining:
            text = text[:remaining].rstrip()
            truncated = True

        chars_used += len(text)
        pages.append({
            "page": index + 1,
            "text": text,
            "char_count": len(text),
        })

        if truncated:
            break

    joined_text = "\n\n".join(
        f"[page {page['page']}]\n{page['text']}"
        for page in pages
        if page.get("text")
    )

    return {
        "backend": "pypdf",
        "total_pages": total_pages,
        "page_start": start_index + 1,
        "page_limit": page_limit,
        "pages_returned": len(pages),
        "next_page": (pages[-1]["page"] + 1) if pages and pages[-1]["page"] < total_pages and not truncated else None,
        "truncated": truncated,
        "text_char_count": len(joined_text),
        "removed_watermark_count": removed_watermark_count,
        "text": joined_text,
        "pages": pages,
    }


def extract_fo_material_text(item, params):
    pdf_path = resolve_fo_pdf_path(item)
    if not pdf_path.exists() or not pdf_path.is_file():
        raise FileNotFoundError(str(pdf_path))

    start_page = parse_positive_int_param_alias(params, ("start_page", "page"), 1)
    page_limit = parse_positive_int_param_alias(params, ("page_limit", "limit"), 5, maximum=20)
    max_chars = parse_positive_int_param(params, "max_chars", 20000, maximum=100000)
    return extract_fo_pdf_text(
        pdf_path,
        start_page=start_page,
        page_limit=page_limit,
        max_chars=max_chars,
    )


def iter_notes(notes_index):
    if isinstance(notes_index, dict):
        for note_id, note in notes_index.items():
            if isinstance(note, dict):
                yield normalize_note(str(note_id), note)
    elif isinstance(notes_index, list):
        for note in notes_index:
            if isinstance(note, dict):
                nid = str(note.get("note_id", ""))
                yield normalize_note(nid, note)


def deck_name_from_item(deck):
    if not isinstance(deck, dict):
        return ""
    return str(deck.get("deck_name") or deck.get("name") or "")


def derive_decks_from_notes(notes):
    by_name = {}
    for note in notes:
        cards = note_real_cards(note)
        if cards:
            for card in cards:
                name = card.get("deck_name") or note.get("deck") or ""
                if not name:
                    continue
                deck = by_name.setdefault(name, {
                    "deck_name": name,
                    "name": name,
                    "deck_id": card.get("deck_id"),
                    "id": card.get("deck_id"),
                    "root_deck": name.split("::", 1)[0],
                    "card_count": 0,
                    "note_ids": set(),
                    "derived_from_notes": True,
                })
                deck["card_count"] += 1
                if note.get("note_id") is not None:
                    deck["note_ids"].add(note.get("note_id"))
            continue

        name = note.get("deck") or ""
        if name:
            deck = by_name.setdefault(name, {
                "deck_name": name,
                "name": name,
                "deck_id": None,
                "id": None,
                "root_deck": name.split("::", 1)[0],
                "card_count": 0,
                "note_ids": set(),
                "derived_from_notes": True,
            })
            if note.get("note_id") is not None:
                deck["note_ids"].add(note.get("note_id"))

    decks = []
    for deck in by_name.values():
        note_ids = deck.pop("note_ids", set())
        deck["note_count"] = len(note_ids)
        deck.setdefault("subtree_card_count", deck.get("card_count", 0))
        deck.setdefault("subtree_note_count", deck.get("note_count", 0))
        decks.append(deck)
    return sorted(decks, key=deck_name_from_item)


def deck_details_from_state(decks_index, notes):
    decks = decks_index.get("decks", []) if isinstance(decks_index, dict) else []
    if isinstance(decks, list) and decks:
        return sorted(
            [deck for deck in decks if isinstance(deck, dict)],
            key=deck_name_from_item,
        )
    return derive_decks_from_notes(notes)


def snapshot_status_from_state(notes_index, note_media_index, decks_index, notes):
    status = load_snapshot_status()
    decks = deck_details_from_state(decks_index, notes)
    card_count = 0
    for note in notes:
        cards = note_real_cards(note)
        card_count += len(cards) if cards else 1

    out = dict(status) if isinstance(status, dict) else {}
    out.setdefault("generated_at", out.get("timestamp"))
    out.setdefault("total_decks", len(decks))
    out.setdefault("total_cards", card_count)
    out.setdefault("total_notes", len(notes))
    out.setdefault("snapshot_note_count", len(notes))
    out.update({
        "state_cache": STATE_CACHE.metrics(),
        "notes_index_mtime": datetime.fromtimestamp(NOTES_INDEX_PATH.stat().st_mtime, timezone.utc).isoformat()
        if NOTES_INDEX_PATH.exists() else None,
        "decks_index_mtime": datetime.fromtimestamp(DECKS_INDEX_PATH.stat().st_mtime, timezone.utc).isoformat()
        if DECKS_INDEX_PATH.exists() else None,
        "note_media_index_loaded": isinstance(note_media_index, dict),
        "deck_index_loaded": bool(decks),
    })
    return out


def unquote_deck_value(value):
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def parse_deck_query(q_raw):
    q = (q_raw or "").strip()
    if not q.casefold().startswith("deck:"):
        return None, q_raw
    return unquote_deck_value(q[5:]), ""


def int_or_none(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def normalize_deck_match_text(value):
    text = unicodedata.normalize("NFC", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text.casefold()


def normalize_result_kind(value):
    kind = str(value or "").strip().casefold()
    if kind in {"card", "cards"}:
        return "card"
    if kind in {"note", "notes"}:
        return "note"
    return None


def card_deck_id(card):
    return int_or_none(card.get("deck_id") if card.get("deck_id") is not None else card.get("did"))


def card_note_id(card, note):
    return int_or_none(card.get("note_id")) or note_id_int(note)


class SnapshotDeckResolver:
    def __init__(self, decks, notes):
        self.decks = [deck for deck in (decks or []) if isinstance(deck, dict)]
        self.deck_by_id = {}
        self.name_to_ids = {}
        self.normalized_name_to_ids = {}
        self.name_by_id = {}

        for deck in self.decks:
            deck_id = int_or_none(deck.get("deck_id") if deck.get("deck_id") is not None else deck.get("id"))
            deck_name = deck_name_from_item(deck)
            if deck_id is None or not deck_name:
                continue
            self.deck_by_id[deck_id] = deck
            self.name_by_id[deck_id] = deck_name
            self.name_to_ids.setdefault(deck_name, set()).add(deck_id)
            self.normalized_name_to_ids.setdefault(normalize_deck_match_text(deck_name), set()).add(deck_id)

        for note in notes:
            note_deck = note.get("deck", "")
            for card in note_real_cards(note):
                deck_id = card_deck_id(card)
                deck_name = card.get("deck_name") or note_deck
                if deck_id is None:
                    continue
                if deck_name and deck_id not in self.name_by_id:
                    self.name_by_id[deck_id] = deck_name
                    self.name_to_ids.setdefault(deck_name, set()).add(deck_id)
                    self.normalized_name_to_ids.setdefault(normalize_deck_match_text(deck_name), set()).add(deck_id)

    def resolve_exact_ids(self, deck):
        deck_name = normalize_deck_argument_for_snapshot(deck)
        if not deck_name:
            return None
        deck_id = int_or_none(deck_name)
        if deck_id is not None and deck_id in self.name_by_id:
            return {deck_id}
        if deck_name in self.name_to_ids:
            return set(self.name_to_ids[deck_name])
        normalized = normalize_deck_match_text(deck_name)
        ids = self.normalized_name_to_ids.get(normalized, set())
        if len(ids) == 1:
            return set(ids)
        return set()

    def resolve_tree_ids(self, deck):
        deck_name = normalize_deck_argument_for_snapshot(deck)
        if not deck_name:
            return None

        exact_ids = self.resolve_exact_ids(deck_name) or set()
        tree_ids = set(exact_ids)
        tree_prefix = deck_name + "::"
        normalized_name = normalize_deck_match_text(deck_name)

        for deck_id, current_name in self.name_by_id.items():
            if current_name == deck_name or current_name.startswith(tree_prefix):
                tree_ids.add(deck_id)

        if not tree_ids:
            normalized_prefix = normalized_name + "::"
            for deck_id, current_name in self.name_by_id.items():
                normalized_current = normalize_deck_match_text(current_name)
                if normalized_current == normalized_name or normalized_current.startswith(normalized_prefix):
                    tree_ids.add(deck_id)

        return tree_ids

    def resolve_root_ids(self, root):
        root_name = normalize_deck_argument_for_snapshot(root)
        if not root_name:
            return None
        root_prefix = root_name + "::"
        normalized_root = normalize_deck_match_text(root_name)
        normalized_prefix = normalized_root + "::"
        ids = set()
        for deck_id, current_name in self.name_by_id.items():
            normalized_current = normalize_deck_match_text(current_name)
            if (
                current_name == root_name
                or current_name.startswith(root_prefix)
                or normalized_current == normalized_root
                or normalized_current.startswith(normalized_prefix)
            ):
                ids.add(deck_id)
        return ids


def build_card(note, note_media_index):
    note_id = str(note.get("note_id"))
    media_info = note_media_index.get(note_id, {})
    cards = note_real_cards(note)
    deck_summary = note_deck_summary_from_cards(cards)

    resolved_media = media_info.get("resolved_media", [])
    external_media = media_info.get("external_media", [])
    broken_media = media_info.get("broken_media", [])

    item = {
        "note_id": note.get("note_id"),
        "deck": note.get("deck"),
        "root_deck": note.get("root_deck"),
        "note_type": note.get("note_type"),
        "kind": note.get("kind"),
        "tags": note.get("tags", []),
        "field_names": note.get("field_names", []),
        "raw_fields": note.get("fields", {}),
        "compare_text": note.get("compare_text", ""),
        "has_images": media_info.get("has_images", False),
        "resolved_media": resolved_media,
        "resolved_media_urls": [f"/media/{name}" for name in resolved_media],
        "external_media": external_media,
        "broken_media": broken_media,
        "cards": cards,
        "card_count": len(cards),
    }
    item.update(deck_summary)
    return item


def parse_id_list(params, key):
    raw_values = get_all(params, key)
    ids = []
    seen = set()
    for raw_value in raw_values:
        for part in str(raw_value or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except Exception:
                continue
            if value not in seen:
                seen.add(value)
                ids.append(value)
    return ids


def note_id_int(note):
    try:
        return int(note.get("note_id"))
    except Exception:
        return None


def normalize_real_card(card, note=None):
    normalized = dict(card or {})
    for key in (
        "card_id", "note_id", "ord", "deck_id", "did", "queue", "type", "due",
        "ivl", "factor", "reps", "lapses", "left", "odue", "odid", "flags",
        "mod", "usn",
    ):
        if key in normalized and normalized[key] is not None:
            try:
                normalized[key] = int(normalized[key])
            except Exception:
                normalized[key] = None

    if "card_id" not in normalized and "id" in normalized:
        normalized["card_id"] = normalized.get("id")
    if "note_id" not in normalized and note is not None:
        normalized["note_id"] = note.get("note_id")
    if "did" not in normalized and "deck_id" in normalized:
        normalized["did"] = normalized.get("deck_id")
    if "deck_id" not in normalized and "did" in normalized:
        normalized["deck_id"] = normalized.get("did")
    normalized.setdefault("deck_name", "")
    normalized.setdefault("template_name", None)
    normalized.setdefault("data", "")
    return normalized


def note_real_cards(note):
    cards = note.get("cards", [])
    if not isinstance(cards, list):
        return []
    return [
        normalize_real_card(card, note)
        for card in cards
        if isinstance(card, dict)
    ]


def notes_by_id_from_snapshot(notes):
    out = {}
    for note in notes:
        nid = note_id_int(note)
        if nid is not None:
            out[nid] = note
    return out


def cards_by_id_from_snapshot(notes):
    out = {}
    for note in notes:
        for card in note_real_cards(note):
            card_id = card.get("card_id")
            if isinstance(card_id, int):
                out[card_id] = (card, note)
    return out


def note_deck_summary_from_cards(cards):
    deck_names = []
    deck_ids = []
    for card in cards:
        deck_name = card.get("deck_name")
        deck_id = card.get("deck_id")
        if deck_name and deck_name not in deck_names:
            deck_names.append(deck_name)
        if deck_id is not None and deck_id not in deck_ids:
            deck_ids.append(deck_id)
    return {
        "card_deck_names": deck_names,
        "card_deck_ids": deck_ids,
        "cards_in_multiple_decks": len(deck_ids) > 1 or len(deck_names) > 1,
    }


def build_note_info(note):
    cards = note_real_cards(note)
    info = {
        "note_id": note.get("note_id"),
        "deck": note.get("deck"),
        "root_deck": note.get("root_deck"),
        "note_type": note.get("note_type"),
        "model": note.get("note_type"),
        "kind": note.get("kind"),
        "tags": note.get("tags", []),
        "field_names": note.get("field_names", []),
        "fields": note.get("fields", {}),
        "compare_text": note.get("compare_text", ""),
        "cards": cards,
        "card_count": len(cards),
        "cards_real_available": bool(cards),
    }
    info.update(note_deck_summary_from_cards(cards))
    return info


def build_materialized_card(card, note):
    normalized = normalize_real_card(card, note)
    note_id = normalized.get("note_id") or note.get("note_id")
    text = clean_note_text(note) or str(note.get("compare_text", "") or "")
    note_cards = note_real_cards(note)
    return {
        "card_id": normalized.get("card_id"),
        "note_id": note_id,
        "deck": normalized.get("deck_name") or note.get("deck", ""),
        "fields": note.get("fields", {}),
        "text": text,
        "clean_text": text,
        "compare_text": note.get("compare_text", ""),
        "created": datetime_string_from_ms_id(normalized.get("card_id")),
        "added": datetime_string_from_ms_id(normalized.get("card_id")),
        "status": card_status_label(normalized),
        "queue": normalized.get("queue"),
        "type": normalized.get("type"),
        "ord": normalized.get("ord"),
        "template_name": normalized.get("template_name"),
        "note_card_count": len(note_cards),
        "note_card_ids": [
            item.get("card_id")
            for item in note_cards
            if item.get("card_id") is not None
        ],
    }


def build_materialized_note(note):
    cards = note_real_cards(note)
    text = clean_note_text(note) or str(note.get("compare_text", "") or "")
    note_id = note.get("note_id")
    return {
        "note_id": note_id,
        "deck": note.get("deck", ""),
        "root_deck": note.get("root_deck", ""),
        "note_type": note.get("note_type", ""),
        "kind": note.get("kind", ""),
        "tags": note.get("tags", []),
        "field_names": note.get("field_names", []),
        "fields": note.get("fields", {}),
        "text": text,
        "clean_text": text,
        "compare_text": note.get("compare_text", ""),
        "created": datetime_string_from_ms_id(note_id),
        "added": datetime_string_from_ms_id(note_id),
        "card_count": len(cards),
        "card_ids": [
            card.get("card_id")
            for card in cards
            if card.get("card_id") is not None
        ],
        "cards": [build_materialized_card(card, note) for card in cards],
    }


def unique_notes_from_card_matches(matches):
    notes = []
    seen = set()
    for _card, note in matches:
        nid = note_id_int(note)
        key = nid if nid is not None else id(note)
        if key in seen:
            continue
        seen.add(key)
        notes.append(note)
    return notes


def build_endpoint_items_from_matches(matches, result_kind, note_media_index):
    if result_kind == "card":
        return [build_materialized_card(card, note) for card, note in matches]
    notes = unique_notes_from_card_matches(matches)
    if result_kind == "note":
        return [build_materialized_note(note) for note in notes]
    return [build_card(note, note_media_index) for note in notes]


def build_card_info(card, note):
    info = normalize_real_card(card, note)
    info["note"] = {
        "note_id": note.get("note_id"),
        "note_type": note.get("note_type"),
        "kind": note.get("kind"),
        "tags": note.get("tags", []),
        "deck": note.get("deck"),
        "root_deck": note.get("root_deck"),
        "field_names": note.get("field_names", []),
        "fields": note.get("fields", {}),
        "compare_text": note.get("compare_text", ""),
    }
    return info


def find_matching_cards_from_snapshot(notes, params, deck_resolver=None, normalized_note_search=None):
    q_raw = (get_first(params, "text") or get_first(params, "q") or "").strip()
    parsed_deck, parsed_q = parse_deck_query(q_raw)
    q = normalize_search_text(parsed_q) if parsed_q else ""
    deck = normalize_deck_argument_for_snapshot(get_first(params, "deck") or parsed_deck)
    root_filter = get_first(params, "root")
    prefix_filter = get_first(params, "prefix")
    tag = get_first(params, "tag")
    kind_filter = get_first(params, "kind")
    result_kind = normalize_result_kind(kind_filter)
    note_kind_filter = None if result_kind else kind_filter
    nid = parse_int(get_first(params, "nid"), None)
    card_id = parse_int(get_first(params, "card_id"), None)
    if deck_resolver is None:
        deck_resolver = SnapshotDeckResolver([], notes)
    deck_ids = deck_resolver.resolve_exact_ids(deck) if deck else None
    root_deck_ids = deck_resolver.resolve_root_ids(root_filter) if root_filter else None
    prefix_deck_ids = deck_resolver.resolve_tree_ids(prefix_filter) if prefix_filter else None

    matches = []
    for note in notes:
        if note_kind_filter and note.get("kind") != note_kind_filter:
            continue
        normalized_search_blob = (
            normalized_note_search.get(id(note))
            if q and normalized_note_search is not None
            else None
        )
        for card in note_real_cards(note):
            cid = card_deck_id(card)
            if root_deck_ids is not None and cid not in root_deck_ids:
                continue
            if prefix_deck_ids is not None and cid not in prefix_deck_ids:
                continue
            if not card_matches_search(
                card,
                note,
                q=q,
                deck=deck,
                deck_ids=deck_ids,
                tag=tag,
                nid=nid,
                card_id=card_id,
                normalized_search_blob=normalized_search_blob,
            ):
                continue
            matches.append((card, note))

    return matches, {
        "query": q_raw,
        "interpreted_text_query": parsed_q,
        "interpreted_deck_query": parsed_deck,
        "deck_filter": deck,
        "root_filter": root_filter,
        "prefix_filter": prefix_filter,
        "tag_filter": tag,
        "kind_filter": kind_filter,
        "result_kind": result_kind,
        "note_kind_filter": note_kind_filter,
        "nid_filter": nid,
        "card_id_filter": card_id,
        "deck_ids": sorted(deck_ids) if deck_ids is not None else None,
        "root_deck_ids": sorted(root_deck_ids) if root_deck_ids is not None else None,
        "prefix_deck_ids": sorted(prefix_deck_ids) if prefix_deck_ids is not None else None,
    }


def card_id_from_pair(pair):
    card, _note = pair
    card_id = card.get("card_id")
    return int(card_id) if isinstance(card_id, int) else 0


def note_id_from_pair(pair):
    _card, note = pair
    nid = note_id_int(note)
    return nid if nid is not None else 0


def query_ids_from_snapshot(notes, params, deck_resolver=None):
    matches, filters = find_matching_cards_from_snapshot(notes, params, deck_resolver)
    card_ids = []
    note_ids = []
    card_to_note = {}
    note_to_cards = {}
    seen_notes = set()

    for card, note in matches:
        card_id = card.get("card_id")
        note_id = card.get("note_id") or note.get("note_id")
        if not isinstance(card_id, int) or not isinstance(note_id, int):
            continue
        card_ids.append(card_id)
        card_to_note[str(card_id)] = note_id
        note_to_cards.setdefault(str(note_id), []).append(card_id)
        if note_id not in seen_notes:
            seen_notes.add(note_id)
            note_ids.append(note_id)

    return {
        **filters,
        "query_contract": "complete_id_scan_before_batch_materialization",
        "batching_rule": "Use batches only to materialize/read data; compute audit, normalization, and reorder only after consolidating every returned id.",
        "recommended_card_batch_size": DEFAULT_CARD_BATCH_SIZE,
        "max_materialize_batch_bytes": MAX_MATERIALIZE_BATCH_BYTES,
        "total_cards_found": len(card_ids),
        "total_notes_found": len(note_ids),
        "count": len(card_ids),
        "card_ids": card_ids,
        "note_ids": note_ids,
        "card_to_note": card_to_note,
        "note_to_cards": note_to_cards,
    }


def parse_materialize_id_params(params):
    card_ids = parse_id_list(params, "card_ids") or parse_id_list(params, "ids")
    note_ids = parse_id_list(params, "note_ids")
    return card_ids, note_ids


def materialize_batch_from_snapshot(notes, params):
    card_ids, note_ids = parse_materialize_id_params(params)
    batch_index = parse_int(get_first(params, "batch_index", 1), 1)
    batch_total = parse_int(get_first(params, "batch_total", 1), 1)
    target = (get_first(params, "target", "") or "").strip().casefold()
    if target not in {"", "card", "note"}:
        raise ValueError("invalid_target")
    if not card_ids and not note_ids:
        raise ValueError("missing_card_ids_or_note_ids")

    note_map = notes_by_id_from_snapshot(notes)
    card_map = cards_by_id_from_snapshot(notes)
    materialized_cards = []
    materialized_notes = []
    missing_card_ids = []
    missing_note_ids = []
    seen_notes = set()

    for card_id in card_ids:
        item = card_map.get(card_id)
        if item is None:
            missing_card_ids.append(card_id)
            continue
        card, note = item
        materialized_cards.append(build_materialized_card(card, note))
        nid = note_id_int(note)
        if nid is not None and nid not in seen_notes:
            seen_notes.add(nid)
            materialized_notes.append(build_materialized_note(note))

    for note_id in note_ids:
        if note_id in seen_notes:
            continue
        note = note_map.get(note_id)
        if note is None:
            missing_note_ids.append(note_id)
            continue
        seen_notes.add(note_id)
        materialized_notes.append(build_materialized_note(note))
        if target != "note":
            materialized_cards.extend(
                build_materialized_card(card, note)
                for card in note_real_cards(note)
            )

    return {
        "materialization_contract": "batch_read_only_no_logical_ordering",
        "global_consolidation_required": True,
        "batch_index": batch_index,
        "batch_total": batch_total,
        "requested_card_ids": card_ids,
        "requested_note_ids": note_ids,
        "materialized_cards": len(materialized_cards),
        "materialized_notes": len(materialized_notes),
        "missing_card_ids": missing_card_ids,
        "missing_note_ids": missing_note_ids,
        "card_to_note": {
            str(card["card_id"]): card["note_id"]
            for card in materialized_cards
            if card.get("card_id") is not None
        },
        "notes": materialized_notes,
        "cards": materialized_cards,
    }


def send_materialized_batch(handler, payload, max_bytes):
    body = json_bytes(payload)
    if max_bytes and len(body) > max_bytes:
        return send_json(handler, {
            "error": "materialized_batch_too_large",
            "message": "Lote excede ANKI_GPT_MAX_BATCH_BYTES; divida apenas a materializacao em lotes menores e nao gere reorder parcial.",
            "batch_index": payload.get("batch_index"),
            "batch_total": payload.get("batch_total"),
            "requested_card_ids": payload.get("requested_card_ids", []),
            "requested_note_ids": payload.get("requested_note_ids", []),
            "actual_bytes": len(body),
            "max_batch_bytes": max_bytes,
        }, status=413)
    return send_json_bytes(handler, body)


def deck_cards_and_notes_from_snapshot(deck):
    deck_name = normalize_deck_argument_for_snapshot(deck)
    notes_index, _note_media_index = load_state()
    notes = list(iter_notes(notes_index))
    deck_resolver = SnapshotDeckResolver(load_decks_index().get("decks", []), notes)
    deck_ids = deck_resolver.resolve_exact_ids(deck_name)
    card_ids = []
    note_ids = []
    card_to_note = {}
    seen_notes = set()
    for note in notes:
        note_deck = note.get("deck", "")
        matching_cards = []
        for card in note_real_cards(note):
            effective_deck = card.get("deck_name") or note_deck
            if (deck_ids is not None and card_deck_id(card) in deck_ids) or effective_deck == deck_name:
                matching_cards.append(card)
        if not matching_cards:
            continue
        nid = note_id_int(note)
        if nid is not None and nid not in seen_notes:
            seen_notes.add(nid)
            note_ids.append(nid)
        for card in matching_cards:
            cid = card.get("card_id")
            if isinstance(cid, int):
                card_ids.append(cid)
                if nid is not None:
                    card_to_note[str(cid)] = nid
    return {
        "deck": deck_name,
        "card_ids": card_ids,
        "note_ids": note_ids,
        "card_to_note": card_to_note,
    }


def validate_global_note_reorder_payload(payload):
    if payload.get("target_created_column") != "note":
        return
    if payload.get("require_global_order") is False:
        return

    ordered_note_ids = payload.get("ordered_note_ids")
    if not isinstance(ordered_note_ids, list) or not ordered_note_ids:
        raise ValueError("ordered_note_ids_required_for_global_note_reorder")

    seen = set()
    duplicates = []
    for note_id in ordered_note_ids:
        if note_id in seen:
            duplicates.append(note_id)
        seen.add(note_id)
    if duplicates:
        raise ValueError(f"duplicate_ordered_note_ids: {duplicates[:20]}")

    try:
        deck_scope = deck_cards_and_notes_from_snapshot(payload.get("deck", ""))
    except FileNotFoundError as exc:
        raise ValueError(f"reorder_global_validation_snapshot_unavailable: {exc}") from exc

    expected_note_ids = deck_scope["note_ids"]
    expected_set = set(expected_note_ids)
    ordered_set = set(ordered_note_ids)
    missing = [note_id for note_id in expected_note_ids if note_id not in ordered_set]
    extra = [note_id for note_id in ordered_note_ids if note_id not in expected_set]
    if missing:
        raise ValueError(
            "ordered_note_ids_not_global_for_deck: "
            f"missing_count={len(missing)} missing_note_ids={missing[:20]}"
        )
    if extra:
        raise ValueError(
            "ordered_note_ids_contain_notes_outside_deck: "
            f"extra_count={len(extra)} extra_note_ids={extra[:20]}"
        )

    payload["global_order_validation"] = {
        "validated": True,
        "deck": deck_scope["deck"],
        "total_cards_found": len(deck_scope["card_ids"]),
        "total_notes_found": len(expected_note_ids),
        "ordered_note_ids_count": len(ordered_note_ids),
        "first_ordered_note_ids": ordered_note_ids[:5],
        "last_ordered_note_ids": ordered_note_ids[-5:],
        "confirmation": "ordered_note_ids covers the complete deck note set from the snapshot; reorder is global after full consolidation.",
    }


def get_cards_info_from_snapshot(card_ids, notes):
    card_map = cards_by_id_from_snapshot(notes)
    cards = []
    missing = []
    for card_id in card_ids:
        item = card_map.get(card_id)
        if item is None:
            missing.append(card_id)
            continue
        card, note = item
        cards.append(build_card_info(card, note))
    return cards, missing


def get_notes_info_from_snapshot(note_ids, notes):
    note_map = notes_by_id_from_snapshot(notes)
    note_infos = []
    missing = []
    for note_id in note_ids:
        note = note_map.get(note_id)
        if note is None:
            missing.append(note_id)
            continue
        note_infos.append(build_note_info(note))
    return note_infos, missing


def cards_to_notes_from_snapshot(card_ids, notes):
    card_map = cards_by_id_from_snapshot(notes)
    mapping = {}
    missing = []
    for card_id in card_ids:
        item = card_map.get(card_id)
        if item is None:
            missing.append(card_id)
            continue
        card, note = item
        mapping[str(card_id)] = card.get("note_id") or note.get("note_id")
    return mapping, missing


def notes_to_cards_from_snapshot(note_ids, notes):
    note_map = notes_by_id_from_snapshot(notes)
    mapping = {}
    missing = []
    for note_id in note_ids:
        note = note_map.get(note_id)
        if note is None:
            missing.append(note_id)
            continue
        mapping[str(note_id)] = note_real_cards(note)
    return mapping, missing


def card_matches_search(
    card,
    note,
    q=None,
    deck=None,
    deck_ids=None,
    tag=None,
    nid=None,
    card_id=None,
    normalized_search_blob=None,
):
    if card_id is not None and card.get("card_id") != card_id:
        return False
    if nid is not None and note_id_int(note) != nid:
        return False
    if deck_ids is not None:
        if card_deck_id(card) not in deck_ids:
            return False
    elif deck:
        deck = normalize_deck_argument_for_snapshot(deck)
        note_deck = note.get("deck", "")
        card_deck = card.get("deck_name", "")
        effective_deck = card_deck or note_deck
        if deck != effective_deck:
            return False
    if tag and tag not in set(note.get("tags", [])):
        return False
    if q:
        if normalized_search_blob is None:
            normalized_search_blob = normalized_note_search_blob(note)
        if q not in normalized_search_blob:
            return False
    return True


def normalized_note_search_blob(note):
    blob = " ".join([
        str(note.get("compare_text", "") or ""),
        " ".join(str(value or "") for value in note.get("fields", {}).values()),
    ])
    return normalize_search_text(blob)


def load_normal_search_cache(notes, generation_id):
    key = (generation_id, id(notes))
    with NORMAL_SEARCH_CACHE_LOCK:
        if NORMAL_SEARCH_CACHE["key"] == key and NORMAL_SEARCH_CACHE["value"] is not None:
            NORMAL_SEARCH_CACHE["hits"] += 1
            return NORMAL_SEARCH_CACHE["value"]
        value = {id(note): normalized_note_search_blob(note) for note in notes}
        NORMAL_SEARCH_CACHE["key"] = key
        NORMAL_SEARCH_CACHE["value"] = value
        NORMAL_SEARCH_CACHE["misses"] += 1
        return value


def normal_search_cache_for_params(notes, params, generation_id):
    q_raw = (get_first(params, "text") or get_first(params, "q") or "").strip()
    _parsed_deck, parsed_q = parse_deck_query(q_raw)
    if not parsed_q:
        return None
    return load_normal_search_cache(notes, generation_id)


def note_matches_deck(note, deck, deck_resolver=None):
    if not deck:
        return True
    if deck_resolver is not None:
        deck_ids = deck_resolver.resolve_exact_ids(deck)
        if deck_ids is not None:
            return any(card_deck_id(card) in deck_ids for card in note_real_cards(note))
    deck = normalize_deck_argument_for_snapshot(deck)
    if note.get("deck", "") == deck:
        return True
    return any(card.get("deck_name", "") == deck for card in note_real_cards(note))


def note_matches_deck_prefix(note, prefix, deck_resolver=None):
    if not prefix:
        return True
    if deck_resolver is not None:
        deck_ids = deck_resolver.resolve_tree_ids(prefix)
        if deck_ids is not None:
            return any(card_deck_id(card) in deck_ids for card in note_real_cards(note))
    if note.get("deck", "").startswith(prefix):
        return True
    return any(card.get("deck_name", "").startswith(prefix) for card in note_real_cards(note))


def note_matches_root(note, root, deck_resolver=None):
    if not root:
        return True
    if deck_resolver is not None:
        deck_ids = deck_resolver.resolve_root_ids(root)
        if deck_ids is not None:
            return any(card_deck_id(card) in deck_ids for card in note_real_cards(note))
    if note.get("root_deck", "") == root:
        return True
    root_prefix = root + "::"
    return any(
        card.get("deck_name", "") == root or card.get("deck_name", "").startswith(root_prefix)
        for card in note_real_cards(note)
    )


def search_real_cards_from_snapshot(notes, params, deck_resolver=None, normalized_note_search=None):
    q_raw = (get_first(params, "text") or get_first(params, "q") or "").strip()
    parsed_deck, parsed_q = parse_deck_query(q_raw)
    q = normalize_search_text(parsed_q) if parsed_q else ""
    deck = normalize_deck_argument_for_snapshot(get_first(params, "deck") or parsed_deck)
    tag = get_first(params, "tag")
    nid = parse_int(get_first(params, "nid"), None)
    card_id = parse_int(get_first(params, "card_id"), None)
    limit = parse_int(get_first(params, "limit", 100), 100)
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    if deck_resolver is None:
        deck_resolver = SnapshotDeckResolver([], notes)
    deck_ids = deck_resolver.resolve_exact_ids(deck) if deck else None

    matches = []
    total = 0
    for note in notes:
        normalized_search_blob = (
            normalized_note_search.get(id(note))
            if q and normalized_note_search is not None
            else None
        )
        for card in note_real_cards(note):
            if not card_matches_search(
                card,
                note,
                q=q,
                deck=deck,
                deck_ids=deck_ids,
                tag=tag,
                nid=nid,
                card_id=card_id,
                normalized_search_blob=normalized_search_blob,
            ):
                continue
            total += 1
            if len(matches) < limit:
                matches.append(build_card_info(card, note))

    return {
        "query": q_raw,
        "interpreted_text_query": parsed_q,
        "interpreted_deck_query": parsed_deck,
        "deck_filter": deck,
        "deck_ids": sorted(deck_ids) if deck_ids is not None else None,
        "tag_filter": tag,
        "nid_filter": nid,
        "card_id_filter": card_id,
        "limit": limit,
        "count": total,
        "returned": len(matches),
        "cards": matches,
        "search_backend": "snapshot_card_fields",
        "limitations": [
            "Nao executa o parser nativo de busca do Anki.",
            "Busca textual usa compare_text e fields serializados no snapshot.",
            "Requer snapshot novo contendo cards reais por note.",
        ],
    }


def paginate(items, offset, limit):
    total = len(items)
    sliced = items[offset: offset + limit]
    return total, sliced


def load_derived_request_state():
    """Cache immutable request helpers for exactly one verified generation."""
    objects, manifest = STATE_CACHE.snapshot()
    key = (manifest.get("generation_id"), id(objects))
    with DERIVED_STATE_LOCK:
        if DERIVED_STATE_CACHE["key"] == key and DERIVED_STATE_CACHE["value"] is not None:
            DERIVED_STATE_CACHE["hits"] += 1
            return DERIVED_STATE_CACHE["value"]

        notes_index = objects["notes_index.json"]
        note_media_index = objects["note_media_index.json"]
        decks_index = objects["decks_index.json"]
        notes = list(iter_notes(notes_index))
        deck_details = deck_details_from_state(decks_index, notes)
        value = {
            "notes_index": notes_index,
            "note_media_index": note_media_index,
            "decks_index": decks_index,
            "notes": notes,
            "deck_details": deck_details,
            "deck_resolver": SnapshotDeckResolver(deck_details, notes),
            "snapshot_status": snapshot_status_from_state(
                notes_index,
                note_media_index,
                decks_index,
                notes,
            ),
            "generation_id": manifest.get("generation_id"),
        }
        DERIVED_STATE_CACHE["key"] = key
        DERIVED_STATE_CACHE["value"] = value
        DERIVED_STATE_CACHE["misses"] += 1
        return value


def operational_diagnostics():
    derived = load_derived_request_state()
    status = derived["snapshot_status"]
    generated_at = status.get("generated_at")
    snapshot_age_seconds = None
    if isinstance(generated_at, str):
        try:
            parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            snapshot_age_seconds = max(0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))
        except Exception:
            pass

    fts_documents = None
    fts_integrity = "missing"
    if FO_SEARCH_INDEX_PATH.is_file() and not FO_SEARCH_INDEX_PATH.is_symlink():
        connection = sqlite3.connect(f"file:{FO_SEARCH_INDEX_PATH.resolve()}?mode=ro", uri=True)
        try:
            fts_documents = int(connection.execute("select count(*) from documents").fetchone()[0])
            fts_integrity = str(connection.execute("pragma quick_check").fetchone()[0])
        finally:
            connection.close()

    disk = os.statvfs(str(STATE_DIR))
    with OBSERVABILITY_LOCK:
        observability = dict(OBSERVABILITY)
    return {
        "ok": fts_integrity == "ok",
        "api_version": API_VERSION,
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "module_hash": PROCESS_MODULE_HASH,
        "started_at": PROCESS_STARTED_AT,
        "uptime_seconds": int(time.monotonic() - PROCESS_STARTED_MONOTONIC),
        "generation_id": derived["generation_id"],
        "snapshot_generated_at": generated_at,
        "snapshot_age_seconds": snapshot_age_seconds,
        "total_deck_count": status.get("total_decks"),
        "total_card_count": status.get("total_cards"),
        "total_note_count": status.get("total_notes"),
        "fts_documents": fts_documents,
        "fts_integrity": fts_integrity,
        "state_cache": STATE_CACHE.metrics(),
        "derived_cache": {
            "generation_id": derived["generation_id"],
            "hits": DERIVED_STATE_CACHE["hits"],
            "misses": DERIVED_STATE_CACHE["misses"],
        },
        "normal_search_cache": {
            "generation_id": (
                NORMAL_SEARCH_CACHE["key"][0]
                if isinstance(NORMAL_SEARCH_CACHE["key"], tuple) and NORMAL_SEARCH_CACHE["value"] is not None
                else None
            ),
            "entries": len(NORMAL_SEARCH_CACHE["value"] or {}),
            "hits": NORMAL_SEARCH_CACHE["hits"],
            "misses": NORMAL_SEARCH_CACHE["misses"],
        },
        "tagging_operation_files": len(list(TAGGING_OPERATIONS_DIR.glob("*.json"))) if TAGGING_OPERATIONS_DIR.is_dir() else 0,
        "organization_operation_files": len(list(ORGANIZATION_OPERATIONS_DIR.glob("*.json"))) if ORGANIZATION_OPERATIONS_DIR.is_dir() else 0,
        "disk_free_bytes": int(disk.f_bavail * disk.f_frsize),
        "request_count": observability["request_count"],
        "last_error": observability["last_error"],
    }


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(REQUEST_SOCKET_TIMEOUT_SECONDS)

    def send_response(self, code, message=None):
        self._response_status = code
        return super().send_response(code, message)

    def end_headers(self):
        request_id = getattr(self, "_request_id", None)
        correlation_id = getattr(self, "_correlation_id", None)
        if request_id:
            self.send_header("X-Request-ID", request_id)
        if correlation_id:
            self.send_header("X-Correlation-ID", correlation_id)
        return super().end_headers()

    def handle_one_request(self):
        self._request_started_at = time.monotonic()
        self._response_status = None
        self._request_id = uuid.uuid4().hex
        self._correlation_id = self._request_id
        self._operation_id = None
        with OBSERVABILITY_LOCK:
            OBSERVABILITY["request_count"] += 1
        try:
            return super().handle_one_request()
        except Exception as exc:
            error = record_sanitized_error("request_unhandled", exc)
            log_server_event(
                "request_unhandled_exception",
                method=getattr(self, "command", None),
                path=urlparse(getattr(self, "path", "")).path,
                **error,
            )
            raise
        finally:
            started_at = getattr(self, "_request_started_at", None)
            duration_ms = None
            if started_at is not None:
                duration_ms = round((time.monotonic() - started_at) * 1000, 2)
            log_server_event(
                "request_end",
                method=getattr(self, "command", None),
                path=urlparse(getattr(self, "path", "")).path,
                status=getattr(self, "_response_status", None),
                duration_ms=duration_ms,
                content_length=request_content_length(self) if hasattr(self, "headers") else None,
                client=self.client_address[0] if self.client_address else None,
                request_id=getattr(self, "_request_id", None),
                correlation_id=getattr(self, "_correlation_id", None),
                generation_id=STATE_CACHE.metrics().get("generation_id"),
                operation_id=getattr(self, "_operation_id", None),
            )

    def log_request_start(self, path):
        incoming_correlation = valid_trace_identifier(self.headers.get("X-Correlation-ID"))
        if incoming_correlation:
            self._correlation_id = incoming_correlation
        log_server_event(
            "request_start",
            method=getattr(self, "command", None),
            path=path,
            content_length=request_content_length(self),
            client=self.client_address[0] if self.client_address else None,
            request_id=getattr(self, "_request_id", None),
            correlation_id=getattr(self, "_correlation_id", None),
            generation_id=STATE_CACHE.metrics().get("generation_id"),
        )

    def do_GET(self):
        try:
            self._extra_response_headers = None
            parsed = urlparse(self.path)
            path = parsed.path
            params = parse_qs(parsed.query, keep_blank_values=True)
            self.log_request_start(path)

            if path == "/health":
                return send_json(self, {
                    "ok": True,
                    "service": "anki-query-api",
                    "time": utc_now_iso(),
                })

            if path == "/version":
                return send_json(self, {
                    "service": "anki-query-api",
                    "api_version": API_VERSION,
                    "index_schema_version": INDEX_SCHEMA_VERSION,
                    "started_at": PROCESS_STARTED_AT,
                    "read_auth_required": REQUIRE_READ_AUTH,
                })

            if REQUIRE_READ_AUTH and path not in PUBLIC_READ_PATHS and not require_tagging_token(self):
                return

            if path in {"/ready", "/diagnostics"}:
                try:
                    diagnostics = operational_diagnostics()
                except Exception:
                    return send_json(self, {"ok": False, "error": "not_ready"}, status=503)
                if path == "/ready":
                    return send_json(self, {
                        "ok": diagnostics["ok"],
                        "api_version": diagnostics["api_version"],
                        "index_schema_version": diagnostics["index_schema_version"],
                        "generation_id": diagnostics["generation_id"],
                        "fts_integrity": diagnostics["fts_integrity"],
                    }, status=200 if diagnostics["ok"] else 503)
                return send_json(self, diagnostics, status=200 if diagnostics["ok"] else 503)

            if path.startswith("/media/"):
                raw_name = path[len("/media/"):]
                filename = unquote(raw_name)
                if (
                    "/" in filename
                    or "\x00" in filename
                    or len(filename.encode("utf-8")) > 255
                    or filename in ("", ".", "..")
                ):
                    return send_json(self, {"error": "invalid_media_path"}, status=400)
                media_root = MEDIA_DIR.resolve()
                unresolved = media_root / filename
                if unresolved.is_symlink():
                    return send_json(self, {"error": "media_not_found"}, status=404)
                candidate = unresolved.resolve()
                try:
                    candidate.relative_to(media_root)
                except ValueError:
                    return send_json(self, {"error": "invalid_media_path"}, status=400)
                return send_file(self, candidate)

            if path == "/debug/action-log":
                if not require_tagging_token(self):
                    return
                limit = parse_int(get_first(params, "limit", 50), 50)
                if limit < 1:
                    limit = 1
                if limit > 500:
                    limit = 500
                events = read_action_log_tail(limit)
                return send_json(self, {
                    "ok": True,
                    "limit": limit,
                    "count": len(events),
                    "events": events,
                })

            if path == "/tagging/operations":
                if not require_tagging_token(self):
                    return

                status_filter = get_first(params, "status", "pending_addon_execution")
                limit, offset = limit_offset(params)
                operations = load_tagging_operations()

                if status_filter and status_filter != "all":
                    operations = [
                        operation for operation in operations
                        if operation.get("status") == status_filter
                    ]

                operations.sort(key=lambda operation: operation.get("created_at", ""))
                total, sliced = paginate(operations, offset, limit)

                return send_json(self, {
                    "status_filter": status_filter,
                    "offset": offset,
                    "limit": limit,
                    "count": total,
                    "returned": len(sliced),
                    "operations": sliced,
                })

            if path == "/organization/operations":
                if not require_tagging_token(self):
                    return

                status_filter = get_first(params, "status", "pending")
                limit, offset = limit_offset(params)
                operations = load_organization_operations()

                if status_filter and status_filter != "all":
                    operations = [
                        operation for operation in operations
                        if operation.get("status") == status_filter
                    ]

                operations.sort(key=lambda operation: operation.get("created_at", ""))
                total, sliced = paginate(operations, offset, limit)

                return send_json(self, {
                    "status_filter": status_filter,
                    "default_execution_mode": DEFAULT_EXECUTION_MODE,
                    "offset": offset,
                    "limit": limit,
                    "count": total,
                    "returned": len(sliced),
                    "operations": sliced,
                })

            if path == "/organization/reorder-order-create":
                mark_deprecated_get(self, path, "/organization/reorder-order")
                log_action_event(self, path, "request_received")
                try:
                    if not require_tagging_token(self):
                        log_action_event(self, path, "auth_failed")
                        return
                    log_action_event(self, path, "auth_ok")
                    payload = {
                        "deck": get_first(params, "deck"),
                        "ordered_note_ids": get_first(params, "ordered_note_ids"),
                        "expected_eligible_card_ids": get_first(params, "expected_eligible_card_ids", ""),
                        "target_created_column": get_first(params, "target_created_column", "note"),
                        "requested_by": get_first(params, "requested_by", "gpt"),
                        "reason": get_first(params, "reason"),
                    }
                    order = normalize_reorder_order_payload(payload)
                    response = persist_reorder_order(order)
                except ValueError as exc:
                    log_action_event(self, path, "response_sent", status=400, error=str(exc))
                    return send_json(self, {"error": str(exc)}, status=400)
                log_action_event(self, path, "reorder_order_created", order_id=response.get("order_id"))
                log_action_event(self, path, "response_sent", status=200, order_id=response.get("order_id"))
                return send_json(self, response, status=200)

            if path == "/organization/reorder-cards-by-material-create":
                mark_deprecated_get(self, path, "/organization/reorder-cards-by-material")
                log_action_event(self, path, "request_received")
                try:
                    if not require_tagging_token(self):
                        log_action_event(self, path, "auth_failed")
                        return
                    log_action_event(self, path, "auth_ok")
                    order_id = get_first(params, "order_id")
                    if not order_id:
                        raise ValueError("invalid_order_id")
                    payload = {
                        "confirmed_by_user": True,
                        "deck": get_first(params, "deck"),
                        "order_id": order_id,
                        "requested_by": get_first(params, "requested_by", "gpt"),
                        "reason": get_first(params, "reason"),
                    }
                    execution_mode = get_first(params, "execution_mode")
                    if execution_mode is not None:
                        payload["execution_mode"] = execution_mode
                    for key in ("dry_run", "require_global_order", "apply_created_date"):
                        value = get_optional_query_bool(params, key)
                        if value is not None:
                            payload[key] = value
                    spacing_ms = get_optional_query_int(params, "spacing_ms")
                    if spacing_ms is not None:
                        payload["spacing_ms"] = spacing_ms
                    for key in ("scope", "target_created_column", "multi_card_note_policy"):
                        value = get_first(params, key)
                        if value is not None and value != "":
                            payload[key] = value
                    operation = build_organization_wrapper_operation(
                        payload,
                        "reorder_cards_by_material",
                        REORDER_CARDS_BY_MATERIAL_PAYLOAD_KEYS,
                    )
                except ValueError as exc:
                    log_action_event(self, path, "response_sent", status=400, error=str(exc))
                    return send_json(self, {"error": str(exc)}, status=400)
                response = persist_organization_operation(operation)
                log_action_event(
                    self,
                    path,
                    "operation_created",
                    operation_id=operation.get("operation_id"),
                    operation_type=operation.get("operation_type"),
                )
                log_action_event(self, path, "response_sent", status=200, operation_id=operation.get("operation_id"))
                return send_json(self, response, status=200)

            if path == "/organization/note-field-updates-create":
                mark_deprecated_get(self, path, "/organization/note-field-updates-create")
                log_action_event(self, path, "request_received")
                try:
                    if not require_tagging_token(self):
                        log_action_event(self, path, "auth_failed")
                        return
                    log_action_event(self, path, "auth_ok")
                    updates = normalize_note_field_updates_create_payload({
                        "note_updates_json": get_first(params, "note_updates_json"),
                        "requested_by": get_first(params, "requested_by", "gpt"),
                        "reason": get_first(params, "reason"),
                    })
                    response = persist_note_field_updates(updates)
                except ValueError as exc:
                    log_action_event(self, path, "response_sent", status=400, error=str(exc))
                    return send_json(self, {"error": str(exc)}, status=400)
                log_action_event(self, path, "note_field_updates_created", updates_id=response.get("updates_id"))
                log_action_event(self, path, "response_sent", status=200, updates_id=response.get("updates_id"))
                return send_json(self, response, status=200)

            if path == "/organization/note-field-updates":
                if not require_tagging_token(self):
                    return
                try:
                    updates_id = normalize_note_field_updates_id(get_first(params, "updates_id"), required=True)
                except ValueError as exc:
                    return send_json(self, {"error": str(exc)}, status=400)
                updates = load_note_field_updates(updates_id)
                if updates is None:
                    return send_json(self, {
                        "error": "note_field_updates_not_found",
                        "updates_id": updates_id,
                    }, status=404)
                return send_json(self, updates, status=200)

            if path == "/organization/update-note-fields-create":
                mark_deprecated_get(self, path, "/organization/update-note-fields-create")
                log_action_event(self, path, "request_received")
                try:
                    if not require_tagging_token(self):
                        log_action_event(self, path, "auth_failed")
                        return
                    log_action_event(self, path, "auth_ok")
                    updates_id = normalize_note_field_updates_id(get_first(params, "updates_id"), required=True)
                    if load_note_field_updates(updates_id) is None:
                        raise ValueError("note_field_updates_not_found")
                    payload = {
                        "confirmed_by_user": True,
                        "updates_id": updates_id,
                        "requested_by": get_first(params, "requested_by", "gpt"),
                        "reason": get_first(params, "reason"),
                    }
                    execution_mode = get_first(params, "execution_mode")
                    if execution_mode is not None:
                        payload["execution_mode"] = execution_mode
                    dry_run = get_optional_query_bool(params, "dry_run")
                    if dry_run is not None:
                        payload["dry_run"] = dry_run
                    operation = build_organization_wrapper_operation(
                        payload,
                        "update_note_fields",
                        ["updates_id", "dry_run"],
                    )
                except ValueError as exc:
                    log_action_event(self, path, "response_sent", status=400, error=str(exc))
                    return send_json(self, {"error": str(exc)}, status=400)
                response = persist_organization_operation(operation)
                log_action_event(
                    self,
                    path,
                    "operation_created",
                    operation_id=operation.get("operation_id"),
                    operation_type=operation.get("operation_type"),
                )
                log_action_event(self, path, "response_sent", status=200, operation_id=operation.get("operation_id"))
                return send_json(self, response, status=200)

            if path == "/organization/reorder-order":
                if not require_tagging_token(self):
                    return

                order_id = get_first(params, "order_id", "")
                try:
                    order_id = normalize_reorder_order_id(order_id, required=True)
                except ValueError as exc:
                    return send_json(self, {"error": str(exc)}, status=400)
                order = load_reorder_order(order_id)
                if order is None:
                    return send_json(self, {
                        "error": "reorder_order_not_found",
                        "order_id": order_id,
                    }, status=404)
                return send_json(self, order)

            if path.startswith("/ufpr/obras"):
                try:
                    if path == "/ufpr/obras/status":
                        return send_json(self, ufpr_status_response())

                    if path == "/ufpr/obras":
                        return send_json(self, ufpr_obras_response())

                    if path == "/ufpr/obras/index":
                        return send_json(self, ufpr_index_response(get_first(params, "categoria")))

                    if path == "/ufpr/obras/regra":
                        return send_json(self, ufpr_regra_response(get_first(params, "categoria")))

                    if path == "/ufpr/obras/trecho":
                        return send_json(self, ufpr_trecho_response(params))

                    return send_json(self, {"error": "not_found", "path": path}, status=404)
                except ValueError as exc:
                    return send_json(self, {"error": str(exc)}, status=400)
                except LookupError as exc:
                    return send_json(self, {"error": str(exc)}, status=404)
                except FileNotFoundError as exc:
                    return send_json(self, {"error": "ufpr_file_not_found"}, status=404)

            if path.startswith("/recurrence/"):
                try:
                    recurrence_index = load_recurrence_index()
                except FileNotFoundError:
                    return send_json(self, {"error": "recurrence_index_not_found"}, status=404)

                recurrence_items = list(iter_recurrence_items(recurrence_index))

                if path == "/recurrence/subjects":
                    by_subject = {}
                    for item in recurrence_items:
                        disciplina = item.get("disciplina", "")
                        fase = item.get("fase", "")
                        if not disciplina or not fase:
                            continue
                        subject = by_subject.setdefault(disciplina, {"disciplina": disciplina, "fases": set(), "count": 0})
                        subject["fases"].add(fase)
                        subject["count"] += 1

                    subjects = []
                    for subject in sorted(by_subject.values(), key=lambda value: value["disciplina"]):
                        subjects.append({
                            "disciplina": subject["disciplina"],
                            "fases": sorted(subject["fases"]),
                            "count": subject["count"],
                        })

                    return send_json(self, {
                        "schema_version": recurrence_index.get("schema_version"),
                        "count": len(subjects),
                        "subjects": subjects,
                    })

                if path == "/recurrence/by-discipline":
                    disciplina = get_first(params, "disciplina")
                    fase = get_first(params, "fase")
                    limit, offset = limit_offset(params)

                    if not disciplina:
                        return send_json(self, {"error": "missing_disciplina"}, status=400)

                    filtered = filter_recurrence_items(recurrence_items, disciplina=disciplina, fase=fase)
                    filtered.sort(key=lambda item: item.get("peso_recorrencia", 0), reverse=True)
                    total, sliced = paginate(filtered, offset, limit)

                    return send_json(self, {
                        "disciplina": disciplina,
                        "fase": fase,
                        "offset": offset,
                        "limit": limit,
                        "count": total,
                        "returned": len(sliced),
                        "items": sliced,
                    })

                if path == "/recurrence/by-theme":
                    disciplina = get_first(params, "disciplina")
                    tema = get_first(params, "tema")
                    fase = get_first(params, "fase")
                    limit, offset = limit_offset(params)

                    if not disciplina:
                        return send_json(self, {"error": "missing_disciplina"}, status=400)
                    if not tema:
                        return send_json(self, {"error": "missing_tema"}, status=400)

                    filtered = filter_recurrence_items(recurrence_items, disciplina=disciplina, fase=fase, tema=tema)
                    total, sliced = paginate(filtered, offset, limit)

                    return send_json(self, {
                        "disciplina": disciplina,
                        "fase": fase,
                        "tema": tema,
                        "offset": offset,
                        "limit": limit,
                        "count": total,
                        "returned": len(sliced),
                        "items": sliced,
                    })

                if path == "/recurrence/search":
                    q_raw = (get_first(params, "q", "") or "").strip()
                    q = normalize_search_text(q_raw)
                    limit, offset = limit_offset(params)

                    if not q:
                        return send_json(self, {"error": "missing_q"}, status=400)

                    filtered = [
                        item for item in recurrence_items
                        if q in normalize_search_text(recurrence_search_blob(item))
                    ]
                    filtered.sort(key=lambda item: item.get("peso_recorrencia", 0), reverse=True)
                    total, sliced = paginate(filtered, offset, limit)

                    return send_json(self, {
                        "query": q_raw,
                        "offset": offset,
                        "limit": limit,
                        "count": total,
                        "returned": len(sliced),
                        "items": sliced,
                    })

                return send_json(self, {"error": "not_found", "path": path}, status=404)

            if path == "/fo/transcripts":
                try:
                    with connect_fo_transcripts_db() as conn:
                        rows = iter_fo_transcript_rows(conn)
                except FileNotFoundError:
                    return send_json(self, {
                        "error": "fo_transcripts_queue_not_found",
                    }, status=404)

                try:
                    exists_filter = get_optional_query_bool(params, "exists")
                    aula_number_filter = get_optional_query_int(params, "aula_number")
                except ValueError as exc:
                    return send_json(self, {"error": str(exc)}, status=400)

                if "exists" not in params:
                    exists_filter = True

                status_filter = get_first(params, "status", "done")
                if status_filter == "":
                    status_filter = None
                materia_filter = get_first(params, "materia")
                frente_filter = get_first(params, "frente")
                tipo_filter = get_first(params, "tipo")
                q_raw = (get_first(params, "q", "") or "").strip()
                limit, offset = limit_offset(params)

                filtered = filter_fo_transcript_rows(
                    rows,
                    status_filter=status_filter,
                    exists_filter=exists_filter,
                    materia=materia_filter,
                    frente=frente_filter,
                    tipo=tipo_filter,
                    aula_number=aula_number_filter,
                    q=q_raw,
                )
                total, sliced = paginate(filtered, offset, limit)
                return send_json(self, {
                    "status_filter": status_filter,
                    "exists_filter": exists_filter,
                    "materia_filter": materia_filter,
                    "frente_filter": frente_filter,
                    "tipo_filter": tipo_filter,
                    "aula_number_filter": aula_number_filter,
                    "query": q_raw,
                    "offset": offset,
                    "limit": limit,
                    "count": total,
                    "returned": len(sliced),
                    "items": [fo_transcript_metadata(row) for row in sliced],
                })

            if path == "/fo/transcript":
                try:
                    transcript_id = get_optional_query_int(params, "id")
                except ValueError as exc:
                    return send_json(self, {"error": str(exc)}, status=400)

                relative_path = get_first(params, "relative_path")
                remote_path = get_first(params, "remote_path")
                q_raw = (get_first(params, "q", "") or "").strip()
                if transcript_id is None and not relative_path and not remote_path and not q_raw:
                    return send_json(self, {"error": "missing_transcript_locator"}, status=400)
                if relative_path:
                    try:
                        validate_fo_transcript_relative_path(relative_path)
                    except ValueError:
                        return send_json(self, {
                            "error": "invalid_relative_path",
                            "relative_path": relative_path,
                        }, status=400)

                try:
                    with connect_fo_transcripts_db() as conn:
                        rows = iter_fo_transcript_rows(conn)
                except FileNotFoundError:
                    return send_json(self, {
                        "error": "fo_transcripts_queue_not_found",
                    }, status=404)

                row = find_fo_transcript_row(
                    rows,
                    transcript_id=transcript_id,
                    relative_path=relative_path,
                    remote_path=remote_path,
                    q=q_raw,
                )
                if row is None:
                    return send_json(
                        self,
                        transcript_unavailable_response(q_raw, relative_path=relative_path, remote_path=remote_path),
                        status=404,
                    )

                metadata = fo_transcript_metadata(row)
                if row["status"] != "done":
                    return send_json(self, {
                        "error": "fo_transcript_not_generated",
                        "availability": row["status"],
                        "metadata": metadata,
                    }, status=404)
                if not metadata["exists"]:
                    return send_json(self, {
                        "error": "fo_transcript_file_not_found",
                        "availability": "queue_done_file_missing",
                        "metadata": metadata,
                    }, status=404)

                try:
                    transcript_path = resolve_fo_transcript_path(row["output_relative_path"])
                except ValueError:
                    return send_json(self, {
                        "error": "invalid_relative_path",
                        "relative_path": row["output_relative_path"],
                    }, status=400)

                max_chars = parse_positive_int_param(params, "max_chars", 20000, maximum=200000)
                text = transcript_path.read_text(encoding="utf-8", errors="replace")
                returned_text = text[:max_chars]
                return send_json(self, {
                    "metadata": metadata,
                    "max_chars": max_chars,
                    "truncated": len(text) > len(returned_text),
                    "chars_returned": len(returned_text),
                    "text": returned_text,
                })

            if path == "/fo/transcripts/search":
                q_raw = (get_first(params, "q", "") or "").strip()
                if not q_raw:
                    return send_json(self, {"error": "missing_q"}, status=400)

                materia_filter = get_first(params, "materia")
                frente_filter = get_first(params, "frente")
                tipo_filter = get_first(params, "tipo")
                limit, offset = limit_offset(params)
                try:
                    indexed = search_fo_transcript_index(
                        q_raw,
                        materia=materia_filter,
                        frente=frente_filter,
                        tipo=tipo_filter,
                        limit=limit,
                        offset=offset,
                    )
                except Exception as exc:
                    log_server_event(
                        "fo_search_index_fallback",
                        error_code=type(exc).__name__,
                    )
                    indexed = None
                if indexed is not None:
                    return send_json(self, {
                        "query": q_raw,
                        "materia_filter": materia_filter,
                        "frente_filter": frente_filter,
                        "tipo_filter": tipo_filter,
                        "offset": offset,
                        "limit": limit,
                        "count": indexed["count"],
                        "returned": len(indexed["items"]),
                        "items": indexed["items"],
                        "search_backend": "sqlite_fts5",
                    })

                try:
                    with connect_fo_transcripts_db() as conn:
                        rows = iter_fo_transcript_rows(conn)
                except FileNotFoundError:
                    return send_json(self, {
                        "error": "fo_transcripts_queue_not_found",
                    }, status=404)

                candidate_rows = filter_fo_transcript_rows(
                    rows,
                    status_filter="done",
                    exists_filter=True,
                    materia=materia_filter,
                    frente=frente_filter,
                    tipo=tipo_filter,
                )
                q_norm = normalize_search_text(q_raw)
                q_compact = fo_transcript_compact_text(q_raw)
                q_terms = [term for term in q_compact.split() if term]
                results = []
                for row in candidate_rows:
                    metadata = fo_transcript_metadata(row)
                    try:
                        transcript_path = resolve_fo_transcript_path(row["output_relative_path"])
                        text = transcript_path.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    text_norm = normalize_search_text(text)
                    text_compact = fo_transcript_compact_text(text)
                    if q_norm not in text_norm and not all(term in fo_transcript_compact_terms(text) for term in q_terms):
                        continue
                    pos = text_norm.find(q_norm)
                    if pos < 0:
                        pos = 0
                    snippet_start = max(0, pos - 180)
                    snippet_end = min(len(text), pos + max(len(q_raw), 1) + 260)
                    match_count = text_norm.count(q_norm) if q_norm else 0
                    if match_count == 0:
                        match_count = 1
                    results.append({
                        "metadata": metadata,
                        "match_count": match_count,
                        "matches": [{
                            "snippet": " ".join(text[snippet_start:snippet_end].split()),
                        }],
                    })

                total, sliced = paginate(results, offset, limit)
                return send_json(self, {
                    "query": q_raw,
                    "materia_filter": materia_filter,
                    "frente_filter": frente_filter,
                    "tipo_filter": tipo_filter,
                    "offset": offset,
                    "limit": limit,
                    "count": total,
                    "returned": len(sliced),
                    "items": sliced,
                    "search_backend": "filesystem_fallback",
                })

            if path == "/fo/materials":
                try:
                    manifest = load_fo_manifest()
                except FileNotFoundError:
                    return send_json(self, {"error": "fo_manifest_not_found"}, status=404)

                q_raw = (get_first(params, "q", "") or "").strip()
                q = normalize_search_text(q_raw)
                area_filter = get_first(params, "area")
                portal_subject_filter = get_first(params, "portal_subject")
                output_subject_filter = get_first(params, "output_subject")
                status_filter = get_first(params, "status")
                limit, offset = limit_offset(params)

                filtered = list(iter_fo_items(manifest))

                if area_filter:
                    area_norm = normalize_search_text(area_filter)
                    filtered = [
                        item for item in filtered
                        if normalize_search_text(item.get("area", "")) == area_norm
                    ]

                if portal_subject_filter:
                    portal_subject_norm = normalize_search_text(portal_subject_filter)
                    filtered = [
                        item for item in filtered
                        if normalize_search_text(item.get("portal_subject", "")) == portal_subject_norm
                    ]

                if output_subject_filter:
                    output_subject_norm = normalize_search_text(output_subject_filter)
                    filtered = [
                        item for item in filtered
                        if normalize_search_text(item.get("output_subject", "")) == output_subject_norm
                    ]

                if status_filter:
                    status_norm = normalize_search_text(status_filter)
                    filtered = [
                        item for item in filtered
                        if normalize_search_text(item.get("status", "")) == status_norm
                    ]

                if q:
                    filtered = [
                        item for item in filtered
                        if q in normalize_search_text(fo_search_blob(item))
                    ]

                total, sliced = paginate(filtered, offset, limit)
                return send_json(self, {
                    "generated_at": manifest.get("generated_at"),
                    "area_filter": area_filter,
                    "portal_subject_filter": portal_subject_filter,
                    "output_subject_filter": output_subject_filter,
                    "status_filter": status_filter,
                    "query": q_raw,
                    "offset": offset,
                    "limit": limit,
                    "count": total,
                    "returned": len(sliced),
                    "items": sliced,
                })

            if path == "/fo/material":
                try:
                    manifest = load_fo_manifest()
                except FileNotFoundError:
                    return send_json(self, {"error": "fo_manifest_not_found"}, status=404)

                relative_path = get_first(params, "relative_path", "")
                if not relative_path:
                    return send_json(self, {"error": "missing_relative_path"}, status=400)

                item = find_fo_item_by_relative_path(iter_fo_items(manifest), relative_path)
                if item is None:
                    return send_json(self, {
                        "error": "fo_material_not_found",
                        "relative_path": relative_path,
                    }, status=404)

                try:
                    extract_text = get_optional_query_bool(params, "extract_text")
                except ValueError as exc:
                    return send_json(self, {"error": str(exc)}, status=400)

                if extract_text:
                    material = fo_material_metadata(item)
                    try:
                        extraction = extract_fo_material_text(item, params)
                    except ValueError:
                        return send_json(self, {
                            "material": material,
                            "extraction_error": {
                                "error": "invalid_relative_path",
                                "relative_path": relative_path,
                            },
                        }, status=400)
                    except FileNotFoundError:
                        return send_json(self, {
                            "material": material,
                            "extraction_error": {
                                "error": "fo_pdf_file_not_found",
                                "relative_path": relative_path,
                            },
                        }, status=404)
                    except RuntimeError as exc:
                        if str(exc) == "pypdf_not_installed":
                            return send_json(self, {
                                "material": material,
                                "extraction_error": {
                                    "error": "fo_text_extractor_unavailable",
                                    "required_python_package": "pypdf",
                                },
                            }, status=503)
                        raise
                    except Exception as exc:
                        return send_json(self, {
                            "material": material,
                            "extraction_error": {
                                "error": "fo_text_extraction_failed",
                                "type": type(exc).__name__,
                                "message": str(exc),
                            },
                        }, status=500)

                    return send_json(self, {
                        "material": material,
                        "extraction": extraction,
                        "text": extraction.get("text", ""),
                    })

                return send_json(self, {"material": item})

            if path == "/fo/text":
                try:
                    manifest = load_fo_manifest()
                except FileNotFoundError:
                    return send_json(self, {"error": "fo_manifest_not_found"}, status=404)

                relative_path = get_first(params, "relative_path", "")
                if not relative_path:
                    return send_json(self, {"error": "missing_relative_path"}, status=400)

                item = find_fo_item_by_relative_path(iter_fo_items(manifest), relative_path)
                if item is None:
                    return send_json(self, {
                        "error": "fo_material_not_found",
                        "relative_path": relative_path,
                    }, status=404)

                try:
                    pdf_path = resolve_fo_pdf_path(item)
                except ValueError:
                    return send_json(self, {
                        "error": "invalid_relative_path",
                        "relative_path": relative_path,
                    }, status=400)
                except Exception:
                    return send_json(self, {
                        "error": "invalid_fo_pdf_path",
                        "relative_path": relative_path,
                    }, status=400)

                if not pdf_path.exists() or not pdf_path.is_file():
                    return send_json(self, {
                        "error": "fo_pdf_file_not_found",
                        "relative_path": relative_path,
                    }, status=404)

                page = parse_positive_int_param(params, "page", 1)
                page_limit = parse_positive_int_param(params, "limit", 5, maximum=20)
                max_chars = parse_positive_int_param(params, "max_chars", 20000, maximum=100000)

                try:
                    extraction = extract_fo_pdf_text(
                        pdf_path,
                        start_page=page,
                        page_limit=page_limit,
                        max_chars=max_chars,
                    )
                except RuntimeError as exc:
                    if str(exc) == "pypdf_not_installed":
                        return send_json(self, {
                            "error": "fo_text_extractor_unavailable",
                            "required_python_package": "pypdf",
                        }, status=503)
                    raise
                except Exception as exc:
                    return send_json(self, {
                        "error": "fo_text_extraction_failed",
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }, status=500)

                material = fo_material_metadata(item)
                return send_json(self, {
                    "material": material,
                    "extraction": extraction,
                })

            if path == "/fo/pdf":
                try:
                    manifest = load_fo_manifest()
                except FileNotFoundError:
                    return send_json(self, {"error": "fo_manifest_not_found"}, status=404)

                relative_path = get_first(params, "relative_path", "")
                if not relative_path:
                    return send_json(self, {"error": "missing_relative_path"}, status=400)

                item = find_fo_item_by_relative_path(iter_fo_items(manifest), relative_path)
                if item is None:
                    return send_json(self, {
                        "error": "fo_material_not_found",
                        "relative_path": relative_path,
                    }, status=404)

                try:
                    pdf_path = resolve_fo_pdf_path(item)
                except ValueError:
                    return send_json(self, {
                        "error": "invalid_relative_path",
                        "relative_path": relative_path,
                    }, status=400)
                except Exception:
                    return send_json(self, {
                        "error": "invalid_fo_pdf_path",
                        "relative_path": relative_path,
                    }, status=400)

                return send_file(self, pdf_path)

            if path in {"/openapi.json", "/anki_gpt_full_schema_30ops_stable.openapi.json"}:
                return send_file(self, OPENAPI_SCHEMA_PATH)

            if path == "/gpt_builder_organization_wrappers.openapi.json":
                return send_file(self, ORGANIZATION_WRAPPER_SCHEMA_PATH)

            derived = load_derived_request_state()
            note_media_index = derived["note_media_index"]
            notes = derived["notes"]
            deck_details = derived["deck_details"]
            deck_resolver = derived["deck_resolver"]
            snapshot_status = dict(derived["snapshot_status"])
            snapshot_status["derived_cache"] = {
                "generation_id": derived["generation_id"],
                "hits": DERIVED_STATE_CACHE["hits"],
                "misses": DERIVED_STATE_CACHE["misses"],
            }

            if path == "/roots":
                roots = sorted({
                    deck.get("root_deck", "")
                    for deck in deck_details
                    if deck.get("root_deck", "")
                })
                if not roots:
                    roots = sorted({n.get("root_deck", "") for n in notes if n.get("root_deck", "")})
                return send_json(self, {
                    "generated_at": snapshot_status.get("generated_at"),
                    "roots": roots,
                })

            if path == "/decks":
                prefix = get_first(params, "prefix", "")
                filtered_details = [
                    deck for deck in deck_details
                    if not prefix or deck_name_from_item(deck).startswith(prefix)
                ]
                decks = [deck_name_from_item(deck) for deck in filtered_details if deck_name_from_item(deck)]
                include_details = get_first(params, "details", "1") != "0"
                limit, offset = limit_offset(params)
                total = len(decks)
                detail_total = len(filtered_details)
                sliced_details = filtered_details[offset: offset + limit]
                sliced_decks = decks[offset: offset + limit]
                if prefix:
                    decks = [d for d in decks if d.startswith(prefix)]
                return send_json(self, {
                    "generated_at": snapshot_status.get("generated_at"),
                    "prefix": prefix,
                    "count": total,
                    "returned": len(sliced_decks),
                    "offset": offset,
                    "limit": limit,
                    "total_decks": snapshot_status.get("total_decks", detail_total),
                    "total_cards": snapshot_status.get("total_cards"),
                    "total_notes": snapshot_status.get("total_notes"),
                    "decks": sliced_decks,
                    "deck_details": sliced_details if include_details else None,
                })

            if path == "/snapshot/status":
                deck = get_first(params, "deck")
                matching_decks = []
                if deck:
                    matching_decks = [
                        item for item in deck_details
                        if deck_name_from_item(item) == deck
                    ]
                return send_json(self, {
                    **snapshot_status,
                    "requested_deck": deck,
                    "requested_deck_found": bool(matching_decks) if deck else None,
                    "requested_deck_details": matching_decks,
                    "sample_decks": deck_details[:20],
                })

            if path in {"/sync/decks", "/sync/full"}:
                return send_json(self, {
                    "ok": True,
                    "mode": path.rsplit("/", 1)[-1],
                    "generated_at": snapshot_status.get("generated_at"),
                    "status": snapshot_status,
                    "message": (
                        "Estado lido do ultimo snapshot publicado. Para ler o Anki agora, "
                        "use no Anki: Tools > Anki GPT > Sincronizar snapshot completo agora, "
                        "ou execute a sincronizacao do Anki para disparar o hook sync_did_finish."
                    ),
                })

            if path == "/cards/info":
                card_ids = parse_id_list(params, "ids")
                cards, missing = get_cards_info_from_snapshot(card_ids, notes)
                return send_json(self, {
                    "requested_card_ids": card_ids,
                    "count": len(cards),
                    "missing_card_ids": missing,
                    "cards": cards,
                })

            if path == "/notes/info":
                note_ids = parse_id_list(params, "ids")
                note_infos, missing = get_notes_info_from_snapshot(note_ids, notes)
                return send_json(self, {
                    "requested_note_ids": note_ids,
                    "count": len(note_infos),
                    "missing_note_ids": missing,
                    "notes": note_infos,
                })

            if path == "/cards/search-real":
                normal_search_cache = normal_search_cache_for_params(notes, params, derived["generation_id"])
                return send_json(
                    self,
                    search_real_cards_from_snapshot(
                        notes,
                        params,
                        deck_resolver,
                        normalized_note_search=normal_search_cache,
                    ),
                )

            if path == "/cards/query-ids":
                return send_json(self, query_ids_from_snapshot(notes, params, deck_resolver))

            if path == "/cards/materialize":
                try:
                    payload = materialize_batch_from_snapshot(notes, params)
                except ValueError as exc:
                    return send_json(self, {"error": str(exc)}, status=400)
                if payload.get("missing_card_ids") or payload.get("missing_note_ids"):
                    return send_json(self, {
                        "error": "materialization_batch_failed",
                        "message": "Um ou mais IDs do lote nao foram encontrados; nao consolide nem gere reorder parcial.",
                        "batch_index": payload.get("batch_index"),
                        "batch_total": payload.get("batch_total"),
                        "requested_card_ids": payload.get("requested_card_ids", []),
                        "requested_note_ids": payload.get("requested_note_ids", []),
                        "missing_card_ids": payload.get("missing_card_ids", []),
                        "missing_note_ids": payload.get("missing_note_ids", []),
                    }, status=404)
                max_bytes = parse_positive_int_param(
                    params,
                    "max_bytes",
                    MAX_MATERIALIZE_BATCH_BYTES,
                    maximum=max(MAX_MATERIALIZE_BATCH_BYTES, 10000),
                )
                return send_materialized_batch(self, payload, max_bytes)

            if path == "/notes/cards":
                note_ids = parse_id_list(params, "note_ids")
                mapping, missing = notes_to_cards_from_snapshot(note_ids, notes)
                return send_json(self, {
                    "requested_note_ids": note_ids,
                    "missing_note_ids": missing,
                    "mapping": mapping,
                })

            if path == "/cards/notes":
                card_ids = parse_id_list(params, "card_ids")
                mapping, missing = cards_to_notes_from_snapshot(card_ids, notes)
                return send_json(self, {
                    "requested_card_ids": card_ids,
                    "missing_card_ids": missing,
                    "mapping": mapping,
                })

            if path == "/cards/by-deck":
                deck = get_first(params, "deck")
                kind_filter = get_first(params, "kind")
                result_kind = normalize_result_kind(kind_filter)
                limit, offset = limit_offset(params)

                matches, filters = find_matching_cards_from_snapshot(notes, params, deck_resolver)
                items = build_endpoint_items_from_matches(matches, result_kind, note_media_index)
                total, cards = paginate(items, offset, limit)

                return send_json(self, {
                    "deck": deck,
                    "kind_filter": kind_filter,
                    "result_kind": result_kind,
                    "note_kind_filter": filters.get("note_kind_filter"),
                    "deck_ids": filters.get("deck_ids"),
                    "offset": offset,
                    "limit": limit,
                    "count": total,
                    "returned": len(cards),
                    "cards": cards,
                })

            if path == "/cards/by-decks":
                decks = get_all(params, "deck")
                kind_filter = get_first(params, "kind")
                limit, offset = limit_offset(params)

                deck_set = set(decks)
                filtered = [n for n in notes if any(note_matches_deck(n, deck, deck_resolver) for deck in deck_set)]
                if kind_filter:
                    filtered = [n for n in filtered if n.get("kind") == kind_filter]

                total, sliced = paginate(filtered, offset, limit)
                cards = [build_card(n, note_media_index) for n in sliced]

                return send_json(self, {
                    "decks": decks,
                    "kind_filter": kind_filter,
                    "offset": offset,
                    "limit": limit,
                    "count": total,
                    "returned": len(cards),
                    "cards": cards,
                })

            if path == "/cards/by-root":
                root = get_first(params, "root")
                kind_filter = get_first(params, "kind")
                result_kind = normalize_result_kind(kind_filter)
                limit, offset = limit_offset(params)

                matches, filters = find_matching_cards_from_snapshot(notes, params, deck_resolver)
                items = build_endpoint_items_from_matches(matches, result_kind, note_media_index)
                total, cards = paginate(items, offset, limit)

                return send_json(self, {
                    "root": root,
                    "kind_filter": kind_filter,
                    "result_kind": result_kind,
                    "note_kind_filter": filters.get("note_kind_filter"),
                    "root_deck_ids": filters.get("root_deck_ids"),
                    "offset": offset,
                    "limit": limit,
                    "count": total,
                    "returned": len(cards),
                    "cards": cards,
                })

            if path == "/cards/by-prefix":
                prefix = get_first(params, "prefix", "")
                kind_filter = get_first(params, "kind")
                result_kind = normalize_result_kind(kind_filter)
                limit, offset = limit_offset(params)

                matches, filters = find_matching_cards_from_snapshot(notes, params, deck_resolver)
                items = build_endpoint_items_from_matches(matches, result_kind, note_media_index)
                total, cards = paginate(items, offset, limit)

                return send_json(self, {
                    "prefix": prefix,
                    "kind_filter": kind_filter,
                    "result_kind": result_kind,
                    "note_kind_filter": filters.get("note_kind_filter"),
                    "prefix_deck_ids": filters.get("prefix_deck_ids"),
                    "offset": offset,
                    "limit": limit,
                    "count": total,
                    "returned": len(cards),
                    "cards": cards,
                })

            if path == "/cards/search":
                q_raw = (get_first(params, "q", "") or "").strip()
                parsed_deck, parsed_q = parse_deck_query(q_raw)
                root_filter = get_first(params, "root")
                prefix_filter = get_first(params, "prefix")
                deck_filter = get_first(params, "deck") or parsed_deck
                kind_filter = get_first(params, "kind")
                result_kind = normalize_result_kind(kind_filter)
                limit, offset = limit_offset(params)

                normal_search_cache = normal_search_cache_for_params(notes, params, derived["generation_id"])
                matches, filters = find_matching_cards_from_snapshot(
                    notes,
                    params,
                    deck_resolver,
                    normalized_note_search=normal_search_cache,
                )
                items = build_endpoint_items_from_matches(matches, result_kind, note_media_index)
                total, cards = paginate(items, offset, limit)

                return send_json(self, {
                    "query": q_raw,
                    "interpreted_text_query": parsed_q,
                    "interpreted_deck_query": parsed_deck,
                    "root_filter": root_filter,
                    "prefix_filter": prefix_filter,
                    "deck_filter": deck_filter,
                    "kind_filter": kind_filter,
                    "result_kind": result_kind,
                    "note_kind_filter": filters.get("note_kind_filter"),
                    "deck_ids": filters.get("deck_ids"),
                    "root_deck_ids": filters.get("root_deck_ids"),
                    "prefix_deck_ids": filters.get("prefix_deck_ids"),
                    "offset": offset,
                    "limit": limit,
                    "count": total,
                    "returned": len(cards),
                    "cards": cards,
                })

            return send_json(self, {"error": "not_found", "path": path}, status=404)

        except Exception as e:
            error = record_sanitized_error("request_get", e)
            log_server_event(
                "request_exception",
                method=getattr(self, "command", None),
                path=urlparse(getattr(self, "path", "")).path,
                request_id=getattr(self, "_request_id", None),
                correlation_id=getattr(self, "_correlation_id", None),
                **error,
            )
            return send_json(self, {
                "error": "internal_error",
            }, status=500)

    def do_POST(self):
        try:
            self._extra_response_headers = None
            parsed = urlparse(self.path)
            path = parsed.path
            self.log_request_start(path)

            if path == "/ufpr/obras/query":
                try:
                    if not require_tagging_token(self):
                        return
                    payload = read_json_body(self)
                    response = ufpr_query_response(payload)
                    status = 400 if response.get("ok") is False else 200
                    return send_json(self, response, status=status)
                except json.JSONDecodeError:
                    return send_json(self, {"error": "invalid_json"}, status=400)
                except ValueError as exc:
                    return send_json(self, {"error": str(exc)}, status=400)
                except LookupError as exc:
                    return send_json(self, {"error": str(exc)}, status=404)
                except FileNotFoundError as exc:
                    return send_json(self, {"error": "ufpr_file_not_found"}, status=404)

            if path == "/sync/full":
                try:
                    if not require_tagging_token(self):
                        return
                    payload = read_json_body(self, max_bytes=MAX_SYNC_BODY_BYTES)
                    result = publish_full_snapshot_payload(payload, request_path=path)
                except json.JSONDecodeError:
                    return send_json(self, {"error": "invalid_json"}, status=400)
                except ValueError as exc:
                    return send_json(self, {"error": str(exc)}, status=400)
                return send_json(self, result)

            if path == "/sync/decks":
                try:
                    if not require_tagging_token(self):
                        return
                    payload = read_json_body(self, max_bytes=MAX_SYNC_BODY_BYTES)
                    if not isinstance(payload, dict):
                        raise ValueError("invalid_deck_snapshot_payload")
                    decks = payload.get("decks", payload if isinstance(payload, list) else [])
                    if not isinstance(decks, list):
                        raise ValueError("invalid_decks")
                    generated_at = payload.get("generated_at") or payload.get("timestamp") or utc_now_iso()
                    objects, current_manifest = STATE_CACHE.snapshot()
                    decks_index = {
                        "generated_at": generated_at,
                        "profile": payload.get("profile"),
                        "snapshot_version": payload.get("snapshot_version"),
                        "total_decks": len(decks),
                        "total_cards": payload.get("total_cards"),
                        "total_notes": payload.get("total_notes"),
                        "decks": decks,
                    }
                    snapshot_status = dict(objects["snapshot_status.json"])
                    snapshot_status.update({
                        "decks_generated_at": generated_at,
                        "total_decks": len(decks),
                        "total_cards": payload.get("total_cards", snapshot_status.get("total_cards")),
                        "total_notes": payload.get("total_notes", snapshot_status.get("total_notes")),
                    })
                    next_objects = dict(objects)
                    next_objects["decks_index.json"] = decks_index
                    next_objects["snapshot_status.json"] = snapshot_status
                    manifest = publish_generation(
                        STATE_DIR,
                        next_objects,
                        metadata={
                            "generated_at": generated_at,
                            "source": "sync_decks",
                            "previous_generation_id": current_manifest.get("generation_id"),
                            "total_decks": len(decks),
                            "total_cards": decks_index.get("total_cards"),
                            "total_notes": decks_index.get("total_notes"),
                        },
                    )
                    atomic_write_json(DECKS_INDEX_PATH, decks_index)
                    atomic_write_json(SNAPSHOT_STATUS_PATH, snapshot_status)
                except json.JSONDecodeError:
                    return send_json(self, {"error": "invalid_json"}, status=400)
                except ValueError as exc:
                    return send_json(self, {"error": str(exc)}, status=400)
                return send_json(self, {
                    "ok": True,
                    "generated_at": generated_at,
                    "total_decks": len(decks),
                    "generation_id": manifest["generation_id"],
                })

            if path == "/tagging/operations":
                if not require_tagging_token(self):
                    return

                try:
                    payload = read_json_body(self)
                    operation = build_tagging_operation(payload)
                except json.JSONDecodeError:
                    return send_json(self, {"error": "invalid_json"}, status=400)
                except ValueError as exc:
                    return send_json(self, {"error": str(exc)}, status=400)

                operation_path = tagging_operation_path(operation["operation_id"])
                atomic_write_json(operation_path, operation)

                return send_json(self, {
                    "ok": True,
                    "operation": operation,
                }, status=201)

            if path == "/tagging/operations/confirm":
                if not require_tagging_token(self):
                    return

                try:
                    payload = read_json_body(self)
                except json.JSONDecodeError:
                    return send_json(self, {"error": "invalid_json"}, status=400)

                operation_id = payload.get("operation_id")
                operation_path, operation = load_tagging_operation(operation_id)
                if operation is None:
                    return send_json(self, {
                        "error": "tagging_operation_not_found",
                        "operation_id": operation_id,
                    }, status=404)

                if operation.get("status") in TAGGING_FINAL_STATUSES:
                    return send_json(self, {
                        "error": "tagging_operation_already_final",
                        "operation_id": operation_id,
                        "status": operation.get("status"),
                    }, status=409)

                status = payload.get("status")
                if status not in TAGGING_FINAL_STATUSES:
                    return send_json(self, {"error": "invalid_execution_status"}, status=400)

                affected_note_ids = payload.get("affected_note_ids", [])
                if not isinstance(affected_note_ids, list):
                    return send_json(self, {"error": "invalid_affected_note_ids"}, status=400)
                normalized_affected_note_ids = []
                for note_id in affected_note_ids:
                    if isinstance(note_id, bool) or not isinstance(note_id, int):
                        return send_json(self, {"error": "invalid_affected_note_id"}, status=400)
                    if note_id not in normalized_affected_note_ids:
                        normalized_affected_note_ids.append(note_id)

                affected_count = payload.get("affected_count", len(normalized_affected_note_ids))
                if isinstance(affected_count, bool) or not isinstance(affected_count, int) or affected_count < 0:
                    return send_json(self, {"error": "invalid_affected_count"}, status=400)

                errors = payload.get("errors", [])
                if not isinstance(errors, list) or not all(isinstance(error, str) for error in errors):
                    return send_json(self, {"error": "invalid_errors"}, status=400)

                executed_at = utc_now_iso()
                try:
                    addon_profile = optional_string(payload, "addon_profile")
                except ValueError as exc:
                    return send_json(self, {"error": str(exc)}, status=400)
                execution_confirmation = {
                    "status": status,
                    "executed_at": executed_at,
                    "addon_profile": addon_profile,
                    "affected_note_ids": normalized_affected_note_ids,
                    "affected_count": affected_count,
                    "errors": errors,
                }
                operation["status"] = status
                operation["executed_at"] = executed_at
                operation["execution_confirmation"] = execution_confirmation
                atomic_write_json(operation_path, operation)

                return send_json(self, {
                    "ok": True,
                    "operation": operation,
                })

            if path == "/organization/note-field-updates-create":
                log_action_event(self, path, "request_received")
                try:
                    if not require_tagging_token(self):
                        log_action_event(self, path, "auth_failed")
                        return
                    log_action_event(self, path, "auth_ok")
                    payload = read_json_body_for_action(self, path)
                    updates = normalize_note_field_updates_create_payload(payload)
                    response = persist_note_field_updates(updates)
                except json.JSONDecodeError:
                    log_action_event(self, path, "response_sent", status=400, error="invalid_json")
                    return send_json(self, {"error": "invalid_json"}, status=400)
                except ValueError as exc:
                    log_action_event(self, path, "response_sent", status=400, error=str(exc))
                    return send_json(self, {"error": str(exc)}, status=400)
                log_action_event(self, path, "note_field_updates_created", updates_id=response.get("updates_id"))
                log_action_event(self, path, "response_sent", status=200, updates_id=response.get("updates_id"))
                return send_json(self, response, status=200)

            if path == "/organization/update-note-fields-create":
                return handle_action_logged_organization_post(
                    self,
                    path,
                    lambda payload: build_organization_wrapper_operation(
                        payload,
                        "update_note_fields",
                        ["updates_id", "dry_run"],
                    ),
                )

            if path == "/organization/operations":
                return handle_action_logged_organization_post(
                    self,
                    path,
                    lambda payload: build_organization_operation(payload),
                )

            if path in {"/organization/reorder-order", "/organization/reorder-order-create"}:
                log_action_event(self, path, "request_received")
                try:
                    if not require_tagging_token(self):
                        log_action_event(self, path, "auth_failed")
                        return
                    log_action_event(self, path, "auth_ok")
                    payload = read_json_body_for_action(self, path)
                    order = normalize_reorder_order_payload(payload)
                    response = persist_reorder_order(order)
                except json.JSONDecodeError:
                    log_action_event(self, path, "response_sent", status=400, error="invalid_json")
                    return send_json(self, {"error": "invalid_json"}, status=400)
                except ValueError as exc:
                    log_action_event(self, path, "response_sent", status=400, error=str(exc))
                    return send_json(self, {"error": str(exc)}, status=400)
                log_action_event(self, path, "reorder_order_created", order_id=response.get("order_id"))
                log_action_event(self, path, "response_sent", status=200, order_id=response.get("order_id"))
                return send_json(self, response, status=200)

            if path in {"/organization/reorder-cards-by-material", "/organization/reorder-cards-by-material-create"}:
                return handle_action_logged_organization_post(
                    self,
                    path,
                    lambda payload: build_organization_wrapper_operation(
                        payload,
                        "reorder_cards_by_material",
                        REORDER_CARDS_BY_MATERIAL_PAYLOAD_KEYS,
                    ),
                )

            if path == "/organization/update-note-fields":
                return handle_action_logged_organization_post(
                    self,
                    path,
                    lambda payload: build_organization_wrapper_operation(
                        payload,
                        "update_note_fields",
                        ["note_updates", "dry_run"],
                    ),
                )

            if path == "/organization/replace-note-tags":
                return handle_action_logged_organization_post(
                    self,
                    path,
                    lambda payload: build_organization_wrapper_operation(
                        payload,
                        "replace_note_tags",
                        ["note_ids", "remove_tags", "add_tags", "dry_run"],
                    ),
                )

            if path == "/organization/create-note":
                return handle_action_logged_organization_post(
                    self,
                    path,
                    lambda payload: build_organization_wrapper_operation(
                        payload,
                        "create_note",
                        ["deck_name", "model_name", "fields", "tags", "allow_duplicate", "dry_run"],
                    ),
                )

            if path == "/organization/create-cloze-note":
                return handle_action_logged_organization_post(
                    self,
                    path,
                    lambda payload: build_create_cloze_note_operation(payload),
                )

            if path in {
                "/organization/operations/result",
                "/organization/operations/confirm",
            }:
                if not require_tagging_token(self):
                    return

                try:
                    payload = read_json_body(self)
                except json.JSONDecodeError:
                    return send_json(self, {"error": "invalid_json"}, status=400)

                operation_id = payload.get("operation_id")
                operation_path, operation = load_organization_operation(operation_id)
                if operation is None:
                    return send_json(self, {
                        "error": "organization_operation_not_found",
                        "operation_id": operation_id,
                    }, status=404)

                try:
                    receipt = validate_organization_confirmation_receipt(operation, payload)
                except ValueError as exc:
                    return send_json(self, {"error": str(exc)}, status=409)

                if operation.get("status") in ORGANIZATION_FINAL_STATUSES:
                    if organization_confirmation_is_receipt_replay(operation, payload, receipt):
                        return send_json(self, {
                            "ok": True,
                            "replayed": True,
                            "operation_id": operation_id,
                            "status": operation.get("status"),
                        })
                    return send_json(self, {
                        "error": "organization_operation_already_final",
                        "operation_id": operation_id,
                        "status": operation.get("status"),
                    }, status=409)

                status = payload.get("status")
                if status not in ORGANIZATION_FINAL_STATUSES:
                    return send_json(self, {"error": "invalid_execution_status"}, status=400)

                ok = payload.get("ok")
                if not isinstance(ok, bool):
                    return send_json(self, {"error": "invalid_ok"}, status=400)

                operation_type = payload.get("operation_type")
                if operation_type is not None and operation_type != operation.get("operation_type"):
                    return send_json(self, {"error": "operation_type_mismatch"}, status=400)

                result = payload.get("result")
                if result is not None and not isinstance(result, dict):
                    return send_json(self, {"error": "invalid_result"}, status=400)

                errors = payload.get("errors", [])
                if not isinstance(errors, list) or not all(isinstance(error, str) for error in errors):
                    return send_json(self, {"error": "invalid_errors"}, status=400)

                metadata = payload.get("metadata", {})
                if not isinstance(metadata, dict):
                    return send_json(self, {"error": "invalid_metadata"}, status=400)

                executed_at = utc_now_iso()
                try:
                    addon_profile = optional_string(payload, "addon_profile")
                except ValueError as exc:
                    return send_json(self, {"error": str(exc)}, status=400)

                operation["status"] = status
                operation["result"] = result
                operation["executed_at"] = executed_at
                execution_result = {
                    "ok": ok,
                    "status": status,
                    "executed_at": executed_at,
                    "addon_profile": addon_profile,
                    "errors": errors,
                    "metadata": metadata,
                    "receipt": receipt,
                }
                operation["execution_result"] = execution_result
                # Legacy readers still use this name. It is a result receipt,
                # never a prerequisite for direct queue execution.
                operation["execution_confirmation"] = execution_result
                atomic_write_json(operation_path, operation)

                return send_json(self, {
                    "ok": True,
                    "operation": operation,
                })

            return send_json(self, {"error": "not_found", "path": path}, status=404)

        except Exception as e:
            error = record_sanitized_error("request_post", e)
            log_server_event(
                "request_exception",
                method=getattr(self, "command", None),
                path=urlparse(getattr(self, "path", "")).path,
                request_id=getattr(self, "_request_id", None),
                correlation_id=getattr(self, "_correlation_id", None),
                **error,
            )
            return send_json(self, {
                "error": "internal_error",
            }, status=500)

    def do_HEAD(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            self.log_request_start(path)

            if path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if path == "/version":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if REQUIRE_READ_AUTH and not require_tagging_token(self):
                return

            if path.startswith("/media/"):
                raw_name = path[len("/media/"):]
                filename = unquote(raw_name)

                if (
                    "/" in filename
                    or "\x00" in filename
                    or len(filename.encode("utf-8")) > 255
                    or filename in ("", ".", "..")
                ):
                    self.send_response(400)
                    self.end_headers()
                    return

                media_root = MEDIA_DIR.resolve()
                unresolved = media_root / filename
                if unresolved.is_symlink():
                    self.send_response(404)
                    self.end_headers()
                    return
                fpath = unresolved.resolve()
                try:
                    fpath.relative_to(media_root)
                except ValueError:
                    self.send_response(400)
                    self.end_headers()
                    return
                if not fpath.exists() or not fpath.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return

                ctype, _ = mimetypes.guess_type(str(fpath))
                if not ctype:
                    ctype = "application/octet-stream"

                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(fpath.stat().st_size))
                self.end_headers()
                return

            self.send_response(501)
            self.end_headers()

        except Exception as exc:
            error = record_sanitized_error("request_head", exc)
            log_server_event(
                "request_exception",
                method=getattr(self, "command", None),
                path=urlparse(getattr(self, "path", "")).path,
                request_id=getattr(self, "_request_id", None),
                correlation_id=getattr(self, "_correlation_id", None),
                **error,
            )
            self.send_response(500)
            self.end_headers()

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    configure_tagging_token_from_file()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()
