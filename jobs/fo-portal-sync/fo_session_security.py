#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any

from safe_io import atomic_output_path

SESSION_FILE_MODE = 0o600
SESSION_DIRECTORY_MODE = 0o700


def protect_storage_state(path: str | Path) -> Path:
    """Restrict an existing Playwright storage state without changing its contents."""
    session_path = Path(path).expanduser()
    session_path.parent.mkdir(
        parents=True,
        exist_ok=True,
        mode=SESSION_DIRECTORY_MODE,
    )
    session_path.parent.chmod(SESSION_DIRECTORY_MODE)
    if session_path.exists():
        session_path.chmod(SESSION_FILE_MODE)
    return session_path


def save_storage_state(context: Any, path: str | Path) -> Path:
    """Persist storage state atomically, keeping it private throughout the write."""
    session_path = protect_storage_state(path)
    with atomic_output_path(
        session_path,
        file_mode=SESSION_FILE_MODE,
        directory_mode=SESSION_DIRECTORY_MODE,
    ) as temporary_path:
        context.storage_state(path=str(temporary_path))
    return session_path
