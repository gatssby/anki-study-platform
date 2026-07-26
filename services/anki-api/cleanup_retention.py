#!/usr/bin/env python3
"""Safe retention cleanup for anki-gpt-sync generated files."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_BASE_DIR = Path(os.environ.get("ANKI_GPT_BASE_DIR", "/home/ubuntu/anki-gpt-sync"))
PENDING_OPERATION_STATUSES = {
    "pending",
    "pending_addon_execution",
    "queued",
    "running",
    "in_progress",
    "processing",
    "started",
}
FINAL_OPERATION_STATUSES = {
    "applied",
    "partially_applied",
    "done",
    "failed",
    "skipped",
    "completed",
    "complete",
    "finished",
    "success",
    "succeeded",
    "cancelled",
    "canceled",
}


@dataclass(frozen=True)
class Candidate:
    path: Path
    size: int
    reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove old generated snapshots, logs, temporary media files and "
            "finalized operation records without touching active Anki data."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Only report what would be removed.")
    mode.add_argument("--apply", action="store_true", help="Remove the reported candidates.")
    parser.add_argument("--keep-data-json", type=int, default=20, help="Snapshots to keep in data/.")
    parser.add_argument("--logs-days", type=float, default=14, help="Age threshold for logs.")
    parser.add_argument("--operations-days", type=float, default=90, help="Age threshold for finalized operations.")
    parser.add_argument("--media-temp-days", type=float, default=1, help="Age threshold for media tmp*/paste-* files.")
    parser.add_argument("--log-archives-days", type=float, default=30, help="Age threshold for compressed request logs.")
    parser.add_argument("--backups-days", type=float, default=90, help="Age threshold for local backup files.")
    parser.add_argument("--temporary-days", type=float, default=2, help="Age threshold for staging/tmp artifacts.")
    parser.add_argument("--keep-generations", type=int, default=3, help="Verified state generations to keep.")
    parser.add_argument(
        "--include-sensitive-log-archives",
        action="store_true",
        help="Allow compressed root request logs to become candidates; requires explicit opt-in.",
    )
    parser.add_argument(
        "--include-backups",
        action="store_true",
        help="Allow old backup files to become candidates; requires explicit opt-in.",
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=DEFAULT_BASE_DIR,
        help="Project base directory. Defaults to ANKI_GPT_BASE_DIR or /home/ubuntu/anki-gpt-sync.",
    )
    return parser.parse_args()


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def safe_child(base_dir: Path, *parts: str) -> Path:
    path = base_dir.joinpath(*parts)
    if not is_relative_to(path, base_dir):
        raise ValueError(f"refusing path outside base dir: {path}")
    return path


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def tree_size(path: Path) -> int:
    if path.is_file():
        return file_size(path)
    return sum(file_size(item) for item in path.rglob("*") if item.is_file() and not item.is_symlink())


def generation_candidates(state_dir: Path, keep: int) -> tuple[list[Candidate], int]:
    generations_dir = safe_child(state_dir, "generations")
    if not generations_dir.exists():
        return [], 0
    directories = sorted(
        (path for path in generations_dir.glob("gen-*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    protected = {path.name for path in directories[:max(2, keep)]}
    current_path = safe_child(state_dir, "current.json")
    if current_path.exists():
        try:
            current = json.loads(current_path.read_text(encoding="utf-8"))
            generation_id = current.get("generation_id")
            if isinstance(generation_id, str):
                protected.add(generation_id)
                manifest_path = generations_dir / generation_id / "manifest.json"
                if manifest_path.exists():
                    previous = json.loads(manifest_path.read_text(encoding="utf-8")).get("previous_generation_id")
                    if isinstance(previous, str):
                        protected.add(previous)
        except Exception:
            # Invalid pointer means no generation deletion is safe.
            return [], len(directories)
    return ([
        Candidate(path=path, size=tree_size(path), reason=f"old inactive generation; keeping {max(2, keep)} newest plus active/previous")
        for path in directories
        if path.name not in protected
    ], len(directories))


def is_older_than(path: Path, cutoff_ts: float) -> bool:
    try:
        return path.stat().st_mtime < cutoff_ts
    except OSError:
        return False


def list_files(directory: Path, patterns: Iterable[str]) -> list[Path]:
    if not directory.exists():
        return []
    if not directory.is_dir():
        return []
    files: list[Path] = []
    for pattern in patterns:
        files.extend(p for p in directory.glob(pattern) if p.is_file() and not p.is_symlink())
    return sorted(set(files))


def data_json_candidates(data_dir: Path, keep: int) -> tuple[list[Candidate], int]:
    files = list_files(data_dir, ("*.json",))
    files.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    keep_count = max(1, keep)
    stale = files[keep_count:]
    return (
        [
            Candidate(path=p, size=file_size(p), reason=f"older data snapshot; keeping newest {keep_count}")
            for p in stale
        ],
        len(files),
    )


def media_temp_candidates(media_dir: Path, days: float) -> tuple[list[Candidate], int]:
    cutoff = time.time() - days * 86400
    files = list_files(media_dir, ("tmp*", "paste-*"))
    candidates = [
        Candidate(path=p, size=file_size(p), reason=f"temporary media older than {days:g} day(s)")
        for p in files
        if is_older_than(p, cutoff)
    ]
    return candidates, len(files)


def log_candidates(log_dirs: Iterable[Path], days: float) -> tuple[list[Candidate], int]:
    cutoff = time.time() - days * 86400
    candidates: list[Candidate] = []
    scanned = 0
    for directory in log_dirs:
        files = list_files(directory, ("*.log", "*.log.*", "*.jsonl", "*.txt"))
        scanned += len(files)
        candidates.extend(
            Candidate(path=p, size=file_size(p), reason=f"log older than {days:g} day(s)")
            for p in files
            if is_older_than(p, cutoff)
        )
    return candidates, scanned


def load_operation_status(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if not isinstance(status, str):
        return None
    return status.strip().lower()


def operation_candidates(operation_dirs: Iterable[Path], days: float) -> tuple[list[Candidate], int, int]:
    cutoff = time.time() - days * 86400
    candidates: list[Candidate] = []
    scanned = 0
    skipped_pending_or_unknown = 0
    for directory in operation_dirs:
        files = list_files(directory, ("*.json",))
        scanned += len(files)
        for path in files:
            if not is_older_than(path, cutoff):
                continue
            status = load_operation_status(path)
            if status in PENDING_OPERATION_STATUSES or status not in FINAL_OPERATION_STATUSES:
                skipped_pending_or_unknown += 1
                continue
            candidates.append(
                Candidate(
                    path=path,
                    size=file_size(path),
                    reason=f"finalized operation status={status!r} older than {days:g} day(s)",
                )
            )
    return candidates, scanned, skipped_pending_or_unknown


def age_candidates(paths: Iterable[Path], days: float, reason: str) -> tuple[list[Candidate], int]:
    cutoff = time.time() - days * 86400
    unique = sorted({path for path in paths if path.exists() and not path.is_symlink()})
    return ([
        Candidate(path=path, size=tree_size(path), reason=f"{reason} older than {days:g} day(s)")
        for path in unique
        if is_older_than(path, cutoff)
    ], len(unique))


def compressed_log_candidates(base_dir: Path, days: float) -> tuple[list[Candidate], int]:
    return age_candidates(
        (path for path in base_dir.glob("requests.log*.gz") if path.is_file()),
        days,
        "compressed request log",
    )


def backup_candidates(base_dir: Path, days: float) -> tuple[list[Candidate], int]:
    paths: list[Path] = []
    for root in (base_dir, safe_child(base_dir, "scripts"), safe_child(base_dir, "backups")):
        if not root.is_dir():
            continue
        for pattern in ("*.bak", "*.bak-*", "*.backup", "*.backup-*"):
            paths.extend(path for path in root.rglob(pattern) if path.is_file())
    return age_candidates(paths, days, "backup artifact")


def temporary_candidates(base_dir: Path, state_dir: Path, days: float) -> tuple[list[Candidate], int]:
    paths: list[Path] = []
    for directory in (
        safe_child(base_dir, "staging"),
        safe_child(state_dir, "staging"),
        safe_child(state_dir, "generations"),
    ):
        if not directory.is_dir():
            continue
        paths.extend(path for path in directory.glob("*.tmp") if not path.is_symlink())
        paths.extend(path for path in directory.glob(".*.tmp") if not path.is_symlink())
    return age_candidates(paths, days, "temporary/staging artifact")


def print_section(
    name: str,
    directory_label: str,
    candidates: list[Candidate],
    scanned: int,
    apply: bool,
    enabled: bool = True,
) -> int:
    total = sum(item.size for item in candidates)
    action = "held_manual" if not enabled else ("removed" if apply else "would_remove")
    print(f"\n[{name}]")
    print(f"directory: {directory_label}")
    print(f"scanned_files: {scanned}")
    print(f"candidate_files: {len(candidates)}")
    print(f"estimated_space: {human_size(total)}")
    print(f"action: {action}")
    for item in candidates:
        print(f"- {item.path} | {human_size(item.size)} | {item.reason}")
    return total


def remove_candidates(candidates: Iterable[Candidate]) -> tuple[int, int]:
    removed = 0
    bytes_removed = 0
    for item in candidates:
        try:
            size = file_size(item.path)
            if item.path.is_dir() and not item.path.is_symlink():
                shutil.rmtree(item.path)
            else:
                item.path.unlink()
            removed += 1
            bytes_removed += size
        except FileNotFoundError:
            continue
        except IsADirectoryError:
            continue
    return removed, bytes_removed


def main() -> int:
    args = parse_args()
    base_dir = args.base_dir.expanduser().resolve()
    apply = bool(args.apply)

    if args.keep_data_json < 1:
        print("ERROR: --keep-data-json must be at least 1", file=sys.stderr)
        return 2
    if args.keep_generations < 2:
        print("ERROR: --keep-generations must be at least 2", file=sys.stderr)
        return 2
    if any(value < 0 for value in (
        args.logs_days,
        args.operations_days,
        args.media_temp_days,
        args.log_archives_days,
        args.backups_days,
        args.temporary_days,
    )):
        print("ERROR: day thresholds must be zero or positive", file=sys.stderr)
        return 2

    data_dir = safe_child(base_dir, "data")
    state_dir = safe_child(base_dir, "state")
    media_dir = safe_child(state_dir, "media")
    log_dirs = [
        safe_child(base_dir, "logs"),
        safe_child(state_dir, "federal_online", "logs"),
    ]
    operation_dirs = [
        safe_child(state_dir, "organization", "operations"),
        safe_child(state_dir, "tagging", "operations"),
    ]

    print("anki-gpt-sync retention cleanup")
    print(f"mode: {'apply' if apply else 'dry-run'}")
    print(f"base_dir: {base_dir}")
    print(f"keep_data_json: {max(1, args.keep_data_json)}")
    print(f"logs_days: {args.logs_days:g}")
    print(f"operations_days: {args.operations_days:g}")
    print(f"media_temp_days: {args.media_temp_days:g}")
    print(f"log_archives_days: {args.log_archives_days:g}")
    print(f"backups_days: {args.backups_days:g}")
    print(f"temporary_days: {args.temporary_days:g}")

    all_candidates: list[Candidate] = []

    generation_cleanup, generations_scanned = generation_candidates(state_dir, args.keep_generations)
    print_section("state generations", str(state_dir / "generations"), generation_cleanup, generations_scanned, apply)
    all_candidates.extend(generation_cleanup)

    data_candidates, data_scanned = data_json_candidates(data_dir, args.keep_data_json)
    print_section("data snapshots", str(data_dir), data_candidates, data_scanned, apply)
    all_candidates.extend(data_candidates)

    media_candidates, media_scanned = media_temp_candidates(media_dir, args.media_temp_days)
    print_section("media temporary files", str(media_dir), media_candidates, media_scanned, apply)
    all_candidates.extend(media_candidates)

    log_cleanup_candidates, log_scanned = log_candidates(log_dirs, args.logs_days)
    print_section("logs", ", ".join(str(p) for p in log_dirs), log_cleanup_candidates, log_scanned, apply)
    all_candidates.extend(log_cleanup_candidates)

    compressed_logs, compressed_logs_scanned = compressed_log_candidates(base_dir, args.log_archives_days)
    print_section(
        "compressed request logs",
        str(base_dir),
        compressed_logs,
        compressed_logs_scanned,
        apply,
        enabled=args.include_sensitive_log_archives,
    )
    if args.include_sensitive_log_archives:
        all_candidates.extend(compressed_logs)

    backups, backups_scanned = backup_candidates(base_dir, args.backups_days)
    print_section(
        "backup artifacts",
        str(base_dir),
        backups,
        backups_scanned,
        apply,
        enabled=args.include_backups,
    )
    if args.include_backups:
        all_candidates.extend(backups)

    temporary, temporary_scanned = temporary_candidates(base_dir, state_dir, args.temporary_days)
    print_section("temporary artifacts", str(state_dir), temporary, temporary_scanned, apply)
    all_candidates.extend(temporary)

    op_candidates, op_scanned, op_skipped = operation_candidates(operation_dirs, args.operations_days)
    print_section("finalized operations", ", ".join(str(p) for p in operation_dirs), op_candidates, op_scanned, apply)
    print(f"operations_skipped_pending_or_unknown: {op_skipped}")
    all_candidates.extend(op_candidates)

    estimated_total = sum(item.size for item in all_candidates)
    print("\n[summary]")
    print(f"candidate_files_total: {len(all_candidates)}")
    print(f"estimated_space_total: {human_size(estimated_total)}")

    if apply:
        removed, bytes_removed = remove_candidates(all_candidates)
        print(f"removed_files_total: {removed}")
        print(f"space_removed_total: {human_size(bytes_removed)}")
    else:
        print("removed_files_total: 0")
        print("space_removed_total: 0 B")

    remaining_snapshots = len(list_files(data_dir, ("*.json",)))
    print(f"data_json_remaining: {remaining_snapshots}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
