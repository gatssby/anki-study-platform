"""Development-tree loader for the canonical Basic -> Cloze contract.

Component bundles replace this loader with packages/anki-contracts/
basic_to_cloze.py so the installed addon remains self-contained.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[3] / "packages" / "anki-contracts" / "basic_to_cloze.py"
_SPEC = importlib.util.spec_from_file_location("anki_contracts_basic_to_cloze", _SOURCE)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"canonical Basic -> Cloze contract unavailable: {_SOURCE}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

cloze_deletions = _MODULE.cloze_deletions
structural_fragments = _MODULE.structural_fragments
validate_basic_to_cloze_fields = _MODULE.validate_basic_to_cloze_fields
