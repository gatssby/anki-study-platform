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
COMPACT_SCHEMA = ROOT / "contracts" / "openapi" / "gpt-action-compact.openapi.json"
GPT_KNOWLEDGE_SCHEMA = ROOT / "apps" / "anki-gpt" / "gpt-knowledge" / "schema gpt.json"
EXPECTED_COMPACT_OPERATIONS = 23


def operations(document: dict) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, item in document.get("paths", {}).items()
        for method in item
        if method in METHODS
    }


def iter_nodes(value, path="$"):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from iter_nodes(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from iter_nodes(child, f"{path}[{index}]")


def unresolved_internal_refs(document: dict) -> list[tuple[str, str]]:
    unresolved = []
    for path, node in iter_nodes(document):
        if not isinstance(node, dict) or "$ref" not in node:
            continue
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            unresolved.append((path, str(ref)))
            continue
        target = document
        try:
            for raw_part in ref[2:].split("/"):
                part = raw_part.replace("~1", "/").replace("~0", "~")
                target = target[part]
        except (KeyError, TypeError):
            unresolved.append((path, ref))
    return unresolved


def object_schemas_without_properties(document: dict) -> list[str]:
    return [
        path
        for path, node in iter_nodes(document)
        if isinstance(node, dict)
        and node.get("type") == "object"
        and not isinstance(node.get("properties"), dict)
    ]


def validate_compact_schema(path: Path, document: dict, operation_count: int) -> None:
    if operation_count != EXPECTED_COMPACT_OPERATIONS:
        raise ValueError(
            f"invalid_compact_operation_count: expected={EXPECTED_COMPACT_OPERATIONS} "
            f"actual={operation_count}"
        )

    invalid_objects = object_schemas_without_properties(document)
    if invalid_objects:
        raise ValueError(
            "gpt_builder_object_schema_missing_properties: "
            + ", ".join(invalid_objects)
        )

    unresolved = unresolved_internal_refs(document)
    if unresolved:
        raise ValueError(f"unresolved_openapi_refs: {unresolved!r}")

    server_urls = [
        server.get("url")
        for server in document.get("servers", [])
        if isinstance(server, dict)
    ]
    if not server_urls or any(
        not isinstance(url, str) or not url.startswith("https://")
        for url in server_urls
    ):
        raise ValueError(f"compact_schema_requires_https_server: {server_urls!r}")

    serialized = json.dumps(document, ensure_ascii=False)
    if "X-Tagging-Token" in serialized:
        raise ValueError("compact_schema_must_not_embed_auth_header")

    if path.read_bytes() != GPT_KNOWLEDGE_SCHEMA.read_bytes():
        raise ValueError("gpt_knowledge_schema_diverges_from_canonical_compact_schema")


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
        if path == COMPACT_SCHEMA:
            validate_compact_schema(path, document, len(operation_ids))
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
