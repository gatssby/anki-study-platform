#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any


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
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{session_path.name}.",
        suffix=".tmp",
        dir=session_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    temporary_path.chmod(SESSION_FILE_MODE)

    previous_umask = os.umask(0o077)
    try:
        context.storage_state(path=str(temporary_path))
        temporary_path.chmod(SESSION_FILE_MODE)
        os.replace(temporary_path, session_path)
        session_path.chmod(SESSION_FILE_MODE)
    finally:
        os.umask(previous_umask)
        temporary_path.unlink(missing_ok=True)

    return session_path
