"""Development-tree loader for the canonical note-precondition contract.

Component bundles replace this loader with packages/anki-contracts/
note_preconditions.py so the deployed API remains self-contained.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "anki-contracts"
    / "note_preconditions.py"
)
_SPEC = importlib.util.spec_from_file_location("anki_contracts_note_preconditions", _SOURCE)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"canonical note preconditions unavailable: {_SOURCE}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

canonical_note_content = _MODULE.canonical_note_content
note_content_hash = _MODULE.note_content_hash
build_note_precondition = _MODULE.build_note_precondition
