#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PROJECT_ROOT = Path("/opt/cronograma-fo")
DEFAULT_INDEX_JSON = (
    DEFAULT_PROJECT_ROOT / "state" / "federal_online" / "cronograma_fo" / "aulas_index.json"
)
DEFAULT_MANIFEST = (
    DEFAULT_PROJECT_ROOT
    / "state"
    / "federal_online"
    / "cronograma_fo"
    / "video_upload_manifest.json"
)
DEFAULT_TMP_DIR = DEFAULT_PROJECT_ROOT / "tmp" / "video_downloads"
DEFAULT_LOGS_DIR = DEFAULT_PROJECT_ROOT / "state" / "federal_online" / "logs"
DEFAULT_REPORTS_DIR = DEFAULT_PROJECT_ROOT / "state" / "federal_online" / "reports"
DEFAULT_QUEUE_DB = Path("/home/ubuntu/fo-transcricoes-system/queue.sqlite")
DEFAULT_RCLONE_REMOTE = "onedrive:"
DEFAULT_REMOTE_BASE = "Federal Online/Extensivo UFPR 2026"
DEFAULT_MIN_FREE_GB = 6.0
MANIFEST_SCHEMA_VERSION = 1
AUDIT_REPORT_COLUMNS = [
    "stable_key",
    "portal_path",
    "portal_title",
    "video_url",
    "previous_video_url",
    "remote_path",
    "queue_remote_path",
    "remote_exists",
    "local_exists",
    "manifest_status",
    "expected_status",
    "detected_issue",
    "action_dry_run",
    "transcription_action_dry_run",
    "risk_level",
    "reason",
    "remote_size",
    "manifest_file_size",
    "duration_seconds",
    "queue_status",
    "queue_id",
]
INVALID_NAME_CHARS_RE = re.compile(r'[\/\\:\*\?"<>\|]+')
CONTROL_WHITESPACE_RE = re.compile(r"[\r\n\t]+")
MULTISPACE_RE = re.compile(r"\s+")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class Logger:
    def __init__(self, paths: list[Path]) -> None:
        self.paths = paths
        for path in self.paths:
            path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        print(line, flush=True)
        for path in self.paths:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()


class PerItemFailure(RuntimeError):
    pass


@dataclass
class Candidate:
    aula: dict[str, Any]
    stable_key: str
    source_ref: str
    source_parts: list[str]
    title: str
    filename: str
    video_stream_url: str
    remote_path: str
    tmp_final_path: Path
    tmp_partial_path: Path


@dataclass
class StreamSelection:
    video_stream_index: int
    audio_stream_index: int | None
    width: int | None
    height: int | None
    bitrate: int | None
    program_id: int | None


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def summarize_exception(exc: BaseException) -> str:
    return f"{exc.__class__.__name__}: {exc}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Baixa videoaulas do Federal Online a partir do aulas_index.json, "
            "envia ao OneDrive via rclone e registra progresso em manifesto."
        )
    )
    parser.add_argument("--index-json", type=Path, default=DEFAULT_INDEX_JSON)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--tmp-dir", type=Path, default=DEFAULT_TMP_DIR)
    parser.add_argument("--rclone-remote", default=DEFAULT_RCLONE_REMOTE)
    parser.add_argument("--remote-base", default=DEFAULT_REMOTE_BASE)
    parser.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_GB)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--only-missing", action="store_true")
    parser.add_argument("--path-filter", action="append", default=[])
    parser.add_argument(
        "--audit-remote",
        action="store_true",
        help="Gera relatorio dry-run comparando indice, manifesto, OneDrive e fila de transcricao.",
    )
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    parser.add_argument("--queue-db", type=Path, default=DEFAULT_QUEUE_DB)
    parser.add_argument(
        "--plan-json",
        type=Path,
        default=None,
        help="Relatorio JSON previamente gerado para acoes apply.",
    )
    parser.add_argument(
        "--apply-delete-remote",
        action="store_true",
        help="Aplica delecao remota somente para itens would_delete_remote do --plan-json.",
    )
    parser.add_argument(
        "--apply-enqueue-transcriptions",
        action="store_true",
        help="Enfileira transcricoes somente para itens planejados no --plan-json, evitando duplicatas.",
    )
    parser.add_argument(
        "--apply-mark-stale",
        action="store_true",
        help="Marca transcricoes antigas como needs_review/stale quando suportado, usando --plan-json.",
    )
    parser.add_argument(
        "--apply-stable-key",
        action="append",
        default=[],
        help="Limita acoes apply a um stable_key exato. Pode repetir.",
    )
    return parser.parse_args()


def normalize_space(value: Any) -> str:
    text = "" if value is None else str(value)
    text = CONTROL_WHITESPACE_RE.sub(" ", text)
    return MULTISPACE_RE.sub(" ", text).strip()


def normalize_key(value: Any) -> str:
    return normalize_space(value).casefold()


def short_hash(value: str, length: int = 10) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def sanitize_name(value: Any, fallback: str, max_len: int | None = None) -> str:
    name = normalize_space(value)
    name = INVALID_NAME_CHARS_RE.sub(" - ", name)
    name = MULTISPACE_RE.sub(" ", name).strip(" .-_")
    if not name:
        name = fallback
    if name.upper() in WINDOWS_RESERVED_NAMES:
        name = f"{name}_"
    if max_len and len(name) > max_len:
        name = name[:max_len].rstrip(" .-_")
    return name or fallback


def source_ref_to_parts(source_ref: str) -> list[str]:
    raw_parts = [part for part in source_ref.split(">") if normalize_space(part)]
    parts = [sanitize_name(part, fallback="Sem pasta", max_len=120) for part in raw_parts]
    return parts or ["Sem pasta"]


def title_from_aula(aula: dict[str, Any]) -> str:
    return (
        normalize_space(aula.get("portal_title"))
        or normalize_space(aula.get("titulo_original"))
        or normalize_space(aula.get("nome_real_aula"))
        or normalize_space(aula.get("media_id"))
        or normalize_space(aula.get("item_id"))
        or "aula_sem_titulo"
    )


def source_ref_from_aula(aula: dict[str, Any]) -> str:
    return (
        normalize_space(aula.get("source_ref"))
        or normalize_space(aula.get("portal_path"))
        or normalize_space(aula.get("caminho"))
        or normalize_space(aula.get("disciplina"))
        or "Sem pasta"
    )


def path_filter_matches(aula: dict[str, Any], filters: list[str]) -> bool:
    clean_filters = [normalize_key(item) for item in filters if normalize_space(item)]
    if not clean_filters:
        return True
    haystack = " | ".join(
        normalize_key(aula.get(key))
        for key in ("source_ref", "portal_path", "caminho", "disciplina", "portal_subject", "portal_root")
    )
    return any(item in haystack for item in clean_filters)


def remote_join(remote: str, remote_base: str, folder_parts: list[str], filename: str) -> str:
    base = "/".join(
        sanitize_name(part, fallback="Sem pasta", max_len=120)
        for part in remote_base.replace("\\", "/").split("/")
        if normalize_space(part)
    )
    relative = "/".join([base, *folder_parts, filename]) if base else "/".join([*folder_parts, filename])
    clean_remote = remote.rstrip("/")
    separator = "" if clean_remote.endswith(":") else "/"
    return f"{clean_remote}{separator}{relative}"


def ensure_tool_exists(tool_name: str) -> None:
    if shutil.which(tool_name) is None:
        raise RuntimeError(f"{tool_name} nao encontrado no PATH.")


def load_index(index_json: Path) -> list[dict[str, Any]]:
    if not index_json.exists():
        raise RuntimeError(f"index_json inexistente: {index_json}")
    payload = json.loads(index_json.read_text(encoding="utf-8"))
    aulas = payload.get("aulas") if isinstance(payload, dict) else payload
    if not isinstance(aulas, list):
        raise RuntimeError("index_json invalido: esperado objeto com lista 'aulas' ou lista direta.")
    return [aula for aula in aulas if isinstance(aula, dict)]


def backup_invalid_manifest(manifest_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = manifest_path.with_name(f"{manifest_path.name}.invalid_{timestamp}.bak")
    shutil.copy2(manifest_path, backup_path)
    return backup_path


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.exists():
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "created_at": now_utc_iso(),
            "updated_at": now_utc_iso(),
            "items": [],
        }
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        backup_path = backup_invalid_manifest(manifest_path)
        raise RuntimeError(
            f"manifesto invalido; backup criado em {backup_path}; abortando sem sobrescrever."
        ) from exc

    if isinstance(payload, list):
        items = payload
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "created_at": now_utc_iso(),
            "updated_at": now_utc_iso(),
            "items": items,
        }
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        backup_path = backup_invalid_manifest(manifest_path)
        raise RuntimeError(
            f"manifesto invalido; backup criado em {backup_path}; abortando sem sobrescrever."
        )
    return payload


def write_manifest_atomic(manifest_path: Path, manifest: dict[str, Any]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest["schema_version"] = MANIFEST_SCHEMA_VERSION
    manifest["updated_at"] = now_utc_iso()
    tmp_path = manifest_path.with_name(f".{manifest_path.name}.tmp")
    tmp_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, manifest_path)


def manifest_maps(manifest: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    latest_by_key: dict[str, dict[str, Any]] = {}
    uploaded_by_key: dict[str, list[dict[str, Any]]] = {}
    for item in manifest.get("items", []):
        if not isinstance(item, dict):
            continue
        stable_key = normalize_space(item.get("stable_key"))
        if not stable_key:
            continue
        latest_by_key[stable_key] = item
        if item.get("status") == "uploaded":
            uploaded_by_key.setdefault(stable_key, []).append(item)
    return latest_by_key, uploaded_by_key


def upsert_manifest_item(manifest: dict[str, Any], new_item: dict[str, Any]) -> None:
    stable_key = new_item["stable_key"]
    items = [item for item in manifest.get("items", []) if not (
        isinstance(item, dict) and item.get("stable_key") == stable_key
    )]
    items.append(new_item)
    manifest["items"] = items


def eligible_aulas(aulas: list[dict[str, Any]], path_filters: list[str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    counts = {
        "not_launched_skipped": 0,
        "no_url_skipped": 0,
        "path_filtered_skipped": 0,
    }
    eligible: list[dict[str, Any]] = []
    for aula in aulas:
        if not path_filter_matches(aula, path_filters):
            counts["path_filtered_skipped"] += 1
            continue
        if aula.get("status") == "not_launched":
            counts["not_launched_skipped"] += 1
            continue
        if aula.get("status") != "ok":
            counts["no_url_skipped"] += 1
            continue
        if not normalize_space(aula.get("video_stream_url")):
            counts["no_url_skipped"] += 1
            continue
        eligible.append(aula)
    return eligible, counts


def build_candidates(
    aulas: list[dict[str, Any]],
    *,
    tmp_dir: Path,
    rclone_remote: str,
    remote_base: str,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    used_names: dict[tuple[str, str], str] = {}

    for aula in aulas:
        source_ref = source_ref_from_aula(aula)
        title = title_from_aula(aula)
        video_stream_url = normalize_space(aula.get("video_stream_url"))
        stable_key = f"{source_ref} | {title}"
        source_parts = source_ref_to_parts(source_ref)
        folder_key = "/".join(source_parts)
        base_name = sanitize_name(title, fallback=f"aula_{short_hash(stable_key)}", max_len=180)
        collision_key = (folder_key, base_name.casefold())
        if collision_key in used_names and used_names[collision_key] != stable_key:
            suffix = short_hash(video_stream_url or stable_key, length=8)
            base_name = sanitize_name(f"{base_name[:171].rstrip()} - {suffix}", fallback=f"aula_{suffix}", max_len=180)
        used_names[(folder_key, base_name.casefold())] = stable_key

        filename = f"{base_name}.mp4"
        tmp_stem = f"{short_hash(stable_key + '|' + video_stream_url, length=16)}_{base_name}"
        tmp_stem = sanitize_name(tmp_stem, fallback=short_hash(stable_key), max_len=200)
        tmp_final_path = tmp_dir / f"{tmp_stem}.mp4"
        tmp_partial_path = tmp_dir / f"{tmp_stem}.partial.mp4"
        remote_path = remote_join(rclone_remote, remote_base, source_parts, filename)

        candidates.append(
            Candidate(
                aula=aula,
                stable_key=stable_key,
                source_ref=source_ref,
                source_parts=source_parts,
                title=title,
                filename=filename,
                video_stream_url=video_stream_url,
                remote_path=remote_path,
                tmp_final_path=tmp_final_path,
                tmp_partial_path=tmp_partial_path,
            )
        )
    return candidates


def should_skip_candidate(
    candidate: Candidate,
    *,
    latest_by_key: dict[str, dict[str, Any]],
    uploaded_by_key: dict[str, list[dict[str, Any]]],
    force: bool,
    only_missing: bool,
) -> tuple[bool, str]:
    if force:
        return False, ""
    uploaded_items = uploaded_by_key.get(candidate.stable_key, [])
    if only_missing and uploaded_items:
        return True, "uploaded_existing"
    latest = latest_by_key.get(candidate.stable_key)
    if (
        latest
        and latest.get("status") == "uploaded"
        and latest.get("video_stream_url") == candidate.video_stream_url
    ):
        return True, "uploaded_same_url"
    return False, ""


def cleanup_tmp_file(path: Path, tmp_dir: Path, logger: Logger) -> None:
    try:
        resolved_path = path.resolve()
        resolved_tmp = tmp_dir.resolve()
        if resolved_path == resolved_tmp or resolved_tmp not in resolved_path.parents:
            logger.log(f"[warn] nao removido fora do tmp-dir: {path}")
            return
        if path.exists():
            path.unlink()
    except Exception as exc:
        logger.log(f"[warn] falha ao remover arquivo temporario={path} detalhe={summarize_exception(exc)}")


def check_free_space(tmp_dir: Path, min_free_gb: float) -> None:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(tmp_dir)
    free_gb = usage.free / (1024**3)
    if free_gb < min_free_gb:
        raise RuntimeError(
            f"espaco livre insuficiente em {tmp_dir}: {free_gb:.2f} GB livres; minimo={min_free_gb:.2f} GB."
        )


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def stream_bitrate(stream: dict[str, Any], program: dict[str, Any] | None = None) -> int | None:
    candidates = [
        stream.get("bit_rate"),
        (stream.get("tags") or {}).get("variant_bitrate"),
        (stream.get("tags") or {}).get("BPS"),
    ]
    if program:
        candidates.extend(
            [
                program.get("bit_rate"),
                (program.get("tags") or {}).get("variant_bitrate"),
                (program.get("tags") or {}).get("BPS"),
            ]
        )
    parsed = [value for value in (parse_int(candidate) for candidate in candidates) if value is not None]
    return max(parsed) if parsed else None


def run_ffprobe(candidate: Candidate) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file,http,https,tcp,tls,crypto",
        "-show_streams",
        "-show_programs",
        "-print_format",
        "json",
        candidate.video_stream_url,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise PerItemFailure("ffprobe nao encontrado no PATH.") from exc
    if result.returncode != 0:
        detail = normalize_space(result.stderr) or f"exit_code={result.returncode}"
        raise PerItemFailure(f"ffprobe falhou: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PerItemFailure(f"ffprobe retornou JSON invalido: {exc}") from exc
    if not isinstance(payload, dict):
        raise PerItemFailure("ffprobe retornou payload inesperado.")
    return payload


def select_best_streams(probe: dict[str, Any]) -> StreamSelection:
    streams = [stream for stream in probe.get("streams") or [] if isinstance(stream, dict)]
    programs = [program for program in probe.get("programs") or [] if isinstance(program, dict)]

    program_by_stream_index: dict[int, dict[str, Any]] = {}
    program_stream_indices: dict[int, set[int]] = {}
    for program in programs:
        program_id = parse_int(program.get("program_id")) or parse_int(program.get("id"))
        if program_id is None:
            program_id = len(program_stream_indices)
        indices: set[int] = set()
        for stream in program.get("streams") or []:
            if not isinstance(stream, dict):
                continue
            index = parse_int(stream.get("index"))
            if index is None:
                continue
            indices.add(index)
            program_by_stream_index[index] = program
        if indices:
            program_stream_indices[program_id] = indices

    video_streams = [
        stream
        for stream in streams
        if stream.get("codec_type") == "video" and parse_int(stream.get("index")) is not None
    ]
    if not video_streams:
        raise PerItemFailure("ffprobe nao encontrou stream de video.")

    def video_score(stream: dict[str, Any]) -> tuple[int, int, int]:
        index = parse_int(stream.get("index")) or -1
        program = program_by_stream_index.get(index)
        height = parse_int(stream.get("height")) or 0
        width = parse_int(stream.get("width")) or 0
        bitrate = stream_bitrate(stream, program) or 0
        return height, width, bitrate

    best_video = max(video_streams, key=video_score)
    best_video_index = parse_int(best_video.get("index"))
    if best_video_index is None:
        raise PerItemFailure("stream de video escolhido sem index.")

    selected_program_id: int | None = None
    same_program_indices: set[int] = set()
    for program_id, indices in program_stream_indices.items():
        if best_video_index in indices:
            selected_program_id = program_id
            same_program_indices = indices
            break

    audio_streams = [
        stream
        for stream in streams
        if stream.get("codec_type") == "audio" and parse_int(stream.get("index")) is not None
    ]
    same_program_audio = [
        stream for stream in audio_streams if parse_int(stream.get("index")) in same_program_indices
    ]
    audio_pool = same_program_audio or audio_streams

    best_audio_index: int | None = None
    if audio_pool:
        best_audio = max(
            audio_pool,
            key=lambda stream: stream_bitrate(
                stream, program_by_stream_index.get(parse_int(stream.get("index")) or -1)
            )
            or 0,
        )
        best_audio_index = parse_int(best_audio.get("index"))

    selected_program = program_by_stream_index.get(best_video_index)
    return StreamSelection(
        video_stream_index=best_video_index,
        audio_stream_index=best_audio_index,
        width=parse_int(best_video.get("width")),
        height=parse_int(best_video.get("height")),
        bitrate=stream_bitrate(best_video, selected_program),
        program_id=selected_program_id,
    )


def choose_stream_selection(candidate: Candidate, logger: Logger) -> StreamSelection:
    probe = run_ffprobe(candidate)
    selection = select_best_streams(probe)
    resolution = (
        f"{selection.width or 0}x{selection.height or 0}"
        if selection.width or selection.height
        else "-"
    )
    logger.log(
        "[download] qualidade escolhida "
        f"best_video_stream_index={selection.video_stream_index} "
        f"best_audio_stream_index={selection.audio_stream_index if selection.audio_stream_index is not None else '-'} "
        f"program_id={selection.program_id if selection.program_id is not None else '-'} "
        f"resolution={resolution} bitrate={selection.bitrate if selection.bitrate is not None else '-'}"
    )
    return selection


def run_ffmpeg(candidate: Candidate, selection: StreamSelection, logger: Logger) -> int:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-protocol_whitelist",
        "file,http,https,tcp,tls,crypto",
        "-i",
        candidate.video_stream_url,
        "-map",
        f"0:{selection.video_stream_index}",
    ]
    if selection.audio_stream_index is not None:
        command.extend(["-map", f"0:{selection.audio_stream_index}?"])
    command.extend(
        [
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(candidate.tmp_partial_path),
        ]
    )
    logger.log(f"[download] iniciado titulo={candidate.title} destino_tmp={candidate.tmp_partial_path}")
    result = subprocess.run(command, check=False)
    if result.returncode == 0:
        os.replace(candidate.tmp_partial_path, candidate.tmp_final_path)
    return result.returncode


def run_rclone_copyto(candidate: Candidate, logger: Logger) -> int:
    command = ["rclone", "copyto", str(candidate.tmp_final_path), candidate.remote_path]
    logger.log(f"[upload] iniciado titulo={candidate.title} remote_path={candidate.remote_path}")
    result = subprocess.run(command, check=False)
    return result.returncode


def build_manifest_item(candidate: Candidate) -> dict[str, Any]:
    return {
        "stable_key": candidate.stable_key,
        "disciplina": normalize_space(candidate.aula.get("disciplina")),
        "source_ref": candidate.source_ref,
        "title": candidate.title,
        "video_stream_url": candidate.video_stream_url,
        "remote_path": candidate.remote_path,
        "status": "uploaded",
        "uploaded_at": now_utc_iso(),
        "local_file_size_bytes": candidate.tmp_final_path.stat().st_size,
        "duration_seconds": candidate.aula.get("duracao_segundos"),
    }


def remote_target(rclone_remote: str, remote_base: str) -> str:
    clean_remote = rclone_remote.rstrip("/")
    separator = "" if clean_remote.endswith(":") else "/"
    return f"{clean_remote}{separator}{remote_base.strip('/')}"


def queue_remote_path_from_full(remote_path: str, rclone_remote: str) -> str:
    clean_remote = rclone_remote.rstrip("/")
    value = remote_path
    if value.startswith(clean_remote):
        value = value[len(clean_remote):]
    value = value.lstrip("/")
    if value.startswith("Federal Online/"):
        value = value[len("Federal Online/"):]
    return value


def full_remote_path_from_queue(queue_remote_path: str, rclone_remote: str) -> str:
    clean_remote = rclone_remote.rstrip("/")
    separator = "" if clean_remote.endswith(":") else "/"
    return f"{clean_remote}{separator}Federal Online/{queue_remote_path.lstrip('/')}"


def list_remote_files(rclone_remote: str, remote_base: str) -> dict[str, dict[str, Any]]:
    ensure_tool_exists("rclone")
    target = remote_target(rclone_remote, remote_base)
    result = subprocess.run(
        ["rclone", "lsjson", "--recursive", target],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = normalize_space(result.stderr) or f"exit_code={result.returncode}"
        raise RuntimeError(f"rclone lsjson falhou em {target}: {detail}")
    payload = json.loads(result.stdout or "[]")
    if not isinstance(payload, list):
        raise RuntimeError("rclone lsjson retornou payload inesperado.")
    files: dict[str, dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict) or item.get("IsDir"):
            continue
        rel_path = normalize_space(item.get("Path"))
        if not rel_path:
            continue
        full_path = remote_join(rclone_remote, remote_base, [], rel_path)
        files[full_path] = {
            "remote_path": full_path,
            "relative_path": rel_path,
            "size": item.get("Size"),
            "mod_time": item.get("ModTime"),
        }
    return files


def load_queue_rows(queue_db: Path, rclone_remote: str) -> dict[str, dict[str, Any]]:
    if not queue_db.exists():
        return {}
    conn = sqlite3.connect(queue_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, remote_path, video_name, video_size, video_mod_time, status,
                   needs_review, output_relative_path, aula_title, tipo, materia, frente, aula_number
            FROM transcription_queue
            """
        ).fetchall()
    finally:
        conn.close()
    by_full_path: dict[str, dict[str, Any]] = {}
    for row in rows:
        full_path = full_remote_path_from_queue(str(row["remote_path"] or ""), rclone_remote)
        by_full_path[full_path] = {key: row[key] for key in row.keys()}
    return by_full_path


def parse_aula_number(aula: dict[str, Any]) -> int | None:
    value = aula.get("ordem")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def subject_base_and_front(source_ref: str, disciplina: str) -> tuple[str, str]:
    parts = [normalize_space(part) for part in source_ref.split(">") if normalize_space(part)]
    frente = normalize_space(disciplina) or (parts[-1] if parts else "")
    materia = parts[-2] if len(parts) >= 2 else frente
    return materia, frente


def output_relative_path_for(candidate: Candidate, queue_remote_path: str) -> str:
    return f"Federal Online Transcrições/{Path(queue_remote_path).with_suffix('.transcricao.md')}"


def write_audit_reports(
    *,
    reports_dir: Path,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = reports_dir / f"fo_video_remote_audit_{timestamp}.json"
    tsv_path = reports_dir / f"fo_video_remote_audit_{timestamp}.tsv"
    payload = {
        "schema_version": 1,
        "generated_at": now_utc_iso(),
        "summary": summary,
        "items": rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with tsv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_REPORT_COLUMNS, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in AUDIT_REPORT_COLUMNS})
    return json_path, tsv_path


def audit_row(
    *,
    stable_key: str,
    portal_path: str,
    portal_title: str,
    video_url: str,
    previous_video_url: str,
    remote_path: str,
    queue_remote_path: str,
    remote_exists: bool,
    local_exists: bool,
    manifest_status: str,
    expected_status: str,
    detected_issue: str,
    action_dry_run: str,
    transcription_action_dry_run: str,
    risk_level: str,
    reason: str,
    remote_size: Any = "",
    manifest_file_size: Any = "",
    duration_seconds: Any = "",
    queue_status: str = "",
    queue_id: Any = "",
) -> dict[str, Any]:
    return {
        "stable_key": stable_key,
        "portal_path": portal_path,
        "portal_title": portal_title,
        "video_url": video_url,
        "previous_video_url": previous_video_url,
        "remote_path": remote_path,
        "queue_remote_path": queue_remote_path,
        "remote_exists": str(bool(remote_exists)).lower(),
        "local_exists": str(bool(local_exists)).lower(),
        "manifest_status": manifest_status,
        "expected_status": expected_status,
        "detected_issue": detected_issue,
        "action_dry_run": action_dry_run,
        "transcription_action_dry_run": transcription_action_dry_run,
        "risk_level": risk_level,
        "reason": reason,
        "remote_size": "" if remote_size is None else remote_size,
        "manifest_file_size": "" if manifest_file_size is None else manifest_file_size,
        "duration_seconds": "" if duration_seconds is None else duration_seconds,
        "queue_status": queue_status,
        "queue_id": "" if queue_id is None else queue_id,
    }


def transcription_action_for(
    *,
    action_dry_run: str,
    detected_issue: str,
    queue_row: dict[str, Any] | None,
) -> tuple[str, str]:
    if queue_row:
        status = str(queue_row.get("status") or "")
        needs_review = int(queue_row.get("needs_review") or 0)
        if status in {"pending", "running"}:
            return "no_action", "transcription queue already has pending/running item"
        if status == "done" and needs_review == 1:
            return "would_enqueue_transcription", "existing done transcription is marked stale/needs_review and should be reset to pending"

    if action_dry_run not in {"upload", "replace"} and detected_issue not in {
        "url_changed",
        "remote_size_differs",
        "duration_changed_same_url",
    }:
        return "no_action", ""
    if queue_row and str(queue_row.get("status")) == "done":
        return "would_enqueue_transcription", "existing done transcription should be marked stale and reset to pending"
    return "would_enqueue_transcription", "no active queue item found for new or replaced video"


def build_video_audit(args: argparse.Namespace, logger: Logger) -> tuple[list[dict[str, Any]], dict[str, Any], Path, Path]:
    ensure_tool_exists("rclone")
    aulas = load_index(args.index_json)
    eligible, skip_counts = eligible_aulas(aulas, args.path_filter)
    candidates = build_candidates(
        eligible,
        tmp_dir=args.tmp_dir,
        rclone_remote=args.rclone_remote,
        remote_base=args.remote_base,
    )
    manifest = load_manifest(args.manifest)
    latest_by_key, _ = manifest_maps(manifest)
    manifest_by_remote = {
        normalize_space(item.get("remote_path")): item
        for item in manifest.get("items", [])
        if isinstance(item, dict) and normalize_space(item.get("remote_path"))
    }
    remote_files = list_remote_files(args.rclone_remote, args.remote_base)
    queue_rows = load_queue_rows(args.queue_db, args.rclone_remote)

    rows: list[dict[str, Any]] = []
    expected_remote_paths: set[str] = set()
    expected_keys: set[str] = set()

    for candidate in candidates:
        expected_remote_paths.add(candidate.remote_path)
        expected_keys.add(candidate.stable_key)
        manifest_item = latest_by_key.get(candidate.stable_key)
        remote_file = remote_files.get(candidate.remote_path)
        queue_row = queue_rows.get(candidate.remote_path)
        previous_video_url = normalize_space((manifest_item or {}).get("video_stream_url"))
        manifest_status = normalize_space((manifest_item or {}).get("status"))
        manifest_size = (manifest_item or {}).get("local_file_size_bytes")
        manifest_duration = (manifest_item or {}).get("duration_seconds")
        current_duration = candidate.aula.get("duracao_segundos")
        remote_size = (remote_file or {}).get("size")
        remote_exists = remote_file is not None
        local_exists = candidate.tmp_final_path.exists()

        detected_issue = "none"
        action = "keep"
        risk = "low"
        reason = "expected video exists remotely and matches manifest URL"
        if manifest_item and previous_video_url and previous_video_url != candidate.video_stream_url:
            detected_issue = "url_changed"
            action = "replace" if remote_exists else "upload"
            risk = "high"
            reason = "portal video_stream_url differs from latest manifest video_stream_url"
        elif not remote_exists:
            detected_issue = "missing_remote"
            action = "upload"
            risk = "medium"
            reason = "expected video is absent from OneDrive remote listing"
        elif not manifest_item:
            detected_issue = "manifest_missing"
            action = "needs_review"
            risk = "medium"
            reason = "remote file exists but stable_key is absent from manifest"
        elif manifest_size not in (None, "") and remote_size not in (None, ""):
            try:
                if int(manifest_size) != int(remote_size):
                    detected_issue = "remote_size_differs"
                    action = "needs_review"
                    risk = "medium"
                    reason = "OneDrive size differs from manifest local_file_size_bytes"
            except (TypeError, ValueError):
                pass
        if (
            detected_issue == "none"
            and manifest_duration not in (None, "")
            and current_duration not in (None, "")
        ):
            try:
                if int(float(str(manifest_duration))) != int(float(str(current_duration))):
                    detected_issue = "duration_changed_same_url"
                    action = "needs_review"
                    risk = "medium"
                    reason = "duration_seconds in current index differs from manifest while URL is unchanged"
            except (TypeError, ValueError):
                pass

        transcription_action, transcription_reason = transcription_action_for(
            action_dry_run=action,
            detected_issue=detected_issue,
            queue_row=queue_row,
        )
        if transcription_reason:
            reason = f"{reason}; {transcription_reason}"
        queue_remote_path = queue_remote_path_from_full(candidate.remote_path, args.rclone_remote)
        rows.append(
            audit_row(
                stable_key=candidate.stable_key,
                portal_path=candidate.source_ref,
                portal_title=candidate.title,
                video_url=candidate.video_stream_url,
                previous_video_url=previous_video_url,
                remote_path=candidate.remote_path,
                queue_remote_path=queue_remote_path,
                remote_exists=remote_exists,
                local_exists=local_exists,
                manifest_status=manifest_status,
                expected_status="expected",
                detected_issue=detected_issue,
                action_dry_run=action,
                transcription_action_dry_run=transcription_action,
                risk_level=risk,
                reason=reason,
                remote_size=remote_size,
                manifest_file_size=manifest_size,
                duration_seconds=candidate.aula.get("duracao_segundos"),
                queue_status="" if not queue_row else str(queue_row.get("status") or ""),
                queue_id="" if not queue_row else queue_row.get("id"),
            )
        )

    for remote_path, remote_file in sorted(remote_files.items()):
        if remote_path in expected_remote_paths:
            continue
        manifest_item = manifest_by_remote.get(remote_path)
        stable_key = normalize_space((manifest_item or {}).get("stable_key"))
        queue_row = queue_rows.get(remote_path)
        rows.append(
            audit_row(
                stable_key=stable_key,
                portal_path=normalize_space((manifest_item or {}).get("source_ref")),
                portal_title=normalize_space((manifest_item or {}).get("title")) or Path(remote_path).stem,
                video_url="",
                previous_video_url=normalize_space((manifest_item or {}).get("video_stream_url")),
                remote_path=remote_path,
                queue_remote_path=queue_remote_path_from_full(remote_path, args.rclone_remote),
                remote_exists=True,
                local_exists=False,
                manifest_status=normalize_space((manifest_item or {}).get("status")),
                expected_status="not_in_index",
                detected_issue="remote_orphan_not_in_index",
                action_dry_run="would_delete_remote",
                transcription_action_dry_run="would_mark_transcription_stale" if queue_row else "no_action",
                risk_level="high",
                reason="remote file exists in OneDrive but current index has no matching expected remote_path",
                remote_size=remote_file.get("size"),
                manifest_file_size=(manifest_item or {}).get("local_file_size_bytes"),
                duration_seconds=(manifest_item or {}).get("duration_seconds"),
                queue_status="" if not queue_row else str(queue_row.get("status") or ""),
                queue_id="" if not queue_row else queue_row.get("id"),
            )
        )

    summary = {
        "total_index_rows": len(aulas),
        "total_expected": len(candidates),
        "total_remote_files": len(remote_files),
        "total_manifest_items": len(manifest.get("items", [])),
        "not_launched_skipped": skip_counts.get("not_launched_skipped", 0),
        "no_url_skipped": skip_counts.get("no_url_skipped", 0),
        "path_filtered_skipped": skip_counts.get("path_filtered_skipped", 0),
        "extras_remote": sum(1 for row in rows if row["detected_issue"] == "remote_orphan_not_in_index"),
        "missing_remote": sum(1 for row in rows if row["detected_issue"] == "missing_remote"),
        "url_changed": sum(1 for row in rows if row["detected_issue"] == "url_changed"),
        "remote_size_differs": sum(1 for row in rows if row["detected_issue"] == "remote_size_differs"),
        "duration_changed_same_url": sum(1 for row in rows if row["detected_issue"] == "duration_changed_same_url"),
        "would_delete_remote": sum(1 for row in rows if row["action_dry_run"] == "would_delete_remote"),
        "would_enqueue_transcription": sum(
            1 for row in rows if row["transcription_action_dry_run"] == "would_enqueue_transcription"
        ),
        "would_mark_transcription_stale": sum(
            1 for row in rows if row["transcription_action_dry_run"] == "would_mark_transcription_stale"
        ),
        "needs_review": sum(1 for row in rows if row["action_dry_run"] == "needs_review"),
    }
    json_path, tsv_path = write_audit_reports(reports_dir=args.reports_dir, rows=rows, summary=summary)
    logger.log("Auditoria remota concluida: " + " ".join(f"{key}={value}" for key, value in summary.items()))
    logger.log(f"Relatorio JSON: {json_path}")
    logger.log(f"Relatorio TSV: {tsv_path}")
    print(json.dumps({"summary": summary, "json_report": str(json_path), "tsv_report": str(tsv_path)}, ensure_ascii=False, indent=2))
    return rows, summary, json_path, tsv_path


def load_plan_items(plan_json: Path) -> list[dict[str, Any]]:
    if not plan_json or not plan_json.exists():
        raise RuntimeError("--plan-json e obrigatorio para qualquer apply.")
    payload = json.loads(plan_json.read_text(encoding="utf-8"))
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("plan-json invalido: campo items ausente.")
    return [item for item in items if isinstance(item, dict)]


def backup_file(path: Path, label: str) -> Path | None:
    if not path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.name}.{label}_{timestamp}.bak")
    shutil.copy2(path, backup_path)
    return backup_path


def plan_item_selected(item: dict[str, Any], args: argparse.Namespace) -> bool:
    selected_keys = {normalize_space(value) for value in args.apply_stable_key if normalize_space(value)}
    if selected_keys and normalize_space(item.get("stable_key")) not in selected_keys:
        return False
    return True


def apply_plan_actions(args: argparse.Namespace, logger: Logger) -> int:
    items = load_plan_items(args.plan_json)
    if args.apply_delete_remote:
        ensure_tool_exists("rclone")
        backup_path = backup_file(args.manifest, "pre_delete_remote")
        logger.log(f"backup_manifest={backup_path}")
        for item in items:
            if not plan_item_selected(item, args):
                continue
            if item.get("action_dry_run") != "would_delete_remote":
                continue
            remote_path = normalize_space(item.get("remote_path"))
            if not remote_path:
                continue
            logger.log(f"[apply-delete-remote] rclone deletefile {remote_path}")
            result = subprocess.run(["rclone", "deletefile", remote_path], check=False)
            if result.returncode != 0:
                raise RuntimeError(f"rclone deletefile falhou para {remote_path}: exit_code={result.returncode}")

    if args.apply_enqueue_transcriptions or args.apply_mark_stale:
        backup_path = backup_file(args.queue_db, "pre_transcription_apply")
        logger.log(f"backup_queue_db={backup_path}")
        conn = sqlite3.connect(args.queue_db)
        conn.row_factory = sqlite3.Row
        try:
            for item in items:
                if not plan_item_selected(item, args):
                    continue
                queue_remote_path = normalize_space(item.get("queue_remote_path"))
                if not queue_remote_path:
                    continue
                existing = conn.execute(
                    "SELECT id, status, needs_review FROM transcription_queue WHERE remote_path = ?",
                    (queue_remote_path,),
                ).fetchone()
                if args.apply_enqueue_transcriptions and item.get("transcription_action_dry_run") == "would_enqueue_transcription":
                    if existing and existing["status"] in {"pending", "running"}:
                        logger.log(f"[skip-enqueue] ja existe ativo remote_path={queue_remote_path} status={existing['status']}")
                        continue
                    video_name = Path(queue_remote_path).name
                    portal_path = normalize_space(item.get("portal_path"))
                    parts = [part.strip() for part in portal_path.split(">") if part.strip()]
                    tipo = parts[0] if parts else ""
                    materia = parts[1] if len(parts) > 1 else ""
                    frente = parts[-1] if parts else ""
                    output_relative_path = f"Federal Online Transcrições/{Path(queue_remote_path).with_suffix('.transcricao.md')}"
                    if existing:
                        conn.execute(
                            """
                            UPDATE transcription_queue
                            SET status = 'pending',
                                needs_review = 0,
                                review_reason = COALESCE(review_reason || '; ', '') || ?,
                                claimed_by = NULL,
                                claimed_at = NULL,
                                started_at = NULL,
                                finished_at = NULL,
                                error = NULL,
                                video_size = COALESCE(?, video_size),
                                video_name = COALESCE(NULLIF(?, ''), video_name),
                                aula_title = COALESCE(NULLIF(?, ''), aula_title),
                                tipo = COALESCE(NULLIF(?, ''), tipo),
                                materia = COALESCE(NULLIF(?, ''), materia),
                                frente = COALESCE(NULLIF(?, ''), frente),
                                output_relative_path = COALESCE(NULLIF(?, ''), output_relative_path),
                                notes = COALESCE(notes || '; ', '') || ?,
                                updated_at = ?
                            WHERE remote_path = ?
                            """,
                            (
                                f"requeued from video audit plan {args.plan_json}",
                                item.get("remote_size") or None,
                                video_name,
                                item.get("portal_title") or Path(video_name).stem,
                                tipo,
                                materia,
                                frente,
                                output_relative_path,
                                f"requeued existing id={existing['id']} from video audit",
                                now_utc_iso(),
                                queue_remote_path,
                            ),
                        )
                        logger.log(f"[requeue-existing] id={existing['id']} remote_path={queue_remote_path}")
                    else:
                        conn.execute(
                            """
                            INSERT INTO transcription_queue (
                                remote_path, video_name, video_size, video_mod_time, aula_title,
                                tipo, materia, frente, output_relative_path, status,
                                needs_review, review_reason, match_status, notes, created_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, 'video_audit', ?, ?, ?)
                            """,
                            (
                                queue_remote_path,
                                video_name,
                                item.get("remote_size") or None,
                                None,
                                item.get("portal_title") or Path(video_name).stem,
                                tipo,
                                materia,
                                frente,
                                output_relative_path,
                                f"enqueued from video audit plan {args.plan_json}",
                                now_utc_iso(),
                                now_utc_iso(),
                            ),
                        )
                        logger.log(f"[enqueue-new] remote_path={queue_remote_path}")
                if args.apply_mark_stale and (
                    item.get("transcription_action_dry_run") in {"would_mark_transcription_stale", "would_enqueue_transcription"}
                    or item.get("detected_issue") in {"url_changed", "remote_size_differs", "duration_changed_same_url"}
                ):
                    if not existing:
                        logger.log(f"[skip-stale] sem item existente remote_path={queue_remote_path}")
                        continue
                    if existing["status"] in {"pending", "running"}:
                        logger.log(f"[skip-stale] item ativo nao sera marcado stale remote_path={queue_remote_path} status={existing['status']}")
                        continue
                    conn.execute(
                        """
                        UPDATE transcription_queue
                        SET needs_review = 1,
                            review_reason = COALESCE(review_reason || '; ', '') || ?,
                            updated_at = ?
                        WHERE remote_path = ?
                        """,
                        (f"video audit marked stale from plan {args.plan_json}", now_utc_iso(), queue_remote_path),
                    )
                    logger.log(f"[mark-stale] remote_path={queue_remote_path} id={existing['id']}")
            conn.commit()
        finally:
            conn.close()
    return 0


def create_logger() -> Logger:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    latest_log = DEFAULT_LOGS_DIR / "fo_video_upload.latest.log"
    timestamped_log = DEFAULT_LOGS_DIR / f"fo_video_upload_{timestamp}.log"
    DEFAULT_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    latest_log.write_text("", encoding="utf-8")
    return Logger([latest_log, timestamped_log])


def main() -> int:
    args = parse_args()
    logger = create_logger()
    if args.apply_delete_remote or args.apply_enqueue_transcriptions or args.apply_mark_stale:
        return apply_plan_actions(args, logger)
    if args.audit_remote:
        build_video_audit(args, logger)
        return 0

    stats = {
        "total_lidas": 0,
        "elegiveis": 0,
        "skipped_uploaded_same_url": 0,
        "skipped_uploaded_existing": 0,
        "downloaded": 0,
        "uploaded": 0,
        "failed": 0,
        "not_launched_skipped": 0,
        "no_url_skipped": 0,
        "dry_run": bool(args.dry_run),
    }

    try:
        logger.log("Inicio do upload de videoaulas do Federal Online.")
        logger.log(f"index_json={args.index_json}")
        logger.log(f"manifest={args.manifest}")
        logger.log(f"tmp_dir={args.tmp_dir}")
        logger.log(f"rclone_remote={args.rclone_remote}")
        logger.log(f"remote_base={args.remote_base}")
        logger.log(
            f"flags dry_run={args.dry_run} force={args.force} only_missing={args.only_missing} "
            f"limit={args.limit} path_filter={args.path_filter or []}"
        )

        if not args.dry_run:
            ensure_tool_exists("ffmpeg")
            ensure_tool_exists("rclone")

        aulas = load_index(args.index_json)
        stats["total_lidas"] = len(aulas)
        eligible, skip_counts = eligible_aulas(aulas, args.path_filter)
        stats.update(skip_counts)
        stats["elegiveis"] = len(eligible)
        logger.log(f"total_aulas_lidas={len(aulas)} elegiveis_com_video_stream_url={len(eligible)}")

        manifest = load_manifest(args.manifest)
        latest_by_key, uploaded_by_key = manifest_maps(manifest)
        candidates = build_candidates(
            eligible,
            tmp_dir=args.tmp_dir,
            rclone_remote=args.rclone_remote,
            remote_base=args.remote_base,
        )

        processed = 0
        for candidate in candidates:
            skip, reason = should_skip_candidate(
                candidate,
                latest_by_key=latest_by_key,
                uploaded_by_key=uploaded_by_key,
                force=args.force,
                only_missing=args.only_missing,
            )
            if skip:
                if reason == "uploaded_same_url":
                    stats["skipped_uploaded_same_url"] += 1
                elif reason == "uploaded_existing":
                    stats["skipped_uploaded_existing"] += 1
                logger.log(f"[skip] {reason} stable_key={candidate.stable_key}")
                continue

            if args.limit is not None and processed >= args.limit:
                logger.log(f"[limit] limite atingido limit={args.limit}")
                break
            processed += 1

            logger.log(
                f"[item] stable_key={candidate.stable_key} remote_path={candidate.remote_path}"
            )

            if args.dry_run:
                logger.log(
                    f"[dry-run] baixaria={candidate.video_stream_url} subiria={candidate.remote_path}"
                )
                continue

            try:
                check_free_space(args.tmp_dir, args.min_free_gb)
                cleanup_tmp_file(candidate.tmp_partial_path, args.tmp_dir, logger)
                cleanup_tmp_file(candidate.tmp_final_path, args.tmp_dir, logger)

                selection = choose_stream_selection(candidate, logger)
                ffmpeg_code = run_ffmpeg(candidate, selection, logger)
                if ffmpeg_code != 0:
                    stats["failed"] += 1
                    logger.log(f"[erro] ffmpeg falhou exit_code={ffmpeg_code} stable_key={candidate.stable_key}")
                    cleanup_tmp_file(candidate.tmp_partial_path, args.tmp_dir, logger)
                    cleanup_tmp_file(candidate.tmp_final_path, args.tmp_dir, logger)
                    continue
                stats["downloaded"] += 1

                rclone_code = run_rclone_copyto(candidate, logger)
                if rclone_code != 0:
                    stats["failed"] += 1
                    logger.log(f"[erro] rclone falhou exit_code={rclone_code} stable_key={candidate.stable_key}")
                    cleanup_tmp_file(candidate.tmp_partial_path, args.tmp_dir, logger)
                    cleanup_tmp_file(candidate.tmp_final_path, args.tmp_dir, logger)
                    continue

                item = build_manifest_item(candidate)
                upsert_manifest_item(manifest, item)
                write_manifest_atomic(args.manifest, manifest)
                latest_by_key, uploaded_by_key = manifest_maps(manifest)
                stats["uploaded"] += 1
                logger.log(f"[ok] upload concluido stable_key={candidate.stable_key} remote_path={candidate.remote_path}")
                cleanup_tmp_file(candidate.tmp_final_path, args.tmp_dir, logger)
            except PerItemFailure as exc:
                stats["failed"] += 1
                logger.log(f"[erro] aula falhou stable_key={candidate.stable_key} detalhe={summarize_exception(exc)}")
                cleanup_tmp_file(candidate.tmp_partial_path, args.tmp_dir, logger)
                cleanup_tmp_file(candidate.tmp_final_path, args.tmp_dir, logger)
            except RuntimeError:
                raise
            except Exception as exc:
                stats["failed"] += 1
                logger.log(f"[erro] aula falhou stable_key={candidate.stable_key} detalhe={summarize_exception(exc)}")
                cleanup_tmp_file(candidate.tmp_partial_path, args.tmp_dir, logger)
                cleanup_tmp_file(candidate.tmp_final_path, args.tmp_dir, logger)

        logger.log(
            "Resumo final: "
            + " ".join(f"{key}={value}" for key, value in stats.items())
        )
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        logger.log(f"Falha estrutural: {summarize_exception(exc)}")
        logger.log(
            "Resumo final: "
            + " ".join(f"{key}={value}" for key, value in stats.items())
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
