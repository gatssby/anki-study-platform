from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "packages" / "fo-contracts" / "fo_contracts.py"
SPEC = importlib.util.spec_from_file_location("fo_contracts_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
fo_contracts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fo_contracts)


def test_artificial_aulas_index_fixture_is_valid() -> None:
    count = fo_contracts.validate_aulas_index_file(
        ROOT / "contracts" / "fixtures" / "aulas_index.v1.tsv"
    )
    assert count == 1


def test_artificial_manifest_fixture_is_valid() -> None:
    count = fo_contracts.validate_material_manifest_file(
        ROOT / "contracts" / "fixtures" / "fo-material-manifest.v1.json"
    )
    assert count == 1


def test_index_rejects_column_reordering(tmp_path: Path) -> None:
    fixture = ROOT / "contracts" / "fixtures" / "aulas_index.v1.tsv"
    header, row = fixture.read_text(encoding="utf-8").splitlines()
    columns = header.split("\t")
    columns[0], columns[1] = columns[1], columns[0]
    invalid = tmp_path / "invalid.tsv"
    invalid.write_text("\t".join(columns) + "\n" + row + "\n", encoding="utf-8")
    with pytest.raises(fo_contracts.ContractError, match="header_mismatch"):
        fo_contracts.validate_aulas_index_file(invalid)


def test_manifest_rejects_path_traversal() -> None:
    fixture = ROOT / "contracts" / "fixtures" / "fo-material-manifest.v1.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["items"][0]["relative_path"] = "../private.pdf"
    with pytest.raises(fo_contracts.ContractError, match="relative_path_invalid"):
        fo_contracts.validate_material_manifest_payload(payload)
