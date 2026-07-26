"""Transactional, versioned state publication and thread-safe read cache."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone

from safe_io import atomic_write_bytes, atomic_write_json, canonical_json_bytes, fsync_directory, sha256_bytes


INDEX_SCHEMA_VERSION = 3
GENERATION_FILES = (
    "notes_index.json",
    "decks_index.json",
    "note_media_index.json",
    "snapshot_status.json",
)


def generation_id_now() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"gen-{stamp}-{uuid.uuid4().hex[:12]}"


def generation_counts(objects: dict) -> dict:
    """Return unambiguous counts derived from the exact generation objects."""
    decks_index = objects.get("decks_index.json")
    notes_index = objects.get("notes_index.json")
    media_index = objects.get("note_media_index.json")

    if not isinstance(decks_index, dict):
        raise ValueError("generation_invalid_decks_index")
    decks = decks_index.get("decks", [])
    if not isinstance(decks, list):
        raise ValueError("generation_invalid_deck_rows")

    declared_decks = decks_index.get("total_decks")
    if declared_decks is not None and declared_decks != len(decks):
        raise ValueError("generation_total_deck_count_mismatch")

    if isinstance(notes_index, dict):
        note_count = len(notes_index)
    elif isinstance(notes_index, list):
        note_count = len(notes_index)
    else:
        raise ValueError("generation_invalid_notes_index")

    if isinstance(media_index, dict):
        media_note_count = len(media_index)
    elif isinstance(media_index, list):
        media_note_count = len(media_index)
    else:
        raise ValueError("generation_invalid_media_index")

    total_cards = decks_index.get("total_cards")
    if total_cards is not None and not isinstance(total_cards, int):
        raise ValueError("generation_invalid_total_card_count")

    return {
        "total_deck_count": len(decks),
        "indexed_deck_count": len(decks),
        "deck_partition_count": 1,
        "total_card_count": total_cards,
        "total_note_count": note_count,
        "media_note_count": media_note_count,
    }


def publish_generation(state_dir: Path, objects: dict, metadata: dict | None = None) -> dict:
    missing = [name for name in GENERATION_FILES if name not in objects]
    if missing:
        raise ValueError(f"generation_missing_files: {missing}")

    state_dir = Path(state_dir)
    generations_dir = state_dir / "generations"
    generations_dir.mkdir(parents=True, exist_ok=True)
    generation_id = generation_id_now()
    temporary_dir = generations_dir / f".{generation_id}.tmp"
    final_dir = generations_dir / generation_id
    temporary_dir.mkdir()

    manifest_files = {}
    try:
        for name in GENERATION_FILES:
            body = canonical_json_bytes(objects[name])
            path = temporary_dir / name
            with path.open("wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            # Parse the exact bytes before they can become active.
            json.loads(body.decode("utf-8"))
            manifest_files[name] = {"sha256": sha256_bytes(body), "bytes": len(body)}

        manifest_metadata = dict(metadata or {})
        # Never trust a caller's len(decks_index) calculation: that is the
        # number of envelope keys, not the number of deck rows.
        manifest_metadata["counts"] = generation_counts(objects)
        current_path = state_dir / "current.json"
        if current_path.exists() and "previous_generation_id" not in manifest_metadata:
            try:
                previous = json.loads(current_path.read_text(encoding="utf-8")).get("generation_id")
                if isinstance(previous, str):
                    manifest_metadata["previous_generation_id"] = previous
            except Exception:
                pass
        manifest = {
            "index_schema_version": INDEX_SCHEMA_VERSION,
            "generation_id": generation_id,
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "files": manifest_files,
            **manifest_metadata,
        }
        manifest_body = canonical_json_bytes(manifest)
        with (temporary_dir / "manifest.json").open("wb") as handle:
            handle.write(manifest_body)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(temporary_dir)
        temporary_dir.replace(final_dir)
        fsync_directory(generations_dir)
        atomic_write_json(state_dir / "current.json", {
            "index_schema_version": INDEX_SCHEMA_VERSION,
            "generation_id": generation_id,
            "manifest_sha256": sha256_bytes(manifest_body),
        })
        return manifest
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise


def validate_generation(state_dir: Path, pointer: dict) -> tuple[Path, dict]:
    generation_id = pointer.get("generation_id") if isinstance(pointer, dict) else None
    if not isinstance(generation_id, str) or not generation_id.startswith("gen-"):
        raise ValueError("invalid_generation_pointer")
    root = (Path(state_dir) / "generations").resolve()
    generation_dir = (root / generation_id).resolve()
    generation_dir.relative_to(root)
    manifest_path = generation_dir / "manifest.json"
    manifest_body = manifest_path.read_bytes()
    if pointer.get("manifest_sha256") != sha256_bytes(manifest_body):
        raise ValueError("generation_manifest_hash_mismatch")
    manifest = json.loads(manifest_body.decode("utf-8"))
    if manifest.get("generation_id") != generation_id:
        raise ValueError("generation_id_mismatch")
    if manifest.get("index_schema_version") != INDEX_SCHEMA_VERSION:
        raise ValueError("unsupported_index_schema_version")
    for name in GENERATION_FILES:
        expected = manifest.get("files", {}).get(name, {})
        body = (generation_dir / name).read_bytes()
        if expected.get("sha256") != sha256_bytes(body) or expected.get("bytes") != len(body):
            raise ValueError(f"generation_file_hash_mismatch: {name}")
    return generation_dir, manifest


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    reload_failures: int = 0


class GenerationStateCache:
    def __init__(self, state_dir: Path, legacy_paths: dict[str, Path]):
        self.state_dir = Path(state_dir)
        self.legacy_paths = {name: Path(path) for name, path in legacy_paths.items()}
        self._lock = threading.RLock()
        self._signature = None
        self._objects: dict | None = None
        self._manifest: dict | None = None
        self.stats = CacheStats()

    def _pointer_signature(self):
        path = self.state_dir / "current.json"
        if not path.exists():
            return ("legacy",) + tuple(
                (name, p.stat().st_mtime_ns if p.exists() else None, p.stat().st_size if p.exists() else None)
                for name, p in sorted(self.legacy_paths.items())
            )
        stat = path.stat()
        return ("generation", stat.st_mtime_ns, stat.st_size, path.read_bytes())

    def _objects_from_generation(self, generation_dir: Path, manifest: dict):
        return {
            name: json.loads((generation_dir / name).read_text(encoding="utf-8"))
            for name in GENERATION_FILES
        }, manifest

    def _load_legacy(self):
        objects = {
            name: json.loads(path.read_text(encoding="utf-8"))
            for name, path in self.legacy_paths.items()
        }
        return objects, {"generation_id": "legacy", "index_schema_version": 2}

    def _recover_previous_generation(self):
        generations = self.state_dir / "generations"
        if generations.exists():
            for candidate in sorted(generations.glob("gen-*"), reverse=True):
                if not candidate.is_dir() or candidate.is_symlink():
                    continue
                try:
                    manifest_body = (candidate / "manifest.json").read_bytes()
                    pointer = {
                        "generation_id": candidate.name,
                        "manifest_sha256": sha256_bytes(manifest_body),
                    }
                    generation_dir, manifest = validate_generation(self.state_dir, pointer)
                    objects, manifest = self._objects_from_generation(generation_dir, manifest)
                    manifest = dict(manifest)
                    manifest["recovered_from_invalid_pointer"] = True
                    return objects, manifest
                except Exception:
                    continue
        return self._load_legacy()

    def _load(self):
        pointer_path = self.state_dir / "current.json"
        if pointer_path.exists():
            try:
                pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
                generation_dir, manifest = validate_generation(self.state_dir, pointer)
                return self._objects_from_generation(generation_dir, manifest)
            except Exception:
                if self._objects is not None:
                    raise
                return self._recover_previous_generation()
        return self._load_legacy()

    def snapshot(self) -> tuple[dict, dict]:
        signature = self._pointer_signature()
        with self._lock:
            if self._objects is not None and self._manifest is not None and signature == self._signature:
                self.stats.hits += 1
                return self._objects, self._manifest
            try:
                objects, manifest = self._load()
            except Exception:
                self.stats.reload_failures += 1
                # A malformed new pointer must not evict the last verified generation.
                if self._objects is not None and self._manifest is not None:
                    return self._objects, self._manifest
                raise
            self._objects = objects
            self._manifest = manifest
            self._signature = signature
            self.stats.misses += 1
            if manifest.get("recovered_from_invalid_pointer"):
                self.stats.reload_failures += 1
            return objects, manifest

    def metrics(self) -> dict:
        with self._lock:
            return {
                "hits": self.stats.hits,
                "misses": self.stats.misses,
                "reload_failures": self.stats.reload_failures,
                "generation_id": (self._manifest or {}).get("generation_id"),
            }
