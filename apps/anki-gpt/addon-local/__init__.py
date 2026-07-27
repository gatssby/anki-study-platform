from pathlib import Path
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from html.parser import HTMLParser
from aqt import gui_hooks, mw
from aqt.qt import (
    QAction,
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    Qt,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import askUser, showInfo
import hashlib
import json
import os
import re
import html
import shlex
import subprocess
import threading
import time
import unicodedata

from .runtime_paths import append_log_resilient, initialize_runtime_paths


RUNTIME_PATHS, RUNTIME_MIGRATION = initialize_runtime_paths()
LOG_FILE = RUNTIME_PATHS.log_file
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
LOG_LOCK = threading.Lock()
STATE_DIR = RUNTIME_PATHS.state
MEDIA_PUBLISH_STATUS_FILE = STATE_DIR / "media_publish_status.json"
AUTO_PUBLISH_PAUSE_FILE = STATE_DIR / "pause_auto_publish"
AUTO_PUBLISH_POLICY_FILE = STATE_DIR / "auto_publish_policy.json"
AUTO_PUBLISH_MODES = {"disabled", "manual", "after_anki_sync", "always"}
DEFAULT_AUTO_PUBLISH_MODE = "manual"
REPO_ROOT = Path(__file__).resolve().parent.parent
MEDIA_PUBLISH_SCRIPT = REPO_ROOT / "local-tools" / "anki_publish.sh"
MEDIA_PUBLISH_TIMEOUT_SECONDS = 1800
COMBINED_SYNC_PROGRESS_MAX = 5
SYNC_URL = "https://gatsby-anki.137.131.191.66.nip.io/sync/full"
TAGGING_API_BASE = "https://gatsby-anki.137.131.191.66.nip.io"
TAGGING_TOKEN_ENV = "ANKI_GPT_TAGGING_TOKEN"
TAGGING_TOKEN_FILE = RUNTIME_PATHS.token_file
TAGGING_MAX_OPERATIONS_PER_RUN = 10
TAGGING_TIMEOUT_SECONDS = 20
ALLOWED_ROOTS = {"Federal Online 2026", "#UFPR", "2025"}
ALLOWED_TAGGING_TAGS = {
    "prio:alta",
    "prio:media",
    "prio:baixa",
    "avaliar",
    "precisa_melhorar",
    "overkill",
    "duplicado",
    "bom_card",
}

TAG_RE = re.compile(r"<[^>]+>")
CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::(.*?))?\}\}", re.IGNORECASE | re.DOTALL)
IMAGE_OCCLUSION_RE = re.compile(r"image-occlusion", re.IGNORECASE)
MENU_REGISTERED = False
COMBINED_SYNC_IN_FLIGHT = False
MEDIA_PUBLISH_IN_FLIGHT = False
MEDIA_PUBLISH_REQUESTED_AGAIN = False
MEDIA_PUBLISH_PENDING_REQUEST = None
MEDIA_PUBLISH_LOCK = threading.Lock()
LAST_POSTED_SNAPSHOT_HASH = ""
LAST_CONFIRMED_GENERATION_ID = ""
OPERATIONS_DIALOG = None
ADDON_VERSION = "3.0.0"
ADDON_LOADED_AT = datetime.now().astimezone().isoformat()
ADDON_MAIN_THREAD_ID = threading.get_ident()
ADDON_RUNTIME_DIAGNOSTICS_FILE = STATE_DIR / "addon_runtime.json"


def log(msg: str) -> None:
    append_log_resilient(
        LOG_FILE,
        msg,
        max_bytes=LOG_MAX_BYTES,
        backup_count=LOG_BACKUP_COUNT,
        thread_lock=LOG_LOCK,
    )


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def duration_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_auto_publish_policy() -> dict:
    configured = AUTO_PUBLISH_POLICY_FILE.exists()
    payload = read_json_file(AUTO_PUBLISH_POLICY_FILE) if configured else {}
    mode = payload.get("mode", DEFAULT_AUTO_PUBLISH_MODE)
    if mode not in AUTO_PUBLISH_MODES:
        log(f"auto publish policy invalid mode={mode!r}; using={DEFAULT_AUTO_PUBLISH_MODE}")
        mode = DEFAULT_AUTO_PUBLISH_MODE
        configured = False
    return {"mode": mode, "configured": configured}


def save_auto_publish_policy(mode: str) -> dict:
    if mode not in AUTO_PUBLISH_MODES:
        raise ValueError("invalid_auto_publish_mode")
    payload = {
        "version": 1,
        "mode": mode,
        "updated_at": now_iso(),
    }
    atomic_write_json(AUTO_PUBLISH_POLICY_FILE, payload)
    AUTO_PUBLISH_POLICY_FILE.chmod(0o600)
    log(f"auto publish policy changed mode={mode}")
    writer = globals().get("write_addon_runtime_diagnostics")
    if callable(writer):
        writer("auto_publish_policy_changed")
    return {"mode": mode, "configured": True}


def publication_decision(trigger: str) -> dict:
    policy = load_auto_publish_policy()
    mode = policy["mode"]
    if AUTO_PUBLISH_PAUSE_FILE.exists():
        decision = {**policy, "allowed": False, "reason": "pause_auto_publish"}
    elif mode == "disabled":
        decision = {**policy, "allowed": False, "reason": "policy_disabled"}
    elif trigger == "manual":
        decision = {**policy, "allowed": True, "reason": "manual_request"}
    elif trigger == "anki_sync_did_finish" and mode in {"after_anki_sync", "always"}:
        decision = {**policy, "allowed": True, "reason": mode}
    elif trigger == "initialization" and mode == "always" and policy["configured"]:
        decision = {**policy, "allowed": True, "reason": "explicit_always"}
    else:
        decision = {**policy, "allowed": False, "reason": "policy_not_enabled_for_trigger"}

    writer = globals().get("write_addon_runtime_diagnostics")
    if callable(writer):
        writer(f"auto_publish_decision_{trigger}")
    return decision


def auto_publish_runtime_state() -> dict:
    policy = load_auto_publish_policy()
    return {
        "auto_publish_mode": policy["mode"],
        "auto_publish_configured": policy["configured"],
        "auto_publish_paused": AUTO_PUBLISH_PAUSE_FILE.exists(),
    }


def set_auto_publish_pause(paused: bool, phase: str) -> bool:
    try:
        AUTO_PUBLISH_PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if paused:
            AUTO_PUBLISH_PAUSE_FILE.touch()
            AUTO_PUBLISH_PAUSE_FILE.chmod(0o600)
        else:
            AUTO_PUBLISH_PAUSE_FILE.unlink(missing_ok=True)
    except Exception as e:
        log(f"auto publish pause change failed paused={paused} {type(e).__name__}: {e}")
        return False
    write_addon_runtime_diagnostics(phase)
    return True


def file_sha256(path_value) -> str:
    try:
        return hashlib.sha256(Path(path_value).read_bytes()).hexdigest()
    except Exception:
        return ""


def write_addon_runtime_diagnostics(phase: str) -> None:
    try:
        module_path = Path(__file__).resolve()
        organization_path = (
            Path(organization_module.__file__).resolve()
            if organization_module is not None and getattr(organization_module, "__file__", None)
            else None
        )
        publish_state = auto_publish_runtime_state()
        payload = {
            "addon_version": ADDON_VERSION,
            "module_file": str(module_path),
            "module_hash": file_sha256(module_path),
            "organization_module_file": str(organization_path) if organization_path else "",
            "organization_module_hash": file_sha256(organization_path) if organization_path else "",
            "loaded_at": ADDON_LOADED_AT,
            "phase": phase,
            "menu_registered": MENU_REGISTERED,
            "organization_import_ok": organization_module is not None,
            "auto_publish_mode": publish_state["auto_publish_mode"],
            "auto_publish_configured": publish_state["auto_publish_configured"],
            "auto_publish_paused": publish_state["auto_publish_paused"],
            # Backwards-compatible field names used by earlier audits.
            "auto_publish_policy": publish_state["auto_publish_mode"],
            "auto_publish_policy_configured": publish_state["auto_publish_configured"],
            "last_confirmed_generation_id": LAST_CONFIRMED_GENERATION_ID,
        }
        atomic_write_json(ADDON_RUNTIME_DIAGNOSTICS_FILE, payload)
        ADDON_RUNTIME_DIAGNOSTICS_FILE.chmod(0o600)
    except Exception as e:
        try:
            log(f"addon runtime diagnostics write failed phase={phase} {type(e).__name__}: {e}")
        except Exception:
            pass


def write_media_publish_status(**updates) -> None:
    status = {
        "status": "idle",
        "reason": "",
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "last_step": "",
        "last_error": "",
        "stdout_tail": "",
        "stderr_tail": "",
        "notes_with_broken_media": None,
        "total_broken_refs": None,
        "media_changed": None,
        "snapshot_changed": None,
        "publish_requested_again": False,
    }
    status.update(read_json_file(MEDIA_PUBLISH_STATUS_FILE))
    status.update(updates)
    atomic_write_json(MEDIA_PUBLISH_STATUS_FILE, status)


def parse_bool_value(value):
    if value is None:
        return None
    return str(value).strip().lower() in {"1", "true", "yes", "sim"}


def parse_publish_stdout(stdout: str) -> dict:
    parsed = {}
    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in {
            "notes_with_broken_media",
            "total_broken_refs",
            "changed_files",
            "uploaded_files",
            "missing",
        }:
            try:
                parsed[key] = int(value)
            except ValueError:
                parsed[key] = None
        elif key in {"media_changed", "snapshot_changed"}:
            parsed[key] = parse_bool_value(value)
    return parsed


def snapshot_content_hash(payload: dict) -> str:
    stable = dict(payload)
    stable.pop("timestamp", None)
    stable.pop("generated_at", None)
    deck_summary = stable.get("deck_summary")
    if isinstance(deck_summary, dict):
        deck_summary = dict(deck_summary)
        deck_summary.pop("generated_at", None)
        stable["deck_summary"] = deck_summary
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


try:
    from . import organization as organization_module
    ORGANIZATION_IMPORT_ERROR = ""
except Exception as e:
    organization_module = None
    ORGANIZATION_IMPORT_ERROR = f"{type(e).__name__}: {e}"


log(f"module importou runtime_migration={RUNTIME_MIGRATION}")


def command_text(args: list[str]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in args)


def tail_text(text: str, limit: int = 4000) -> str:
    if not isinstance(text, str):
        return ""
    if len(text) <= limit:
        return text
    return text[-limit:]


def run_on_main_thread(callback) -> None:
    taskman = getattr(mw, "taskman", None)
    run_on_main = getattr(taskman, "run_on_main", None) if taskman is not None else None
    if callable(run_on_main):
        run_on_main(callback)
    else:
        callback()


def progress_start(label: str, max_value=None) -> bool:
    try:
        if max_value is None:
            mw.progress.start(label=label, immediate=True)
        else:
            mw.progress.start(label=label, immediate=True, max=max_value)
        return True
    except TypeError:
        try:
            mw.progress.start(label=label, immediate=True)
            return True
        except Exception as e:
            log(f"progress start failed label={label} {type(e).__name__}: {e}")
    except Exception as e:
        log(f"progress start failed label={label} {type(e).__name__}: {e}")
    return False


def progress_update(label: str, value=None, max_value=None) -> None:
    try:
        kwargs = {"label": label}
        if value is not None:
            kwargs["value"] = value
        if max_value is not None:
            kwargs["max"] = max_value
        mw.progress.update(**kwargs)
    except TypeError:
        try:
            mw.progress.update(label=label)
        except Exception as e:
            log(f"progress update failed label={label} {type(e).__name__}: {e}")
    except Exception as e:
        log(f"progress update failed label={label} {type(e).__name__}: {e}")


def progress_finish() -> None:
    try:
        mw.progress.finish()
    except Exception as e:
        log(f"progress finish failed {type(e).__name__}: {e}")


def media_publish_in_flight_result(reason: str, dry_run: bool) -> dict:
    return {
        "ok": False,
        "status": "skipped_in_flight",
        "reason": reason,
        "dry_run": dry_run,
        "command": "",
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "error": "media_publish_in_flight",
    }


def acquire_media_publish(reason: str, step_label: str, dry_run: bool = False):
    global MEDIA_PUBLISH_IN_FLIGHT

    with MEDIA_PUBLISH_LOCK:
        if MEDIA_PUBLISH_IN_FLIGHT:
            log(f"media publish skipped reason={reason} step={step_label} error=media_publish_in_flight")
            return media_publish_in_flight_result(reason, dry_run)

        MEDIA_PUBLISH_IN_FLIGHT = True
        return None


def release_media_publish() -> None:
    global MEDIA_PUBLISH_IN_FLIGHT

    with MEDIA_PUBLISH_LOCK:
        MEDIA_PUBLISH_IN_FLIGHT = False


def run_media_publish_script_with_guard(reason: str, step_label: str, dry_run: bool = False, force: bool = False) -> dict:
    in_flight_result = acquire_media_publish(reason, step_label, dry_run=dry_run)
    if in_flight_result is not None:
        return in_flight_result

    try:
        return run_media_publish_script(reason=reason, dry_run=dry_run, force=force)
    finally:
        release_media_publish()


def run_media_publish_script(reason: str, dry_run: bool = False, force: bool = False) -> dict:
    cmd = ["bash", str(MEDIA_PUBLISH_SCRIPT), "--no-delete"]
    if dry_run:
        cmd.append("--dry-run")
    if force:
        cmd.append("--force")

    display = command_text(cmd)
    if AUTO_PUBLISH_PAUSE_FILE.exists():
        error = "auto_publish_paused"
        log(f"media publish skipped reason={reason} error={error}")
        writer = globals().get("write_addon_runtime_diagnostics")
        if callable(writer):
            writer("media_publish_paused")
        return {
            "ok": False,
            "status": "skipped_paused",
            "reason": reason,
            "dry_run": dry_run,
            "command": display,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "error": error,
        }
    if not MEDIA_PUBLISH_SCRIPT.exists():
        error = f"media_publish_script_not_found: {MEDIA_PUBLISH_SCRIPT}"
        log(f"media publish skipped reason={reason} error={error}")
        return {
            "ok": False,
            "reason": reason,
            "dry_run": dry_run,
            "command": display,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "error": error,
        }

    env = os.environ.copy()
    if LAST_POSTED_SNAPSHOT_HASH:
        env["ANKI_GPT_SNAPSHOT_HASH"] = LAST_POSTED_SNAPSHOT_HASH
    env["ANKI_GPT_PUBLISH_REASON"] = reason

    log(f"media publish start reason={reason} dry_run={dry_run} command={display}")
    started = time.perf_counter()
    write_media_publish_status(
        status="running",
        reason=reason,
        started_at=now_iso(),
        finished_at=None,
        exit_code=None,
        last_step="publish_start",
        last_error="",
        stdout_tail="",
        stderr_tail="",
        media_changed=None,
        snapshot_changed=None,
        publish_requested_again=False,
    )
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=MEDIA_PUBLISH_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        stdout_bytes = len((e.stdout or "").encode("utf-8", errors="replace")) if isinstance(e.stdout, str) else len(e.stdout or b"")
        stderr_bytes = len((e.stderr or "").encode("utf-8", errors="replace")) if isinstance(e.stderr, str) else len(e.stderr or b"")
        error = f"timeout_after_{MEDIA_PUBLISH_TIMEOUT_SECONDS}s"
        log(
            "media publish timeout "
            f"reason={reason} command={display} error={error} "
            f"stdout_bytes={stdout_bytes} stderr_bytes={stderr_bytes}"
        )
        write_media_publish_status(
            status="failed",
            reason=reason,
            finished_at=now_iso(),
            exit_code=None,
            last_step="publish_timeout",
            last_error=error,
            stdout_tail="",
            stderr_tail="",
        )
        return {
            "ok": False,
            "reason": reason,
            "dry_run": dry_run,
            "command": display,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "error": error,
        }
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log(f"media publish exception reason={reason} command={display} error={error}")
        write_media_publish_status(
            status="failed",
            reason=reason,
            finished_at=now_iso(),
            exit_code=None,
            last_step="publish_exception",
            last_error=error,
            stdout_tail="",
            stderr_tail="",
        )
        return {
            "ok": False,
            "reason": reason,
            "dry_run": dry_run,
            "command": display,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "error": error,
        }

    stdout_bytes = len((completed.stdout or "").encode("utf-8", errors="replace"))
    stderr_bytes = len((completed.stderr or "").encode("utf-8", errors="replace"))
    ok = completed.returncode == 0
    parsed = parse_publish_stdout(completed.stdout or "")
    log(
        "media publish finished "
        f"reason={reason} exit={completed.returncode} ok={ok} "
        f"duration_ms={duration_ms(started)} step=publish_total "
        f"media_changed={parsed.get('media_changed')} snapshot_changed={parsed.get('snapshot_changed')} "
        f"command={display} stdout_bytes={stdout_bytes} stderr_bytes={stderr_bytes}"
    )
    write_media_publish_status(
        status="success" if ok else "failed",
        reason=reason,
        finished_at=now_iso(),
        exit_code=completed.returncode,
        last_step="publish_finished",
        last_error="" if ok else f"exit={completed.returncode}",
        stdout_tail="",
        stderr_tail="",
        notes_with_broken_media=parsed.get("notes_with_broken_media"),
        total_broken_refs=parsed.get("total_broken_refs"),
        media_changed=parsed.get("media_changed"),
        snapshot_changed=parsed.get("snapshot_changed"),
    )
    return {
        "ok": ok,
        "status": "success" if ok else "failed",
        "reason": reason,
        "dry_run": dry_run,
        "command": display,
        "returncode": completed.returncode,
        "stdout": "",
        "stderr": "",
        "error": "" if ok else f"exit={completed.returncode}",
        "parsed": parsed,
    }


def run_background_task_with_progress(label: str, worker, on_done, show_progress: bool = True) -> None:
    taskman = getattr(mw, "taskman", None)
    run_in_background = getattr(taskman, "run_in_background", None) if taskman is not None else None
    run_on_main = getattr(taskman, "run_on_main", None) if taskman is not None else None
    progress_started = False

    if show_progress:
        progress_started = progress_start(label)

    def finish_progress() -> None:
        if not progress_started:
            return
        progress_finish()

    if not callable(run_in_background):
        try:
            result = worker()
        except Exception as e:
            finish_progress()
            on_done(None, e)
            return

        finish_progress()
        on_done(result, None)
        return

    def done(future) -> None:
        def handle_main() -> None:
            finish_progress()
            try:
                on_done(future.result(), None)
            except Exception as e:
                on_done(None, e)

        if callable(run_on_main):
            run_on_main(handle_main)
        else:
            handle_main()

    run_in_background(worker, done)


def schedule_media_publish_background(
    reason: str,
    step_label: str,
    on_done=None,
    dry_run: bool = False,
    show_progress: bool = False,
    force: bool = False,
) -> dict:
    global MEDIA_PUBLISH_IN_FLIGHT, MEDIA_PUBLISH_REQUESTED_AGAIN, MEDIA_PUBLISH_PENDING_REQUEST

    request = {
        "reason": reason,
        "step_label": step_label,
        "dry_run": dry_run,
        "force": force,
    }

    with MEDIA_PUBLISH_LOCK:
        if MEDIA_PUBLISH_IN_FLIGHT:
            MEDIA_PUBLISH_REQUESTED_AGAIN = True
            MEDIA_PUBLISH_PENDING_REQUEST = request
            log(f"media publish coalesced reason={reason} step={step_label} error=media_publish_in_flight")
            write_media_publish_status(
                status="running",
                reason=reason,
                last_step="coalesced_publish_request",
                last_error="media_publish_in_flight",
                publish_requested_again=True,
            )
            result = media_publish_in_flight_result(reason, dry_run)
            if callable(on_done):
                run_on_main_thread(lambda: on_done(result, None))
            return result

        MEDIA_PUBLISH_IN_FLIGHT = True

    write_media_publish_status(
        status="queued",
        reason=reason,
        started_at=None,
        finished_at=None,
        exit_code=None,
        last_step="queued",
        last_error="",
        stdout_tail="",
        stderr_tail="",
        publish_requested_again=False,
    )

    def worker_loop():
        global MEDIA_PUBLISH_IN_FLIGHT, MEDIA_PUBLISH_REQUESTED_AGAIN, MEDIA_PUBLISH_PENDING_REQUEST

        current = request
        last_result = None
        while current is not None:
            last_result = run_media_publish_script(
                reason=current["reason"],
                dry_run=current["dry_run"],
                force=current["force"],
            )

            with MEDIA_PUBLISH_LOCK:
                if MEDIA_PUBLISH_REQUESTED_AGAIN:
                    current = MEDIA_PUBLISH_PENDING_REQUEST or {
                        "reason": "coalesced_media_publish",
                        "step_label": "Publicando midia solicitada durante execucao anterior",
                        "dry_run": dry_run,
                        "force": force,
                    }
                    MEDIA_PUBLISH_REQUESTED_AGAIN = False
                    MEDIA_PUBLISH_PENDING_REQUEST = None
                    log(
                        "media publish follow-up starting "
                        f"reason={current['reason']} step={current['step_label']}"
                    )
                    write_media_publish_status(
                        status="queued",
                        reason=current["reason"],
                        started_at=None,
                        finished_at=None,
                        last_step="queued_followup",
                        last_error="",
                        publish_requested_again=False,
                    )
                    continue

                MEDIA_PUBLISH_IN_FLIGHT = False
                current = None

        return last_result

    progress_started = progress_start(step_label) if show_progress else False

    def finish_callback(result=None, error=None) -> None:
        if progress_started:
            progress_finish()
        if callable(on_done):
            on_done(result, error)

    def thread_main() -> None:
        global MEDIA_PUBLISH_IN_FLIGHT, MEDIA_PUBLISH_REQUESTED_AGAIN, MEDIA_PUBLISH_PENDING_REQUEST

        result = None
        error = None
        try:
            result = worker_loop()
        except Exception as e:
            error = e
            write_media_publish_status(
                status="failed",
                reason=reason,
                finished_at=now_iso(),
                last_step="publish_thread_exception",
                last_error=f"{type(e).__name__}: {e}",
            )
            with MEDIA_PUBLISH_LOCK:
                MEDIA_PUBLISH_IN_FLIGHT = False
                MEDIA_PUBLISH_REQUESTED_AGAIN = False
                MEDIA_PUBLISH_PENDING_REQUEST = None

        run_on_main_thread(lambda: finish_callback(result, error))

    thread = threading.Thread(target=thread_main, name="anki-gpt-media-publish", daemon=True)
    thread.start()

    return {
        "ok": True,
        "status": "queued",
        "reason": reason,
        "dry_run": dry_run,
        "command": command_text(["bash", str(MEDIA_PUBLISH_SCRIPT), "--no-delete"] + (["--dry-run"] if dry_run else []) + (["--force"] if force else [])),
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "error": "",
    }


def start_media_publish_step(reason: str, step_label: str, on_done, dry_run: bool = False, show_progress: bool = True) -> None:
    schedule_media_publish_background(
        reason=reason,
        step_label=step_label,
        on_done=on_done,
        dry_run=dry_run,
        show_progress=show_progress,
    )


def root_deck_name(deck_name: str) -> str:
    return deck_name.split("::", 1)[0]


def is_filtered_deck(deck_name: str) -> bool:
    return deck_name.startswith("Filtered Deck")


def is_default_deck(deck_name: str) -> bool:
    return deck_name == "Default"


def include_in_study_system(deck_name: str) -> bool:
    root = root_deck_name(deck_name)
    if is_filtered_deck(deck_name):
        return False
    if is_default_deck(deck_name):
        return False
    return root in ALLOWED_ROOTS


def get_deck_name_map():
    return {int(item.id): item.name for item in mw.col.decks.all_names_and_ids()}


def deck_sort_key(deck: dict):
    return (deck.get("name", ""), deck.get("id", 0))


def get_deck_count_map(count_notes: bool = False) -> dict[int, int]:
    expr = "count(distinct nid)" if count_notes else "count(*)"
    rows = mw.col.db.all(f"select did, {expr} from cards group by did")
    counts = {}
    for deck_id, count in rows:
        try:
            counts[int(deck_id)] = int(count)
        except Exception:
            continue
    return counts


def get_collection_card_count() -> int:
    return int(mw.col.db.scalar("select count(*) from cards") or 0)


def get_collection_note_count() -> int:
    return int(mw.col.db.scalar("select count(*) from notes") or 0)


def build_deck_snapshot() -> dict:
    deck_name_map = get_deck_name_map()
    name_to_id = {name: deck_id for deck_id, name in deck_name_map.items()}
    card_counts = get_deck_count_map(count_notes=False)
    note_counts = get_deck_count_map(count_notes=True)

    children_by_parent_id = {deck_id: [] for deck_id in deck_name_map}
    deck_rows = []
    for deck_id, deck_name in deck_name_map.items():
        parts = deck_name.split("::") if deck_name else []
        parent_name = "::".join(parts[:-1]) if len(parts) > 1 else ""
        parent_id = name_to_id.get(parent_name)
        if parent_id in children_by_parent_id:
            children_by_parent_id[parent_id].append(deck_id)

        deck_rows.append({
            "deck_id": deck_id,
            "id": deck_id,
            "deck_name": deck_name,
            "name": deck_name,
            "root_deck": root_deck_name(deck_name) if deck_name else "",
            "parent_deck_id": parent_id,
            "parent_deck_name": parent_name,
            "level": max(len(parts) - 1, 0),
            "path": parts,
            "included_in_study_system": include_in_study_system(deck_name),
            "card_count": card_counts.get(deck_id, 0),
            "note_count": note_counts.get(deck_id, 0),
        })

    for deck in deck_rows:
        deck_id = deck["deck_id"]
        child_ids = sorted(children_by_parent_id.get(deck_id, []), key=lambda did: deck_name_map.get(did, ""))
        descendant_ids = [
            other_id
            for other_id, other_name in deck_name_map.items()
            if other_id != deck_id and other_name.startswith(deck["deck_name"] + "::")
        ]
        descendant_ids.sort(key=lambda did: deck_name_map.get(did, ""))

        subtree_ids = [deck_id] + descendant_ids
        deck["child_deck_ids"] = child_ids
        deck["child_deck_names"] = [deck_name_map[did] for did in child_ids]
        deck["descendant_deck_ids"] = descendant_ids
        deck["descendant_deck_names"] = [deck_name_map[did] for did in descendant_ids]
        deck["subtree_card_count"] = sum(card_counts.get(did, 0) for did in subtree_ids)
        deck["subtree_note_count"] = sum(note_counts.get(did, 0) for did in subtree_ids)
        deck["has_cards"] = deck["card_count"] > 0
        deck["has_cards_in_subtree"] = deck["subtree_card_count"] > 0

    roots = sorted(
        [deck for deck in deck_rows if deck.get("parent_deck_id") is None],
        key=deck_sort_key,
    )

    return {
        "decks": sorted(deck_rows, key=deck_sort_key),
        "roots": roots,
        "deck_count": len(deck_rows),
        "total_cards": get_collection_card_count(),
        "total_notes": get_collection_note_count(),
    }


def get_model_name(mid: int) -> str:
    model = mw.col.models.get(mid)
    return model["name"] if model else ""


def get_template_name(note, ord_value):
    try:
        model = mw.col.models.get(note.mid)
        templates = model.get("tmpls", []) if model else []
        if isinstance(ord_value, int) and 0 <= ord_value < len(templates):
            name = templates[ord_value].get("name")
            return str(name) if name is not None else None
    except Exception as e:
        log(f"Exception get_template_name note_id={getattr(note, 'id', '')}: {type(e).__name__}: {e}")
    return None


def serialize_card_row(row, note, deck_name_map: dict) -> dict:
    (
        card_id,
        note_id,
        deck_id,
        ord_value,
        mod,
        usn,
        card_type,
        queue,
        due,
        ivl,
        factor,
        reps,
        lapses,
        left,
        odue,
        odid,
        flags,
        data,
    ) = row

    return {
        "card_id": int(card_id),
        "note_id": int(note_id),
        "ord": int(ord_value),
        "deck_id": int(deck_id),
        "did": int(deck_id),
        "deck_name": deck_name_map.get(deck_id, ""),
        "queue": int(queue),
        "type": int(card_type),
        "due": int(due),
        "ivl": int(ivl),
        "factor": int(factor),
        "reps": int(reps),
        "lapses": int(lapses),
        "left": int(left),
        "odue": int(odue),
        "odid": int(odid),
        "original_deck_name": deck_name_map.get(odid, "") if odid else "",
        "flags": int(flags),
        "data": data if isinstance(data, str) else "",
        "mod": int(mod),
        "usn": int(usn),
        "template_name": get_template_name(note, int(ord_value)),
    }


def get_note_cards(note, deck_name_map: dict) -> list[dict]:
    rows = mw.col.db.all(
        """
        select id, nid, did, ord, mod, usn, type, queue, due, ivl,
               factor, reps, lapses, left, odue, odid, flags, data
        from cards
        where nid = ?
        order by ord, id
        """,
        note.id,
    )

    cards = []
    for row in rows:
        try:
            cards.append(serialize_card_row(row, note, deck_name_map))
        except Exception as e:
            log(f"Exception serialize_card note_id={note.id}: {type(e).__name__}: {e}")
    return cards


class ImageSrcParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "img":
            return
        for name, value in attrs:
            if name and name.lower() == "src" and value:
                self.refs.append(html.unescape(value).strip())
                return


def extract_image_refs_from_html(value: str) -> list[str]:
    parser = ImageSrcParser()
    parser.feed(value)
    parser.close()
    return [ref for ref in parser.refs if ref]


def extract_image_refs(fields: dict) -> list[str]:
    refs = []
    for value in fields.values():
        if not isinstance(value, str):
            continue
        refs.extend(extract_image_refs_from_html(value))

    seen = set()
    ordered = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            ordered.append(r)
    return ordered


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = TAG_RE.sub(" ", text)
    text = text.replace("\xa0", " ")
    return text


def normalize_text(text: str) -> str:
    text = strip_html(text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def replace_cloze_with_content(text: str) -> str:
    def repl(match):
        content = match.group(1) or ""
        return content
    return CLOZE_RE.sub(repl, text or "")


def infer_kind(model_name: str, fields: dict) -> str:
    model_name_l = (model_name or "").lower()

    if "image occlusion" in model_name_l:
        return "image_occlusion"

    joined = " ".join(
        value for value in fields.values()
        if isinstance(value, str)
    )

    if IMAGE_OCCLUSION_RE.search(joined):
        return "image_occlusion"

    if CLOZE_RE.search(joined):
        return "cloze"

    return "basic"


def build_compare_text(kind: str, fields: dict) -> str:
    text_parts = []

    for value in fields.values():
        if not isinstance(value, str) or not value.strip():
            continue
        text_parts.append(value)

    joined = " ".join(text_parts)

    if kind == "cloze":
        joined = replace_cloze_with_content(joined)

    return normalize_text(joined)


def serialize_note(note, deck_name: str, deck_name_map: dict, included_deck_ids=None) -> dict:
    field_names = list(note.keys())
    fields = {name: note[name] for name in field_names}
    image_refs = extract_image_refs(fields)
    note_type = get_model_name(note.mid)
    kind = infer_kind(note_type, fields)
    compare_text = build_compare_text(kind, fields)
    cards = get_note_cards(note, deck_name_map)
    included_deck_ids = set(included_deck_ids or [])
    included_cards = [
        card for card in cards
        if not included_deck_ids or card.get("deck_id") in included_deck_ids
    ]
    primary_cards = included_cards or cards
    card_deck_names = []
    card_deck_ids = []
    for card in cards:
        card_deck_name = card.get("deck_name", "")
        card_deck_id = card.get("deck_id")
        if card_deck_name and card_deck_name not in card_deck_names:
            card_deck_names.append(card_deck_name)
        if card_deck_id is not None and card_deck_id not in card_deck_ids:
            card_deck_ids.append(card_deck_id)

    primary_deck = deck_name
    if not primary_deck and primary_cards:
        primary_deck = primary_cards[0].get("deck_name", "")

    return {
        "note_id": int(note.id),
        "deck": primary_deck,
        "root_deck": root_deck_name(primary_deck) if primary_deck else "",
        "note_type": note_type,
        "kind": kind,
        "compare_text": compare_text,
        "tags": list(note.tags),
        "field_names": field_names,
        "fields": fields,
        "has_images": len(image_refs) > 0,
        "image_refs": image_refs,
        "cards": cards,
        "card_count": len(cards),
        "card_deck_names": card_deck_names,
        "card_deck_ids": card_deck_ids,
        "cards_in_multiple_decks": len(card_deck_names) > 1 or len(card_deck_ids) > 1,
    }


def get_relevant_notes():
    deck_name_map = get_deck_name_map()
    note_ids = mw.col.db.list(
        """
        select id
        from notes
        order by id
        """,
    )

    notes = []
    seen_note_ids = set()

    for nid in note_ids:
        if nid in seen_note_ids:
            continue
        seen_note_ids.add(nid)

        try:
            note = mw.col.get_note(nid)
            notes.append(serialize_note(note, "", deck_name_map))
        except Exception as e:
            log(f"Exception serialize_note note_id={nid}: {type(e).__name__}: {e}")

    notes.sort(key=lambda x: (x["deck"], x["note_id"]))
    return notes


def build_payload() -> dict:
    if threading.get_ident() != ADDON_MAIN_THREAD_ID:
        raise RuntimeError("collection_access_outside_main_thread")
    notes = get_relevant_notes()
    deck_snapshot = build_deck_snapshot()
    notes_with_images = sum(1 for n in notes if n["has_images"])
    generated_at = datetime.now().astimezone().isoformat()
    return {
        "source": "anki",
        "event": "sync_did_finish_notes_snapshot",
        "timestamp": generated_at,
        "generated_at": generated_at,
        "snapshot_version": 2,
        "addon_version": ADDON_VERSION,
        "profile": mw.pm.name,
        "note_count": len(notes),
        "total_notes": deck_snapshot["total_notes"],
        "total_cards": deck_snapshot["total_cards"],
        "total_decks": deck_snapshot["deck_count"],
        "notes_with_images": notes_with_images,
        "decks": deck_snapshot["decks"],
        "deck_roots": deck_snapshot["roots"],
        "deck_summary": {
            "generated_at": generated_at,
            "total_decks": deck_snapshot["deck_count"],
            "total_cards": deck_snapshot["total_cards"],
            "total_notes": deck_snapshot["total_notes"],
        },
        "notes": notes,
    }


def load_tagging_token() -> str:
    token = os.environ.get(TAGGING_TOKEN_ENV, "").strip()
    if token:
        return token

    try:
        token = TAGGING_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as e:
        log(
            "tagging token file unreadable "
            f"path={TAGGING_TOKEN_FILE} error_type={type(e).__name__}"
        )
        return ""
    if not token or any(character.isspace() for character in token):
        log(f"tagging token file invalid path={TAGGING_TOKEN_FILE}")
        return ""
    return token


def tagging_api_request(path: str, method: str = "GET", payload=None):
    token = load_tagging_token()
    if not token:
        log(
            "tagging queue skipped: missing token "
            f"env={TAGGING_TOKEN_ENV} file={TAGGING_TOKEN_FILE}"
        )
        return None

    headers = {"X-Tagging-Token": token}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(
        TAGGING_API_BASE + path,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urlopen(req, timeout=TAGGING_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log(f"tagging HTTPError path={path} status={e.code} reason={e.reason} body_bytes={len(body.encode('utf-8'))}")
    except URLError as e:
        log(f"tagging URLError path={path} reason={e.reason}")
    except Exception as e:
        log(f"tagging request exception path={path} {type(e).__name__}: {e}")

    return None


def get_note_ids_for_deck_tree(deck_name: str) -> list[int]:
    deck_name_map = get_deck_name_map()
    deck_ids = [
        deck_id for deck_id, current_name in deck_name_map.items()
        if current_name == deck_name or current_name.startswith(deck_name + "::")
    ]
    if not deck_ids:
        raise ValueError(f"deck_not_found: {deck_name}")

    note_ids = []
    seen = set()
    for deck_id in deck_ids:
        ids = mw.col.db.list(
            """
            select distinct nid
            from cards
            where did = ?
            """,
            deck_id,
        )
        for nid in ids:
            nid_int = int(nid)
            if nid_int not in seen:
                seen.add(nid_int)
                note_ids.append(nid_int)

    return note_ids


def resolve_tagging_target_note_ids(target: dict) -> list[int]:
    if not isinstance(target, dict):
        raise ValueError("invalid_target")

    target_type = target.get("type")
    if target_type == "note_ids":
        note_ids = target.get("note_ids")
        if not isinstance(note_ids, list) or not note_ids:
            raise ValueError("invalid_note_ids_target")
        return [int(nid) for nid in note_ids]

    if target_type == "deck":
        deck_name = target.get("deck")
        if not isinstance(deck_name, str) or not deck_name.strip():
            raise ValueError("invalid_deck_target")
        return get_note_ids_for_deck_tree(deck_name.strip())

    raise ValueError(f"unsupported_target_type: {target_type}")


def persist_note(note) -> None:
    if hasattr(mw.col, "update_note"):
        mw.col.update_note(note)
    elif hasattr(note, "flush"):
        note.flush()
    else:
        raise RuntimeError("no_supported_note_persist_method")


def note_has_tag(note, tag: str) -> bool:
    return tag in set(note.tags)


def add_note_tag(note, tag: str) -> bool:
    if note_has_tag(note, tag):
        return False
    if hasattr(note, "add_tag"):
        note.add_tag(tag)
    else:
        note.tags.append(tag)
    return True


def remove_note_tag(note, tag: str) -> bool:
    if not note_has_tag(note, tag):
        return False
    if hasattr(note, "del_tag"):
        note.del_tag(tag)
    elif hasattr(note, "remove_tag"):
        note.remove_tag(tag)
    else:
        note.tags = [current for current in note.tags if current != tag]
    return True


def apply_tagging_operation(operation: dict) -> dict:
    operation_id = operation.get("operation_id", "")
    operation_type = operation.get("operation_type")
    tags = operation.get("tags")
    errors = []
    changed_note_ids = []
    successful_note_ids = []

    if operation_type not in {"add_tags", "remove_tags"}:
        raise ValueError(f"unsupported_operation_type: {operation_type}")
    if not isinstance(tags, list) or not tags:
        raise ValueError("invalid_tags")
    for tag in tags:
        if tag not in ALLOWED_TAGGING_TAGS:
            raise ValueError(f"unsupported_tag: {tag}")

    note_ids = resolve_tagging_target_note_ids(operation.get("target"))
    if not note_ids:
        raise ValueError("target_resolved_to_zero_notes")

    log(
        f"tagging apply start operation_id={operation_id} "
        f"type={operation_type} notes={len(note_ids)} tags={tags}"
    )

    for note_id in note_ids:
        try:
            note = mw.col.get_note(note_id)
            changed = False
            for tag in tags:
                if operation_type == "add_tags":
                    changed = add_note_tag(note, tag) or changed
                else:
                    changed = remove_note_tag(note, tag) or changed

            if changed:
                persist_note(note)
                changed_note_ids.append(int(note_id))

            successful_note_ids.append(int(note_id))
        except Exception as e:
            errors.append(f"note_id={note_id}: {type(e).__name__}: {e}")
            log(f"tagging apply note error operation_id={operation_id} {errors[-1]}")

    if errors and successful_note_ids:
        status = "partially_applied"
    elif errors:
        status = "failed"
    else:
        status = "applied"

    try:
        if hasattr(mw.col, "save"):
            mw.col.save()
    except Exception as e:
        log(f"tagging collection save warning operation_id={operation_id} {type(e).__name__}: {e}")

    return {
        "operation_id": operation_id,
        "status": status,
        "addon_profile": mw.pm.name,
        "affected_note_ids": changed_note_ids,
        "affected_count": len(changed_note_ids),
        "errors": errors[:20],
    }


def confirm_tagging_operation(result: dict) -> bool:
    operation_id = result.get("operation_id", "")
    response = tagging_api_request(
        "/tagging/operations/confirm",
        method="POST",
        payload=result,
    )
    if response and response.get("ok"):
        log(
            f"tagging confirm ok operation_id={operation_id} "
            f"status={result.get('status')} affected={result.get('affected_count')}"
        )
        return True

    log(f"tagging confirm failed operation_id={operation_id} status={result.get('status')}")
    return False


def process_tagging_queue() -> None:
    try:
        response = tagging_api_request(
            f"/tagging/operations?limit={TAGGING_MAX_OPERATIONS_PER_RUN}"
        )
        if not response:
            return

        operations = response.get("operations", [])
        if not isinstance(operations, list) or not operations:
            log("tagging queue empty")
            return

        log(f"tagging queue fetched count={len(operations)}")

        for operation in operations:
            operation_id = operation.get("operation_id", "")
            if operation.get("status") != "pending_addon_execution":
                log(
                    f"tagging skip non-pending operation_id={operation_id} "
                    f"status={operation.get('status')}"
                )
                continue

            try:
                result = apply_tagging_operation(operation)
            except Exception as e:
                result = {
                    "operation_id": operation_id,
                    "status": "failed",
                    "addon_profile": mw.pm.name,
                    "affected_note_ids": [],
                    "affected_count": 0,
                    "errors": [f"{type(e).__name__}: {e}"],
                }
                log(f"tagging apply failed operation_id={operation_id} {result['errors'][0]}")

            confirm_tagging_operation(result)
    except Exception as e:
        log(f"tagging queue exception {type(e).__name__}: {e}")


def get_or_create_anki_gpt_menu():
    tools_menu = mw.form.menuTools
    for action in tools_menu.actions():
        menu = action.menu()
        if menu and menu.title().replace("&", "") == "Anki GPT":
            return menu

    menu = QMenu("Anki GPT", mw)
    tools_menu.addMenu(menu)
    return menu


def process_organization_queue_now() -> None:
    log("organization manual processing requested")
    if organization_module is None:
        message = f"Fila organization indisponivel: {ORGANIZATION_IMPORT_ERROR}"
        log(message)
        showInfo(message)
        return

    log(
        "organization manual processing delegating "
        f"module_file={getattr(organization_module, '__file__', '')}"
    )
    summary = organization_module.process_organization_queue()
    if not isinstance(summary, dict):
        summary = {}

    message = (
        "Fila organization processada.\n"
        f"Buscadas: {summary.get('fetched', 0)}\n"
        f"Processadas: {summary.get('processed', 0)}\n"
        f"Sucesso: {summary.get('succeeded', 0)}\n"
        f"Falhas: {summary.get('failed', 0)}\n"
        f"Ignoradas: {summary.get('skipped', 0)}\n"
        f"Resultados enviados: {summary.get('confirmed', 0)}"
    )
    if summary.get("confirmation_failed", 0):
        message += f"\nResultados pendentes de envio: {summary.get('confirmation_failed')}"

    log(
        "organization manual processing finished "
        f"fetched={summary.get('fetched', 0)} processed={summary.get('processed', 0)} "
        f"succeeded={summary.get('succeeded', 0)} failed={summary.get('failed', 0)} "
        f"skipped={summary.get('skipped', 0)}"
    )
    showInfo(message)


def run_dialog(dialog) -> None:
    exec_method = getattr(dialog, "exec", None)
    if callable(exec_method):
        exec_method()
        return
    dialog.exec_()


def set_table_no_edit(table) -> None:
    try:
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    except Exception:
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)


def set_table_row_selection(table) -> None:
    try:
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    except Exception:
        table.setSelectionBehavior(QAbstractItemView.SelectRows)


def stretch_table_header(table) -> None:
    header = table.horizontalHeader()
    try:
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    except Exception:
        header.setSectionResizeMode(QHeaderView.Stretch)


def operation_value(operation: dict, *keys: str) -> str:
    for key in keys:
        value = operation.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


class OperationDetailsDialog(QDialog):
    def __init__(self, operation: dict, parent=None):
        super().__init__(parent)
        operation_id = operation_value(operation, "operation_id", "op_id", "id") or "unknown"
        self.setWindowTitle(f"Detalhes da operacao {operation_id}")
        self.resize(760, 520)

        layout = QVBoxLayout(self)
        title = QLabel(f"Operacao: {operation_id}", self)
        layout.addWidget(title)

        if organization_module is not None:
            mode = organization_module.operation_mode_label(operation)
            state = organization_module.operation_state_label(operation)
            result = organization_module.operation_result_label(operation)
            error = organization_module.operation_error_label(operation)
            layout.addWidget(QLabel(f"Modo: {mode}", self))
            layout.addWidget(QLabel(f"Estado: {state}", self))
            if result:
                layout.addWidget(QLabel(f"Resultado: {result}", self))
            if error:
                layout.addWidget(QLabel(f"Erro: {error}", self))

        details = QTextEdit(self)
        details.setReadOnly(True)
        details.setPlainText(json.dumps(operation, ensure_ascii=False, indent=2, sort_keys=True))
        layout.addWidget(details)

        close_button = QPushButton("Fechar", self)
        close_button.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)


class OperationsDialog(QDialog):
    STATUS_FILTERS = [
        ("Todos", "all"),
        ("Pendentes/problemas", "problems"),
        ("Pendente", "state:pending"),
        ("Em processamento", "state:running"),
        ("Conclu\u00edda", "state:done"),
        ("Parcialmente aplicada", "state:partially_applied"),
        ("Falhou", "state:failed"),
        ("Ignorada", "state:skipped"),
        ("Pr\u00e9via", "mode:dry_run"),
        ("Aplica\u00e7\u00e3o real", "mode:apply"),
        ("Estado desconhecido", "state:unknown"),
    ]
    TYPE_LABELS = {
        "reorder_cards_by_material": "Reordenar",
        "update_note_fields": "Atualizar cards",
        "normalize": "Normalizar",
        "highlight": "Grifar",
        "normalize/highlight": "Normalizar/grifar",
        "normalize_highlight": "Normalizar/grifar",
        "normalize_and_highlight": "Normalizar/grifar",
        "highlight_normalize": "Normalizar/grifar",
    }
    COLUMNS = [
        "Modo",
        "Estado",
        "Tipo",
        "Alvo/deck",
        "Resultado",
        "Criada em",
        "ID",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.operations = []
        self.setWindowTitle("Anki GPT - Opera\u00e7\u00f5es em andamento")
        self.resize(1320, 620)

        layout = QVBoxLayout(self)
        top_bar = QHBoxLayout()
        top_bar.addWidget(QLabel("Filtro:", self))
        self.status_filter = QComboBox(self)
        for label, value in self.STATUS_FILTERS:
            self.status_filter.addItem(label, value)
        self.status_filter.currentTextChanged.connect(lambda _text: self.populate_table())
        top_bar.addWidget(self.status_filter)
        top_bar.addStretch(1)

        self.refresh_button = QPushButton("Atualizar", self)
        self.refresh_button.clicked.connect(lambda: self.refresh_operations(refresh_remote=True))
        top_bar.addWidget(self.refresh_button)
        layout.addLayout(top_bar)

        self.table = QTableWidget(self)
        self.table.setColumnCount(len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        set_table_no_edit(self.table)
        set_table_row_selection(self.table)
        self.configure_columns()
        self.set_context_menu_policy()
        self.table.cellDoubleClicked.connect(self.open_details_from_row)
        layout.addWidget(self.table)

        bottom_bar = QHBoxLayout()
        self.status_label = QLabel("", self)
        bottom_bar.addWidget(self.status_label)
        bottom_bar.addStretch(1)
        close_button = QPushButton("Fechar", self)
        close_button.clicked.connect(self.close)
        bottom_bar.addWidget(close_button)
        layout.addLayout(bottom_bar)

        self.refresh_operations(refresh_remote=True)

    def configure_columns(self) -> None:
        widths = [120, 150, 170, 370, 180, 200, 160]
        for index, width in enumerate(widths):
            self.table.setColumnWidth(index, width)

        header = self.table.horizontalHeader()
        try:
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        except Exception:
            header.setSectionResizeMode(QHeaderView.Interactive)
            header.setSectionResizeMode(3, QHeaderView.Stretch)

    def set_context_menu_policy(self) -> None:
        try:
            self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        except Exception:
            self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)

    def refresh_operations(self, refresh_remote: bool = False) -> None:
        if organization_module is None:
            message = f"Operacoes indisponiveis: {ORGANIZATION_IMPORT_ERROR}"
            log(message)
            showInfo(message)
            return

        try:
            refreshed_operations = organization_module.list_known_operations(
                refresh_remote=refresh_remote
            )
            self.operations = refreshed_operations
            log(
                "operations panel refreshed "
                f"refresh_remote={refresh_remote} count={len(self.operations)}"
            )
        except Exception as e:
            log(f"operations panel refresh failed {type(e).__name__}: {e}")
            showInfo(
                "Falha ao atualizar operacoes. A lista anterior foi mantida. "
                f"Veja o log: {LOG_FILE}"
            )

        self.populate_table()

    def filtered_operations(self) -> list[dict]:
        selected = self.status_filter.currentData()
        if selected in {None, "all"}:
            return self.operations
        if selected == "problems":
            return [
                op for op in self.operations
                if self.raw_status(op) in {"pending", "running", "failed", "partially_applied"}
            ]
        if isinstance(selected, str) and selected.startswith("state:"):
            state = selected.split(":", 1)[1]
            return [op for op in self.operations if self.raw_status(op) == state]
        if selected == "mode:dry_run":
            return [op for op in self.operations if self.raw_dry_run(op) is True]
        if selected == "mode:apply":
            return [op for op in self.operations if self.raw_dry_run(op) is False]
        return self.operations

    def populate_table(self) -> None:
        operations = self.filtered_operations()
        self.visible_operations = operations
        self.table.setRowCount(len(operations))
        for row, operation in enumerate(operations):
            values = [
                self.friendly_mode(operation),
                self.friendly_status(operation),
                self.friendly_type(operation),
                operation_value(operation, "target", "deck", "target_deck"),
                self.result_label(operation),
                operation_value(operation, "created_at", "created", "timestamp"),
                self.short_operation_id(operation),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 6:
                    item.setToolTip(self.full_operation_id(operation))
                self.table.setItem(row, column, item)

        index_path = ""
        if organization_module is not None:
            index_path = str(getattr(organization_module, "OPERATIONS_INDEX_FILE", ""))
        self.status_label.setText(self.footer_summary())
        self.status_label.setToolTip(index_path)

    def raw_status(self, operation: dict) -> str:
        if organization_module is not None:
            return organization_module.operation_status(operation)
        return operation_value(operation, "status") or "unknown"

    def friendly_status(self, operation: dict) -> str:
        if organization_module is not None:
            return organization_module.operation_state_label(operation)
        return self.raw_status(operation)

    def raw_dry_run(self, operation: dict):
        if organization_module is not None:
            return organization_module.operation_dry_run(operation)
        execution_mode = operation.get("execution_mode")
        if execution_mode in {"preview", "direct"}:
            return execution_mode == "preview"
        return operation.get("dry_run") if isinstance(operation.get("dry_run"), bool) else None

    def friendly_mode(self, operation: dict) -> str:
        if organization_module is not None:
            return organization_module.operation_mode_label(operation)
        dry_run = self.raw_dry_run(operation)
        return "Pr\u00e9via" if dry_run is True else "Aplica\u00e7\u00e3o real" if dry_run is False else "N\u00e3o informado"

    def result_label(self, operation: dict) -> str:
        if organization_module is not None:
            return organization_module.operation_result_label(operation)
        return ""

    def raw_operation_type(self, operation: dict) -> str:
        return operation_value(operation, "operation_type", "type", "function", "operation")

    def friendly_type(self, operation: dict) -> str:
        operation_type = self.raw_operation_type(operation)
        normalized = operation_type.strip().lower()
        if normalized in self.TYPE_LABELS:
            return self.TYPE_LABELS[normalized]
        if "normalize" in normalized and "highlight" in normalized:
            return "Normalizar/grifar"
        return operation_type

    def full_operation_id(self, operation: dict) -> str:
        return operation_value(operation, "operation_id", "op_id", "id")

    def short_operation_id(self, operation: dict) -> str:
        operation_id = self.full_operation_id(operation)
        if len(operation_id) <= 22:
            return operation_id
        return f"{operation_id[:10]}\u2026{operation_id[-8:]}"

    def footer_summary(self) -> str:
        total = len(self.operations)
        done = sum(1 for op in self.operations if self.raw_status(op) == "done")
        dry_run = sum(1 for op in self.operations if self.raw_dry_run(op) is True)
        failed = sum(1 for op in self.operations if self.raw_status(op) == "failed")
        return (
            f"{total} opera\u00e7\u00f5es \u2022 "
            f"{done} conclu\u00eddas \u2022 "
            f"{dry_run} pr\u00e9vias \u2022 "
            f"{failed} falhas"
        )

    def operation_at_row(self, row: int):
        if row < 0 or row >= len(getattr(self, "visible_operations", [])):
            return None
        return self.visible_operations[row]

    def open_details_from_row(self, row: int, _column: int) -> None:
        operation = self.operation_at_row(row)
        if operation:
            self.show_details(operation)

    def show_context_menu(self, position) -> None:
        row = self.table.rowAt(position.y())
        operation = self.operation_at_row(row)
        if not operation:
            return

        self.table.selectRow(row)
        menu = QMenu(self)
        details_action = menu.addAction("Ver detalhes")
        copy_id_action = menu.addAction("Copiar ID")
        copy_target_action = menu.addAction("Copiar alvo/deck")
        menu.addSeparator()
        remove_action = menu.addAction("Remover do \u00edndice local")
        if operation_value(operation, "source") == "reorganization_log":
            remove_action.setEnabled(False)
            remove_action.setToolTip("Registro inferido do log de aplicacao; remocao direta fica desabilitada por seguranca.")
        resend_action = menu.addAction("Reenviar")
        resend_action.setEnabled(False)
        resend_action.setToolTip("Reenvio individual permanece bloqueado para evitar aplicacao duplicada.")

        selected = self.exec_menu(menu, self.table.viewport().mapToGlobal(position))
        if selected == details_action:
            self.show_details(operation)
        elif selected == copy_id_action:
            self.copy_text(self.full_operation_id(operation), "ID copiado.")
        elif selected == copy_target_action:
            self.copy_text(operation_value(operation, "target", "deck", "target_deck"), "Alvo/deck copiado.")
        elif selected == remove_action:
            self.remove_operation(operation)
        elif selected == resend_action:
            self.resend_operation(operation)

    def exec_menu(self, menu, position):
        exec_method = getattr(menu, "exec", None)
        if callable(exec_method):
            return exec_method(position)
        return menu.exec_(position)

    def copy_text(self, text: str, message: str) -> None:
        QApplication.clipboard().setText(text or "")
        self.status_label.setText(message)

    def show_details(self, operation: dict) -> None:
        dialog = OperationDetailsDialog(operation, self)
        run_dialog(dialog)

    def resend_operation(self, operation: dict) -> None:
        operation_id = operation_value(operation, "operation_id", "op_id", "id") or "unknown"
        status = operation_value(operation, "status") or "unknown"
        log(f"operations panel resend requested operation_id={operation_id} status={status}")
        showInfo(
            "Reenvio individual nao foi executado.\n\n"
            "O addon processa operacoes pela fila remota e usa recibos idempotentes "
            "para evitar aplicacao real duplicada. Use 'Sincronizar tudo agora' ou processe a fila atual."
        )

    def remove_operation(self, operation: dict) -> None:
        if organization_module is None:
            showInfo(f"Operacoes indisponiveis: {ORGANIZATION_IMPORT_ERROR}")
            return

        operation_id = operation_value(operation, "operation_id", "op_id", "id")
        if not operation_id:
            showInfo("Operacao sem ID nao pode ser removida do indice local.")
            return

        status = self.raw_status(operation)
        if status == "done" and self.raw_dry_run(operation) is False:
            prompt = (
                "Esta operacao ja foi aplicada.\n\n"
                "Esta acao nao desfaz alteracoes em cards; ela apenas remove o registro do indice local. Continuar?"
            )
        else:
            prompt = (
                "Remover esta operacao do indice local?\n\n"
                "Esta acao nao altera cards e nao remove a fila remota. Se a operacao ainda existir no backend, "
                "ela pode reaparecer ao atualizar."
            )

        if not askUser(prompt, parent=self):
            return

        result = organization_module.remove_operation_from_local_index(operation_id)
        if result.get("ok"):
            showInfo("Registro local removido. Nenhuma alteracao em cards foi desfeita.")
            self.refresh_operations(refresh_remote=False)
            return

        showInfo(f"Nao foi possivel remover do indice local: {result.get('error', 'erro desconhecido')}")


def show_operations_panel() -> None:
    global OPERATIONS_DIALOG
    OPERATIONS_DIALOG = OperationsDialog(mw)
    OPERATIONS_DIALOG.show()
    OPERATIONS_DIALOG.raise_()
    OPERATIONS_DIALOG.activateWindow()


def process_organization_queue_for_sync() -> dict:
    if organization_module is None:
        message = f"Fila organization indisponivel: {ORGANIZATION_IMPORT_ERROR}"
        log(message)
        return {
            "fetched": 0,
            "processed": 0,
            "succeeded": 0,
            "failed": 1,
            "skipped": 0,
            "confirmed": 0,
            "confirmation_failed": 0,
            "changed": False,
            "changed_operations": 0,
            "changed_note_count": 0,
            "changed_card_count": 0,
            "errors": [message],
        }

    log(
        "organization combined sync delegating "
        f"module_file={getattr(organization_module, '__file__', '')}"
    )
    summary = organization_module.process_organization_queue()
    return summary if isinstance(summary, dict) else {}


def organization_summary_failed(summary: dict) -> bool:
    return bool(
        summary.get("failed", 0)
        or summary.get("confirmation_failed", 0)
        or summary.get("errors")
    )


def organization_summary_changed(summary: dict) -> bool:
    return bool(
        summary.get("changed")
        or summary.get("changed_operations", 0)
        or summary.get("changed_note_count", 0)
        or summary.get("changed_card_count", 0)
    )


def authentication_required_message() -> str:
    return (
        "Autenticação ausente.\n"
        f"Configure {TAGGING_TOKEN_ENV} ou:\n"
        f"{TAGGING_TOKEN_FILE}"
    )


def snapshot_failure_message(upload_result: dict) -> str:
    cause = upload_result.get("cause")
    if cause == "missing_authentication_token":
        return authentication_required_message()
    if cause == "authentication_rejected":
        return (
            "Autenticação rejeitada pelo backend.\n"
            f"Verifique {TAGGING_TOKEN_ENV} ou:\n"
            f"{TAGGING_TOKEN_FILE}"
        )
    if cause == "network_error":
        return f"Falha de rede ao enviar o snapshot. Veja o log: {LOG_FILE}"
    if cause == "server_error":
        return f"O backend recusou o snapshot. Veja o log: {LOG_FILE}"
    if cause == "invalid_response":
        return f"O backend retornou uma resposta inválida. Veja o log: {LOG_FILE}"
    if cause == "auto_publish_paused":
        return "Publicação pausada pela configuração local."
    return f"Falha ao enviar o snapshot. Veja o log: {LOG_FILE}"


def combined_snapshot_failure(stage: str, upload_result: dict) -> dict:
    stage_label = "Snapshot inicial" if stage == "initial" else "Snapshot final"
    return {
        "lines": [snapshot_failure_message(upload_result)],
        "fatal_failures": [stage_label],
        "warnings": [],
        "error": f"{stage}_snapshot_upload_failed",
        "failure_cause": upload_result.get("cause") or "unknown_error",
        "upload_result": dict(upload_result),
    }


def post_snapshot_payload_result(payload_dict: dict, reason: str) -> dict:
    global LAST_POSTED_SNAPSHOT_HASH, LAST_CONFIRMED_GENERATION_ID

    if AUTO_PUBLISH_PAUSE_FILE.exists():
        log(f"snapshot sync skipped reason={reason} error=auto_publish_paused")
        writer = globals().get("write_addon_runtime_diagnostics")
        if callable(writer):
            writer("snapshot_publish_paused")
        return {"ok": False, "cause": "auto_publish_paused"}

    token = load_tagging_token()
    if not token:
        log(
            "snapshot sync skipped cause=missing_authentication_token "
            f"env={TAGGING_TOKEN_ENV} file={TAGGING_TOKEN_FILE}"
        )
        return {"ok": False, "cause": "missing_authentication_token"}

    LAST_POSTED_SNAPSHOT_HASH = snapshot_content_hash(payload_dict)
    payload = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")

    req = Request(
        SYNC_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Tagging-Token": token,
        },
        method="POST",
    )

    post_started = time.perf_counter()
    try:
        with urlopen(req, timeout=180) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                response_payload = json.loads(body)
            except json.JSONDecodeError:
                response_payload = {}
            generation_id = response_payload.get("generation_id") if isinstance(response_payload, dict) else None
            if not isinstance(generation_id, str) or not generation_id:
                log(
                    f"POST confirmation missing reason={reason} url={SYNC_URL} status={resp.status} "
                    f"duration_ms={duration_ms(post_started)} step=post_sync_full"
                )
                return {
                    "ok": False,
                    "cause": "invalid_response",
                    "status": int(resp.status),
                }
            LAST_CONFIRMED_GENERATION_ID = generation_id
            log(
                f"POST ok reason={reason} url={SYNC_URL} status={resp.status} "
                f"duration_ms={duration_ms(post_started)} step=post_sync_full "
                f"generation_id={generation_id} body_bytes={len(body.encode('utf-8'))}"
            )
        return {
            "ok": True,
            "cause": "",
            "status": int(resp.status),
            "generation_id": generation_id,
        }
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        cause = "authentication_rejected" if e.code in {401, 403} else "server_error"
        log(
            f"HTTPError reason={reason} cause={cause} url={SYNC_URL} "
            f"status={e.code} reason={e.reason} "
            f"duration_ms={duration_ms(post_started)} step=post_sync_full body_bytes={len(body.encode('utf-8'))}"
        )
        return {"ok": False, "cause": cause, "status": int(e.code)}
    except URLError as e:
        log(
            f"URLError reason={reason} cause=network_error url={SYNC_URL} "
            f"reason={e.reason} duration_ms={duration_ms(post_started)} step=post_sync_full"
        )
        return {"ok": False, "cause": "network_error"}
    except Exception as e:
        log(
            f"Exception POST reason={reason} cause=network_error url={SYNC_URL} "
            f"{type(e).__name__}: {e} duration_ms={duration_ms(post_started)} "
            "step=post_sync_full"
        )
        return {"ok": False, "cause": "network_error"}


def post_snapshot_payload(payload_dict: dict, reason: str) -> bool:
    return bool(post_snapshot_payload_result(payload_dict, reason).get("ok"))


def post_full_snapshot_result(reason: str) -> dict:
    if not load_tagging_token():
        log(
            "snapshot sync skipped before collection read "
            "cause=missing_authentication_token "
            f"env={TAGGING_TOKEN_ENV} file={TAGGING_TOKEN_FILE}"
        )
        return {"ok": False, "cause": "missing_authentication_token"}
    snapshot_started = time.perf_counter()
    try:
        payload_dict = build_payload()
        log(
            f"payload montado reason={reason} note_count={payload_dict['note_count']} "
            f"total_decks={payload_dict.get('total_decks')} "
            f"total_cards={payload_dict.get('total_cards')} "
            f"notes_with_images={payload_dict['notes_with_images']} "
            f"snapshot_hash={snapshot_content_hash(payload_dict)} "
            f"duration_ms={duration_ms(snapshot_started)} step=snapshot_local"
        )
    except Exception as e:
        log(f"Exception build_payload reason={reason} {type(e).__name__}: {e}")
        return {"ok": False, "cause": "snapshot_build_error"}
    return post_snapshot_payload_result(payload_dict, reason)


def post_full_snapshot(reason: str) -> bool:
    return bool(post_full_snapshot_result(reason).get("ok"))


def sync_full_snapshot_now() -> None:
    log("manual full snapshot sync requested")
    decision = publication_decision("manual")
    if not decision["allowed"]:
        log(f"manual full snapshot skipped reason={decision['reason']} mode={decision['mode']}")
        showInfo(f"Publicacao bloqueada: {decision['reason']} (modo {decision['mode']}).")
        return
    upload_result = post_full_snapshot_result("manual_full_sync")
    if not upload_result.get("ok"):
        showInfo(snapshot_failure_message(upload_result))
        return

    def after_media_publish(result, error) -> None:
        if error is not None:
            log(f"manual full snapshot media publish failed {type(error).__name__}: {error}")
            showInfo(
                "Snapshot completo enviado, mas a publicacao de midia falhou.\n"
                f"Veja o log: {LOG_FILE}"
            )
            return

        if not result or not result.get("ok"):
            log(
                "manual full snapshot media publish failed "
                f"error={result.get('error') if result else 'missing_result'} "
                f"command={result.get('command', '') if result else ''}"
            )
            showInfo(
                "Snapshot completo enviado, mas a publicacao de midia falhou.\n"
                f"Veja o log: {LOG_FILE}"
            )
            return

        showInfo("Snapshot completo enviado para o backend e midia publicada.")

    start_media_publish_step(
        reason="manual_full_sync_media_publish",
        step_label="Publicando midia do snapshot completo",
        on_done=after_media_publish,
        dry_run=False,
        show_progress=True,
    )


def sync_everything_now() -> None:
    global COMBINED_SYNC_IN_FLIGHT

    decision = publication_decision("manual")
    if not decision["allowed"]:
        log(f"manual combined sync skipped reason={decision['reason']} mode={decision['mode']}")
        showInfo(f"Publicacao bloqueada: {decision['reason']} (modo {decision['mode']}).")
        return

    if not load_tagging_token():
        log(
            "manual combined sync skipped cause=missing_authentication_token "
            f"env={TAGGING_TOKEN_ENV} file={TAGGING_TOKEN_FILE}"
        )
        showInfo(authentication_required_message())
        return

    if COMBINED_SYNC_IN_FLIGHT:
        showInfo("Sincronizacao completa ja esta em andamento.")
        return

    COMBINED_SYNC_IN_FLIGHT = True
    log("manual combined sync requested")
    progress_started = progress_start("Enviando snapshot inicial...", max_value=COMBINED_SYNC_PROGRESS_MAX)

    def set_progress(label: str, value: int) -> None:
        log(f"manual combined sync progress value={value} label={label}")
        run_on_main_thread(
            lambda: progress_update(
                label,
                value=value,
                max_value=COMBINED_SYNC_PROGRESS_MAX,
            )
        )

    def build_message(result: dict) -> str:
        lines = result.get("lines", [])
        fatal_failures = result.get("fatal_failures", [])
        warnings = result.get("warnings", [])
        publish_schedule_status = result.get("publish_schedule_status")

        if fatal_failures:
            message = "Sincronizacao completa falhou.\n"
            message += "Etapa(s) com falha: " + ", ".join(fatal_failures) + "\n"
        elif warnings:
            message = "Sincronizacao completa concluida com avisos.\n"
            message += "Etapa(s) com aviso: " + ", ".join(warnings) + "\n"
        elif publish_schedule_status == "skipped_in_flight":
            message = "Sincronizacao principal concluida. Ja havia publicacao de midia em andamento.\n"
        elif publish_schedule_status == "queued":
            message = "Sincronizacao principal concluida. Publicacao de midia e rebuild remoto continuam em segundo plano.\n"
        else:
            message = "Sincronizacao completa concluida.\n"

        message += "\n".join(lines)
        return message

    def process_queue_on_main_thread() -> tuple[dict, dict | None]:
        lines = []
        fatal_failures = []
        warnings = []
        lines.append("Snapshot inicial enviado.")

        set_progress("Processando fila organization...", 1)
        organization_started = time.perf_counter()
        summary = process_organization_queue_for_sync()
        log(f"duration_ms={duration_ms(organization_started)} step=organization_queue reason=manual_combined_sync")
        processed = summary.get("processed", 0)
        changed_note_count = summary.get("changed_note_count", 0)
        changed = organization_summary_changed(summary)
        lines.append(
            "Fila organization processada: "
            f"{processed} operacoes, {changed_note_count} notes alteradas."
        )
        log(
            "manual combined organization summary "
            f"fetched={summary.get('fetched', 0)} processed={processed} "
            f"succeeded={summary.get('succeeded', 0)} failed={summary.get('failed', 0)} "
            f"skipped={summary.get('skipped', 0)} confirmed={summary.get('confirmed', 0)} "
            f"confirmation_failed={summary.get('confirmation_failed', 0)} "
            f"changed={changed} changed_operations={summary.get('changed_operations', 0)} "
            f"changed_note_count={changed_note_count} "
            f"changed_card_count={summary.get('changed_card_count', 0)}"
        )

        if organization_summary_failed(summary):
            warnings.append("Fila organization")
            lines.append(f"Fila organization com avisos. Veja o log: {LOG_FILE}")
            for error_message in summary.get("errors", [])[:3]:
                log(f"manual combined organization error: {error_message}")

        if changed:
            set_progress("Materializando snapshot final...", 2)
            try:
                final_payload = build_payload()
            except Exception as e:
                fatal_failures.append("Snapshot final")
                lines.append(f"Snapshot final falhou. Veja o log: {LOG_FILE}")
                log(f"manual combined sync failed step=final_snapshot_build {type(e).__name__}: {e}")
                return ({
                    "lines": lines,
                    "fatal_failures": fatal_failures,
                    "warnings": warnings,
                }, None)
        else:
            lines.append("Snapshot final nao necessario; fila sem alteracao real.")
            final_payload = None

        return ({
            "lines": lines,
            "fatal_failures": fatal_failures,
            "warnings": warnings,
        }, final_payload)

    def finish_from_result(result=None, error=None) -> None:
        global COMBINED_SYNC_IN_FLIGHT

        if progress_started:
            progress_finish()

        COMBINED_SYNC_IN_FLIGHT = False

        if error is not None:
            log(f"manual combined sync exception {type(error).__name__}: {error}")
            result = {
                "lines": [f"Erro inesperado: {type(error).__name__}: {error}. Veja o log: {LOG_FILE}"],
                "fatal_failures": ["Erro inesperado"],
                "warnings": [],
            }

        if not isinstance(result, dict):
            result = {
                "lines": [f"Resultado invalido do fluxo. Veja o log: {LOG_FILE}"],
                "fatal_failures": ["Resultado invalido"],
                "warnings": [],
            }

        log(
            "manual combined sync finished "
            f"fatal_failures={result.get('fatal_failures', [])} "
            f"warnings={result.get('warnings', [])} "
            f"error={result.get('error', '')} "
            f"failure_cause={result.get('failure_cause', '')}"
        )
        showInfo(build_message(result))

    def finish_with_media(result: dict, reason: str) -> None:
        set_progress("Agendando publicacao de midia em segundo plano...", 3)
        publish_schedule = schedule_media_publish_background(
            reason=reason,
            step_label="Publicando midia em segundo plano",
            dry_run=False,
            show_progress=False,
        )
        result["publish_schedule_status"] = publish_schedule.get("status")
        if publish_schedule.get("status") == "skipped_in_flight":
            result.setdefault("lines", []).append("Ja havia publicacao de midia em andamento.")
        else:
            result.setdefault("lines", []).append(
                "Publicacao de midia e rebuild remoto agendados em segundo plano."
            )
        set_progress("Concluido.", 5)
        finish_from_result(result, None)

    taskman = getattr(mw, "taskman", None)
    run_in_background = getattr(taskman, "run_in_background", None) if taskman is not None else None

    # Collection materialization must happen on the Anki/UI thread. Only the
    # detached Python payload is handed to a worker for network I/O.
    try:
        set_progress("Materializando snapshot inicial...", 0)
        initial_payload = build_payload()
    except Exception as e:
        finish_from_result(None, e)
        return

    if not callable(run_in_background):
        try:
            initial_upload = post_snapshot_payload_result(
                initial_payload,
                "manual_combined_initial_sync",
            )
            if not initial_upload.get("ok"):
                finish_from_result(combined_snapshot_failure("initial", initial_upload), None)
                return
            result, final_payload = process_queue_on_main_thread()
            if final_payload is not None:
                final_upload = post_snapshot_payload_result(
                    final_payload,
                    "manual_combined_final_sync",
                )
                if not final_upload.get("ok"):
                    finish_from_result(combined_snapshot_failure("final", final_upload), None)
                    return
                result["lines"].append("Snapshot final enviado.")
                finish_with_media(result, "manual_combined_after_final_sync")
            else:
                finish_with_media(result, "manual_combined_after_initial_sync")
        except Exception as e:
            finish_from_result(None, e)
        return

    def schedule_snapshot_upload(payload: dict, reason: str, on_done) -> None:
        def done(future) -> None:
            def handle_main() -> None:
                try:
                    on_done(future.result(), None)
                except Exception as e:
                    on_done(None, e)

            run_on_main_thread(handle_main)

        run_in_background(lambda: post_snapshot_payload_result(payload, reason), done)

    def after_final_upload(upload_result, error) -> None:
        if error is not None:
            finish_from_result(None, error)
            return
        if not isinstance(upload_result, dict) or not upload_result.get("ok"):
            normalized = (
                upload_result
                if isinstance(upload_result, dict)
                else {"ok": False, "cause": "unknown_error"}
            )
            finish_from_result(combined_snapshot_failure("final", normalized), None)
            return
        pending_result["value"]["lines"].append("Snapshot final enviado.")
        finish_with_media(pending_result["value"], "manual_combined_after_final_sync")

    def after_initial_upload(upload_result, error) -> None:
        if error is not None:
            finish_from_result(None, error)
            return
        if not isinstance(upload_result, dict) or not upload_result.get("ok"):
            normalized = (
                upload_result
                if isinstance(upload_result, dict)
                else {"ok": False, "cause": "unknown_error"}
            )
            finish_from_result(combined_snapshot_failure("initial", normalized), None)
            return
        try:
            result, final_payload = process_queue_on_main_thread()
            if result.get("fatal_failures"):
                finish_from_result(result, None)
                return
            if final_payload is None:
                finish_with_media(result, "manual_combined_after_initial_sync")
                return
            pending_result["value"] = result
            set_progress("Enviando snapshot final...", 2)
            schedule_snapshot_upload(
                final_payload,
                "manual_combined_final_sync",
                after_final_upload,
            )
        except Exception as e:
            finish_from_result(None, e)

    pending_result = {"value": None}
    set_progress("Enviando snapshot inicial...", 0)
    schedule_snapshot_upload(
        initial_payload,
        "manual_combined_initial_sync",
        after_initial_upload,
    )


def setup_anki_gpt_menu() -> None:
    global MENU_REGISTERED
    if MENU_REGISTERED:
        return

    try:
        menu = get_or_create_anki_gpt_menu()
        for action in list(menu.actions()):
            action_text = action.text().replace("&", "")
            if action_text in {
                "Sincronizar snapshot completo agora",
                "Processar fila organization agora",
            }:
                menu.removeAction(action)

        existing_actions = {action.text().replace("&", "") for action in menu.actions()}

        operations_action_text = "Opera\u00e7\u00f5es"
        if operations_action_text not in existing_actions:
            action = QAction(operations_action_text, mw)
            action.triggered.connect(show_operations_panel)
            menu.addAction(action)

        action_text = "Sincronizar tudo agora"
        if action_text not in existing_actions:
            action = QAction(action_text, mw)
            action.triggered.connect(sync_everything_now)
            menu.addAction(action)

        policy_state = load_auto_publish_policy()
        policy_labels = {
            "disabled": "Desativada",
            "manual": "Somente manual",
            "after_anki_sync": "Depois do sync do Anki",
            "always": "Sempre (decisao explicita)",
        }
        policy_menu = QMenu(f"Publicacao automatica: {policy_labels[policy_state['mode']]}", menu)
        policy_actions = {}

        def select_policy(mode: str) -> None:
            try:
                saved = save_auto_publish_policy(mode)
                for candidate, policy_action in policy_actions.items():
                    policy_action.setChecked(candidate == saved["mode"])
                policy_menu.setTitle(f"Publicacao automatica: {policy_labels[saved['mode']]}")
                showInfo(f"Politica de publicacao: {policy_labels[saved['mode']]}")
            except Exception as e:
                log(f"auto publish policy change failed {type(e).__name__}: {e}")
                showInfo(f"Falha ao alterar politica de publicacao: {type(e).__name__}.")

        for mode in ("disabled", "manual", "after_anki_sync", "always"):
            policy_action = QAction(policy_labels[mode], mw)
            policy_action.setCheckable(True)
            policy_action.setChecked(mode == policy_state["mode"])
            policy_action.triggered.connect(lambda _checked=False, selected=mode: select_policy(selected))
            policy_actions[mode] = policy_action
            policy_menu.addAction(policy_action)
        menu.addMenu(policy_menu)

        MENU_REGISTERED = True
        write_addon_runtime_diagnostics("menu_registered")
        log("anki gpt menu registered")
    except Exception as e:
        log(f"organization menu setup failed {type(e).__name__}: {e}")


def on_sync_did_finish() -> None:
    log("hook sync_did_finish disparou")
    decision = publication_decision("anki_sync_did_finish")
    log(
        "auto publish decision "
        f"trigger=anki_sync_did_finish mode={decision['mode']} "
        f"configured={decision['configured']} allowed={decision['allowed']} reason={decision['reason']}"
    )
    if not decision["allowed"]:
        return
    if not post_full_snapshot("anki_sync_did_finish"):
        return

    def after_hook_media_publish(result, error) -> None:
        if error is not None:
            log(f"hook media publish failed {type(error).__name__}: {error}")
            return
        if not result or not result.get("ok"):
            log(
                "hook media publish failed "
                f"error={result.get('error') if result else 'missing_result'} "
                f"command={result.get('command', '') if result else ''}"
            )
            return
        log("hook media publish concluida")

    start_media_publish_step(
        reason="anki_sync_did_finish_media_publish",
        step_label="Publicando midia do sync automatico",
        on_done=after_hook_media_publish,
        dry_run=False,
        show_progress=False,
    )

    process_tagging_queue()
    if organization_module is None:
        log(f"organization queue unavailable: {ORGANIZATION_IMPORT_ERROR}")
    else:
        log(
            "organization sync hook delegating "
            f"module_file={getattr(organization_module, '__file__', '')}"
        )
        organization_module.process_organization_queue()


gui_hooks.sync_did_finish.append(on_sync_did_finish)
setup_anki_gpt_menu()
log("hook registrado")
