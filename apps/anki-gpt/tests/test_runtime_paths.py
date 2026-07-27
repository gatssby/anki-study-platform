import importlib.util
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_runtime_paths():
    name = f"anki_gpt_runtime_paths_test_{os.urandom(6).hex()}"
    path = ROOT / "addon-local" / "runtime_paths.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def initialize(module, home, reports=None):
    reports = [] if reports is None else reports
    paths, migration = module.initialize_runtime_paths(
        home=home,
        platform="darwin",
        environ={},
        report=reports.append,
    )
    return paths, migration, reports


def test_canonical_runtime_path_is_mac_application_support_and_cwd_independent(
    tmp_path, monkeypatch
):
    module = load_runtime_paths()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    path = module.canonical_runtime_root(
        home=tmp_path,
        platform="darwin",
        environ={},
    )

    assert path == (
        tmp_path
        / "Library"
        / "Application Support"
        / "Anki2"
        / "addon-data"
        / "anki_gpt_sync"
    )
    assert unrelated not in path.parents


def test_relative_runtime_override_is_anchored_to_home_not_cwd(tmp_path, monkeypatch):
    module = load_runtime_paths()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    path = module.canonical_runtime_root(
        home=tmp_path,
        platform="darwin",
        environ={"ANKI_GPT_RUNTIME_DIR": "fixture-runtime"},
    )

    assert path == tmp_path / "fixture-runtime"


def test_missing_runtime_directory_is_created(tmp_path):
    module = load_runtime_paths()

    paths, migration, reports = initialize(module, tmp_path)

    assert migration == "missing"
    assert reports == []
    for directory in (
        paths.root,
        paths.logs,
        paths.state,
        paths.cache,
        paths.staging,
        paths.backups,
        paths.organization,
    ):
        assert directory.is_dir()


def test_existing_canonical_directory_is_preserved(tmp_path):
    module = load_runtime_paths()
    root = module.canonical_runtime_root(home=tmp_path, platform="darwin", environ={})
    root.mkdir(parents=True)
    marker = root / "existing.txt"
    marker.write_text("canonical", encoding="utf-8")

    paths, migration, reports = initialize(module, tmp_path)

    assert migration == "missing"
    assert reports == []
    assert paths.root == root
    assert marker.read_text(encoding="utf-8") == "canonical"


def test_legacy_directory_is_moved_with_runtime_data_and_permissions(tmp_path):
    module = load_runtime_paths()
    legacy = tmp_path / module.LEGACY_RUNTIME_NAME
    for relative in ("logs", "cache", "runtime", "state"):
        (legacy / relative).mkdir(parents=True)
        payload = legacy / relative / "fixture.txt"
        payload.write_text(relative, encoding="utf-8")
        payload.chmod(0o640)
    legacy.chmod(0o750)

    paths, migration, reports = initialize(module, tmp_path)

    assert migration == "directory_moved"
    assert reports == []
    assert not legacy.exists()
    assert stat.S_IMODE(paths.root.stat().st_mode) == 0o750
    for relative in ("logs", "cache", "runtime", "state"):
        payload = paths.root / relative / "fixture.txt"
        assert payload.read_text(encoding="utf-8") == relative
        assert stat.S_IMODE(payload.stat().st_mode) == 0o640


def test_existing_legacy_and_canonical_directories_are_merged_without_overwrite(tmp_path):
    module = load_runtime_paths()
    legacy = tmp_path / module.LEGACY_RUNTIME_NAME
    legacy.mkdir()
    (legacy / "cache").mkdir()
    (legacy / "cache" / "same.txt").write_text("legacy", encoding="utf-8")
    root = module.canonical_runtime_root(home=tmp_path, platform="darwin", environ={})
    (root / "cache").mkdir(parents=True)
    (root / "cache" / "same.txt").write_text("canonical", encoding="utf-8")

    paths, migration, reports = initialize(module, tmp_path)

    assert migration == "directory_merged"
    assert reports == []
    assert not legacy.exists()
    assert (paths.cache / "same.txt").read_text(encoding="utf-8") == "canonical"
    assert (paths.cache / "same.txt.legacy").read_text(encoding="utf-8") == "legacy"


def test_valid_legacy_symlink_is_copied_then_unlinked_without_touching_target(tmp_path):
    module = load_runtime_paths()
    target = tmp_path / "old-project-runtime"
    (target / "logs").mkdir(parents=True)
    (target / "logs" / "fixture.log").write_text("preserved", encoding="utf-8")
    legacy = tmp_path / module.LEGACY_RUNTIME_NAME
    legacy.symlink_to(target, target_is_directory=True)

    paths, migration, reports = initialize(module, tmp_path)

    assert migration == "valid_symlink_migrated"
    assert reports == []
    assert not os.path.lexists(legacy)
    assert (target / "logs" / "fixture.log").read_text(encoding="utf-8") == "preserved"
    assert (paths.logs / "fixture.log").read_text(encoding="utf-8") == "preserved"


def test_broken_legacy_symlink_is_removed_and_never_blocks_initialization(tmp_path):
    module = load_runtime_paths()
    legacy = tmp_path / module.LEGACY_RUNTIME_NAME
    legacy.symlink_to(tmp_path / "missing-target", target_is_directory=True)

    paths, migration, reports = initialize(module, tmp_path)

    assert migration == "broken_symlink_removed"
    assert reports == []
    assert not os.path.lexists(legacy)
    assert paths.root.is_dir()


def test_file_at_legacy_path_is_ignored(tmp_path):
    module = load_runtime_paths()
    legacy = tmp_path / module.LEGACY_RUNTIME_NAME
    legacy.write_text("do not delete", encoding="utf-8")

    paths, migration, reports = initialize(module, tmp_path)

    assert migration == "file_ignored"
    assert legacy.read_text(encoding="utf-8") == "do not delete"
    assert paths.root.is_dir()
    assert any("occupied by a file" in message for message in reports)


def test_permission_errors_are_reported_without_raising(tmp_path, monkeypatch):
    module = load_runtime_paths()
    reports = []

    def deny_migration(*_args, **_kwargs):
        raise PermissionError("fixture migration denied")

    def deny_prepare(*_args, **_kwargs):
        raise PermissionError("fixture mkdir denied")

    monkeypatch.setattr(module, "_migrate_legacy_path", deny_migration)
    monkeypatch.setattr(module, "_prepare_runtime_directories", deny_prepare)

    paths, migration = module.initialize_runtime_paths(
        home=tmp_path,
        platform="darwin",
        environ={},
        report=reports.append,
    )

    assert migration == "initialization_failed"
    assert paths.root.name == "anki_gpt_sync"
    assert any("migration failure" in message for message in reports)
    assert any("prepare runtime directories" in message for message in reports)


def test_logging_failure_falls_back_to_report_and_returns_false(tmp_path):
    module = load_runtime_paths()
    occupied_parent = tmp_path / "occupied"
    occupied_parent.write_text("file", encoding="utf-8")
    reports = []

    written = module.append_log_resilient(
        occupied_parent / "addon.log",
        "fixture message",
        max_bytes=100,
        backup_count=2,
        report=reports.append,
    )

    assert written is False
    assert len(reports) == 1
    assert reports[0].startswith("logging failed:")
    assert "fixture message" not in reports[0]


def test_logging_writes_private_files_and_rotates(tmp_path):
    module = load_runtime_paths()
    log_file = tmp_path / "logs" / "addon.log"

    assert module.append_log_resilient(
        log_file,
        "first",
        max_bytes=1,
        backup_count=2,
    )
    assert module.append_log_resilient(
        log_file,
        "second",
        max_bytes=1,
        backup_count=2,
    )

    assert "second" in log_file.read_text(encoding="utf-8")
    assert "first" in (tmp_path / "logs" / "addon.log.1").read_text(encoding="utf-8")
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
