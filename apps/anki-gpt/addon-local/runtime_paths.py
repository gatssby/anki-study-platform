"""Canonical, repository-independent runtime paths for the Anki GPT add-on."""

from __future__ import annotations

import fcntl
import os
import shutil
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

ADDON_ID = "anki_gpt_sync"
LEGACY_RUNTIME_NAME = "anki-gpt-files"
RUNTIME_DIR_MODE = 0o700
RUNTIME_FILE_MODE = 0o600


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    logs: Path
    state: Path
    cache: Path
    staging: Path
    backups: Path
    organization: Path
    log_file: Path
    token_file: Path
    reorganization_log_file: Path
    operations_index_file: Path


def report_runtime_issue(message: str) -> None:
    """Report filesystem trouble without ever interrupting add-on import."""
    rendered = f"[{ADDON_ID}] {message}"
    try:
        print(rendered, file=sys.stderr)
    except Exception:
        try:
            print(rendered)
        except Exception:
            pass


def canonical_runtime_root(
    *,
    home: Path | None = None,
    platform: str | None = None,
    environ: dict[str, str] | None = None,
) -> Path:
    """Return the platform-specific application-data directory for the add-on."""
    selected_home = Path.home() if home is None else Path(home)
    selected_platform = sys.platform if platform is None else platform
    selected_environ = os.environ if environ is None else environ

    override = selected_environ.get("ANKI_GPT_RUNTIME_DIR", "").strip()
    if override:
        override_path = Path(override).expanduser()
        return override_path if override_path.is_absolute() else selected_home / override_path
    if selected_platform == "darwin":
        return (
            selected_home
            / "Library"
            / "Application Support"
            / "Anki2"
            / "addon-data"
            / ADDON_ID
        )
    if selected_platform == "win32":
        app_data = selected_environ.get("APPDATA", "").strip()
        base = Path(app_data) if app_data else selected_home / "AppData" / "Roaming"
        return base / "Anki2" / "addon-data" / ADDON_ID

    xdg_data_home = selected_environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg_data_home) if xdg_data_home else selected_home / ".local" / "share"
    return base / "Anki2" / "addon-data" / ADDON_ID


def build_runtime_paths(root: Path) -> RuntimePaths:
    root = Path(root)
    return RuntimePaths(
        root=root,
        logs=root / "logs",
        state=root / "state",
        cache=root / "cache",
        staging=root / "staging",
        backups=root / "backups",
        organization=root / "organization",
        log_file=root / "logs" / "anki_gpt_sync.log",
        token_file=root / "tagging_token.txt",
        reorganization_log_file=root / "organization_move_log.jsonl",
        operations_index_file=root / "organization" / "operations_index.json",
    )


def default_runtime_paths() -> RuntimePaths:
    return build_runtime_paths(canonical_runtime_root())


_ACTIVE_RUNTIME_PATHS = default_runtime_paths()


def get_runtime_paths() -> RuntimePaths:
    return _ACTIVE_RUNTIME_PATHS


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _unique_legacy_destination(path: Path) -> Path:
    candidate = path.with_name(f"{path.name}.legacy")
    index = 2
    while _path_exists(candidate):
        candidate = path.with_name(f"{path.name}.legacy.{index}")
        index += 1
    return candidate


def _copy_symlink(source: Path, destination: Path) -> None:
    destination.symlink_to(os.readlink(source), target_is_directory=source.is_dir())


def _merge_directory(source: Path, destination: Path, *, move: bool) -> None:
    """Merge without overwriting; conflicting legacy entries receive a suffix."""
    source_mode = source.stat().st_mode & 0o777
    destination.mkdir(parents=True, exist_ok=True, mode=source_mode)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_symlink():
            if _path_exists(target):
                target = _unique_legacy_destination(target)
            if move:
                child.rename(target)
            else:
                _copy_symlink(child, target)
            continue

        if child.is_dir() and target.is_dir() and not target.is_symlink():
            _merge_directory(child, target, move=move)
            continue

        if _path_exists(target):
            target = _unique_legacy_destination(target)
        if move:
            shutil.move(str(child), str(target))
        elif child.is_dir():
            shutil.copytree(child, target, symlinks=True, copy_function=shutil.copy2)
        else:
            shutil.copy2(child, target, follow_symlinks=False)

    try:
        shutil.copystat(source, destination, follow_symlinks=True)
    except OSError:
        pass
    if move:
        source.rmdir()


def _migrate_legacy_path(
    legacy_root: Path,
    canonical_root: Path,
    report: Callable[[str], None],
) -> str:
    try:
        legacy_root.lstat()
    except FileNotFoundError:
        return "missing"

    if legacy_root.is_symlink():
        if not legacy_root.exists():
            try:
                legacy_root.unlink()
                return "broken_symlink_removed"
            except OSError as exc:
                report(f"could not remove broken legacy symlink: {type(exc).__name__}: {exc}")
                return "broken_symlink_ignored"

        if not legacy_root.is_dir():
            try:
                legacy_root.unlink()
                report("removed legacy symlink because its target is not a directory")
                return "non_directory_symlink_removed"
            except OSError as exc:
                report(f"could not remove legacy symlink: {type(exc).__name__}: {exc}")
                return "non_directory_symlink_ignored"

        try:
            if legacy_root.resolve() == canonical_root.resolve(strict=False):
                legacy_root.unlink()
                return "canonical_symlink_removed"
            canonical_root.parent.mkdir(parents=True, exist_ok=True, mode=RUNTIME_DIR_MODE)
            _merge_directory(legacy_root, canonical_root, move=False)
            legacy_root.unlink()
            return "valid_symlink_migrated"
        except OSError as exc:
            report(f"could not migrate legacy symlink: {type(exc).__name__}: {exc}")
            return "valid_symlink_migration_failed"

    if legacy_root.is_dir():
        try:
            canonical_root.parent.mkdir(parents=True, exist_ok=True, mode=RUNTIME_DIR_MODE)
            if not _path_exists(canonical_root):
                legacy_root.rename(canonical_root)
                return "directory_moved"
            if canonical_root.is_dir() and not canonical_root.is_symlink():
                _merge_directory(legacy_root, canonical_root, move=True)
                return "directory_merged"
            report("canonical runtime path is occupied by a non-directory; legacy directory kept")
            return "canonical_path_blocked"
        except OSError as exc:
            report(f"could not migrate legacy directory: {type(exc).__name__}: {exc}")
            return "directory_migration_failed"

    report("legacy runtime path is occupied by a file; ignoring it")
    return "file_ignored"


def _prepare_runtime_directories(paths: RuntimePaths) -> None:
    for path in (
        paths.root,
        paths.logs,
        paths.state,
        paths.cache,
        paths.staging,
        paths.backups,
        paths.organization,
    ):
        path.mkdir(parents=True, exist_ok=True, mode=RUNTIME_DIR_MODE)


def initialize_runtime_paths(
    *,
    home: Path | None = None,
    platform: str | None = None,
    environ: dict[str, str] | None = None,
    report: Callable[[str], None] = report_runtime_issue,
) -> tuple[RuntimePaths, str]:
    """Migrate legacy data and prepare runtime directories, without raising."""
    global _ACTIVE_RUNTIME_PATHS

    selected_home = Path.home() if home is None else Path(home)
    paths = build_runtime_paths(
        canonical_runtime_root(home=selected_home, platform=platform, environ=environ)
    )
    migration = "initialization_failed"
    try:
        migration = _migrate_legacy_path(
            selected_home / LEGACY_RUNTIME_NAME,
            paths.root,
            report,
        )
    except Exception as exc:
        report(f"unexpected legacy migration failure: {type(exc).__name__}: {exc}")

    try:
        _prepare_runtime_directories(paths)
    except Exception as exc:
        report(f"could not prepare runtime directories: {type(exc).__name__}: {exc}")

    _ACTIVE_RUNTIME_PATHS = paths
    return paths, migration


def append_log_resilient(
    log_file: Path,
    message: str,
    *,
    max_bytes: int,
    backup_count: int,
    thread_lock: threading.Lock | None = None,
    report: Callable[[str], None] = report_runtime_issue,
) -> bool:
    """Append and rotate a private log; return False instead of raising."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    selected_lock = thread_lock if thread_lock is not None else threading.Lock()
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True, mode=RUNTIME_DIR_MODE)
        try:
            log_file.parent.chmod(RUNTIME_DIR_MODE)
        except OSError:
            pass
        with selected_lock:
            lock_path = log_file.with_name(f".{log_file.name}.lock")
            with lock_path.open("a", encoding="utf-8") as lock:
                try:
                    lock_path.chmod(RUNTIME_FILE_MODE)
                except OSError:
                    pass
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                if log_file.exists() and log_file.stat().st_size >= max_bytes:
                    log_file.with_name(f"{log_file.name}.{backup_count}").unlink(
                        missing_ok=True
                    )
                    for index in range(backup_count - 1, 0, -1):
                        source = log_file.with_name(f"{log_file.name}.{index}")
                        if source.exists():
                            source.replace(
                                log_file.with_name(f"{log_file.name}.{index + 1}")
                            )
                    log_file.replace(log_file.with_name(f"{log_file.name}.1"))
                with log_file.open("a", encoding="utf-8") as stream:
                    stream.write(f"[{timestamp}] {message}\n")
                try:
                    log_file.chmod(RUNTIME_FILE_MODE)
                    for index in range(1, backup_count + 1):
                        rotated = log_file.with_name(f"{log_file.name}.{index}")
                        if rotated.exists():
                            rotated.chmod(RUNTIME_FILE_MODE)
                except OSError:
                    pass
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return True
    except Exception as exc:
        report(f"logging failed: {type(exc).__name__}: {exc}")
        return False
