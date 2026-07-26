"""Dependency-free filesystem primitives shared by server-side components."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Callable, Iterator


def canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def atomic_output_path(
    path: Path | str,
    *,
    file_mode: int | None = None,
    directory_mode: int | None = None,
) -> Iterator[Path]:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=directory_mode or 0o777)
    if directory_mode is not None:
        destination.parent.chmod(directory_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if file_mode is not None:
            temporary.chmod(file_mode)
        yield temporary
        if file_mode is not None:
            temporary.chmod(file_mode)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if file_mode is not None:
            destination.chmod(file_mode)
        fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_bytes(path: Path | str, body: bytes) -> None:
    with atomic_output_path(path) as temporary:
        temporary.write_bytes(body)


def atomic_write_json(path: Path | str, value: object) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value))


def safe_resolve_under(root: Path | str, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in relative_path:
        raise ValueError("invalid_relative_path")
    resolved_root = Path(root).resolve()
    candidate = (resolved_root / Path(*relative.parts)).resolve()
    candidate.relative_to(resolved_root)
    return candidate
