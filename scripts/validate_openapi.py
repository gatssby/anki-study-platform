#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from openapi_spec_validator import validate

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = (
    ROOT / "contracts" / "openapi" / "anki-api.openapi.json",
    ROOT / "contracts" / "openapi" / "gpt-organization-wrappers.openapi.json",
    ROOT / "contracts" / "openapi" / "gpt-action-compact.openapi.json",
)
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def operations(document: dict) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, item in document.get("paths", {}).items()
        for method in item
        if method in METHODS
    }


def main() -> int:
    documents: list[tuple[Path, dict]] = []
    for path in SCHEMAS:
        document = json.loads(path.read_text(encoding="utf-8"))
        validate(document)
        operation_ids = [
            operation.get("operationId")
            for item in document.get("paths", {}).values()
            for method, operation in item.items()
            if method in METHODS
        ]
        if None in operation_ids or len(operation_ids) != len(set(operation_ids)):
            raise ValueError(f"invalid_or_duplicate_operation_id: {path.name}")
        documents.append((path, document))
        print(f"VALID {path.relative_to(ROOT)} operations={len(operation_ids)}")

    full_operations = operations(documents[0][1])
    runtime_source = (ROOT / "services" / "anki-api" / "query_api.py").read_text(encoding="utf-8")
    for path, document in documents[1:]:
        missing = operations(document) - full_operations
        unsupported = {
            (method, route)
            for method, route in missing
            if f'"{route}"' not in runtime_source
        }
        if unsupported:
            raise ValueError(
                f"openapi_variant_not_implemented: {path.name}: {sorted(unsupported)!r}"
            )
        if missing:
            print(f"VALID_EXTENSION {path.relative_to(ROOT)} operations={sorted(missing)!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
