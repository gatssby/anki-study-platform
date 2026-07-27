"""Canonical note preconditions shared by snapshot readers and the Anki executor."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def _integer_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def canonical_note_content(
    *,
    model_id: int | None,
    field_names: Sequence[str],
    fields: Mapping[str, Any],
    tags: Iterable[Any],
) -> dict[str, Any]:
    """Return the exact stable material hashed by apply-v2.

    Field order is the model order supplied by Anki. Values, HTML, Unicode and
    line endings are preserved exactly. A missing model field is represented by
    the same empty string fallback used by the executor. Tags are sorted because
    their collection order is not semantic. Scheduling and volatile timestamps
    are deliberately excluded.
    """

    ordered_names: list[str] = []
    seen: set[str] = set()
    for raw_name in field_names:
        name = str(raw_name)
        if name not in seen:
            seen.add(name)
            ordered_names.append(name)

    return {
        "model_id": _integer_or_none(model_id),
        "fields": [
            [name, str(fields.get(name, ""))]
            for name in ordered_names
        ],
        "tags": sorted(str(tag) for tag in tags),
    }


def note_content_hash(
    *,
    model_id: int | None,
    field_names: Sequence[str],
    fields: Mapping[str, Any],
    tags: Iterable[Any],
) -> str:
    payload = canonical_note_content(
        model_id=model_id,
        field_names=field_names,
        fields=fields,
        tags=tags,
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_note_precondition(
    *,
    model_id: int | None,
    mod: int | None,
    usn: int | None,
    field_names: Sequence[str],
    fields: Mapping[str, Any],
    tags: Iterable[Any],
) -> dict[str, int | str | None]:
    return {
        "expected_content_hash": note_content_hash(
            model_id=model_id,
            field_names=field_names,
            fields=fields,
            tags=tags,
        ),
        "expected_mod": _integer_or_none(mod),
        "expected_usn": _integer_or_none(usn),
        "expected_model_id": _integer_or_none(model_id),
    }
