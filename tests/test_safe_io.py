from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "safe_io_under_test", ROOT / "packages" / "safe-io" / "safe_io.py"
)
assert SPEC and SPEC.loader
safe_io = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(safe_io)


def test_atomic_write_json_replaces_complete_document(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"
    safe_io.atomic_write_json(destination, {"generation": 1})
    assert json.loads(destination.read_text(encoding="utf-8")) == {"generation": 1}
    assert not list(tmp_path.glob(".*.tmp"))


def test_safe_resolve_under_rejects_escape(tmp_path: Path) -> None:
    assert safe_io.safe_resolve_under(tmp_path, "subject/file.txt").is_relative_to(tmp_path)
    with pytest.raises(ValueError, match="invalid_relative_path"):
        safe_io.safe_resolve_under(tmp_path, "../private.txt")
