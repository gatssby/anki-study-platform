"""Pure validators for FO index and material manifest contracts."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

AULAS_INDEX_SCHEMA_VERSION = 1
MATERIAL_MANIFEST_SCHEMA_VERSION = 1

AULAS_INDEX_COLUMNS = (
    "area",
    "portal_root",
    "portal_subject",
    "disciplina",
    "ordem",
    "nome_real_aula",
    "titulo_original",
    "duracao_segundos",
    "duracao_texto",
    "media_id",
    "item_id",
    "media_type",
    "course_url",
    "source_ref",
    "captured_at",
    "status",
    "error",
    "video_stream_url",
)

MATERIAL_MANIFEST_COLUMNS = (
    "target_key",
    "group",
    "portal_subject",
    "output_subject",
    "material_type",
    "status",
    "file_name",
    "relative_path",
    "sha256",
    "byte_size",
    "source_url",
    "source_label",
    "started_at",
    "finished_at",
    "error",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    """Raised before invalid producer output is published or consumed."""


def validate_aulas_index_header(fieldnames: Iterable[str] | None) -> None:
    actual = tuple(fieldnames or ())
    if actual != AULAS_INDEX_COLUMNS:
        raise ContractError(
            f"aulas_index_header_mismatch: expected={AULAS_INDEX_COLUMNS!r} actual={actual!r}"
        )


def _optional_nonnegative_int(value: Any, field: str, row_number: int) -> None:
    if value in (None, ""):
        return
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field}_not_integer: row={row_number}") from exc
    if parsed < 0:
        raise ContractError(f"{field}_negative: row={row_number}")


def validate_aulas_index_rows(rows: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    for count, row in enumerate(rows, start=1):
        if tuple(row.keys()) != AULAS_INDEX_COLUMNS:
            raise ContractError(f"aulas_index_row_shape_mismatch: row={count}")
        _optional_nonnegative_int(row.get("ordem"), "ordem", count)
        _optional_nonnegative_int(row.get("duracao_segundos"), "duracao_segundos", count)
        if not str(row.get("source_ref") or "").strip():
            raise ContractError(f"source_ref_missing: row={count}")
        if not str(row.get("status") or "").strip():
            raise ContractError(f"status_missing: row={count}")
    return count


def validate_aulas_index_file(path: Path | str) -> int:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        validate_aulas_index_header(reader.fieldnames)
        return validate_aulas_index_rows(reader)


def _validate_relative_path(value: Any, row_number: int) -> None:
    text = str(value or "")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or "\\" in text:
        raise ContractError(f"manifest_relative_path_invalid: row={row_number}")


def validate_material_manifest_payload(payload: Mapping[str, Any]) -> int:
    items = payload.get("items")
    if not isinstance(items, list):
        raise ContractError("manifest_items_not_list")
    if payload.get("schema_version", MATERIAL_MANIFEST_SCHEMA_VERSION) != MATERIAL_MANIFEST_SCHEMA_VERSION:
        raise ContractError("manifest_schema_version_unsupported")
    for row_number, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            raise ContractError(f"manifest_item_not_object: row={row_number}")
        missing = [column for column in MATERIAL_MANIFEST_COLUMNS if column not in item]
        if missing:
            raise ContractError(f"manifest_fields_missing: row={row_number} fields={missing!r}")
        _validate_relative_path(item.get("relative_path"), row_number)
        try:
            byte_size = int(item.get("byte_size"))
        except (TypeError, ValueError) as exc:
            raise ContractError(f"manifest_byte_size_invalid: row={row_number}") from exc
        if byte_size < 0:
            raise ContractError(f"manifest_byte_size_negative: row={row_number}")
        if item.get("status") == "ok" and not _SHA256_RE.fullmatch(str(item.get("sha256") or "")):
            raise ContractError(f"manifest_sha256_invalid: row={row_number}")
    if payload.get("count") != len(items):
        raise ContractError("manifest_count_mismatch")
    return len(items)


def validate_material_manifest_file(path: Path | str) -> int:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ContractError("manifest_not_object")
    return validate_material_manifest_payload(payload)
