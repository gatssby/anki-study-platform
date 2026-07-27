#!/usr/bin/env bash
set -euo pipefail

REMOTE="oracle-vps"
REMOTE_BASE="/home/ubuntu/anki-gpt-sync"

LOCAL_BASE="${ANKI_GPT_RUNTIME_DIR:-$HOME/Library/Application Support/Anki2/addon-data/anki_gpt_sync}"
if [[ "$LOCAL_BASE" != /* ]]; then
  LOCAL_BASE="$HOME/$LOCAL_BASE"
fi
LOCAL_STATE="$LOCAL_BASE/state"
STAGING_DIR="$LOCAL_BASE/staging"
REFS_FILE="$STAGING_DIR/media_refs.txt"
SNAPSHOT_STATUS_FILE="$STAGING_DIR/snapshot_status.json"
CHANGED_LIST="$STAGING_DIR/media_publish_files_from.bin"
COMPARE_FILE="$STAGING_DIR/media_publish_compare.json"
MANIFEST_FILE="$LOCAL_STATE/media_publish_manifest.json"
PUBLISH_STATE_FILE="$LOCAL_STATE/anki_publish_state.json"
MEDIA_DIR="$HOME/Library/Application Support/Anki2/User 1/collection.media"

DRY_RUN=0
DELETE_MODE=0
IGNORE_EXISTING=0
SKIP_REBUILD=0
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --no-delete)
      DELETE_MODE=0
      ;;
    --delete)
      DELETE_MODE=1
      ;;
    --ignore-existing)
      IGNORE_EXISTING=1
      ;;
    --skip-rebuild)
      SKIP_REBUILD=1
      ;;
    --force)
      FORCE=1
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

now_ms() {
  python3 -c 'import time; print(time.time_ns() // 1000000)'
}

duration_log() {
  local started="$1"
  local step="$2"
  local finished
  finished="$(now_ms)"
  echo "duration_ms=$((finished - started)) step=$step"
}

echo "config dry_run=$DRY_RUN delete_mode=$DELETE_MODE ignore_existing=$IGNORE_EXISTING skip_rebuild=$SKIP_REBUILD force=$FORCE"

mkdir -p "$LOCAL_STATE" "$STAGING_DIR"

TOTAL_STARTED="$(now_ms)"

STEP_STARTED="$(now_ms)"
echo "[1/7] Baixando media_refs.txt da VPS..."
scp "$REMOTE:$REMOTE_BASE/state/media_refs.txt" "$REFS_FILE"
scp "$REMOTE:$REMOTE_BASE/state/snapshot_status.json" "$SNAPSHOT_STATUS_FILE" >/dev/null 2>&1 || true
duration_log "$STEP_STARTED" "read_refs"

STEP_STARTED="$(now_ms)"
echo "[2/7] Comparando manifesto local..."
COMPARE_JSON="$(
  ANKI_GPT_FORCE="$FORCE" \
  ANKI_GPT_REFS_FILE="$REFS_FILE" \
  ANKI_GPT_SNAPSHOT_STATUS_FILE="$SNAPSHOT_STATUS_FILE" \
  ANKI_GPT_CHANGED_LIST="$CHANGED_LIST" \
  ANKI_GPT_COMPARE_FILE="$COMPARE_FILE" \
  ANKI_GPT_MANIFEST_FILE="$MANIFEST_FILE" \
  ANKI_GPT_PUBLISH_STATE_FILE="$PUBLISH_STATE_FILE" \
  ANKI_GPT_MEDIA_DIR="$MEDIA_DIR" \
  python3 - <<'PY'
import json
import hashlib
import os
from pathlib import Path

force = os.environ.get("ANKI_GPT_FORCE") == "1"
refs_file = Path(os.environ["ANKI_GPT_REFS_FILE"])
snapshot_status_file = Path(os.environ["ANKI_GPT_SNAPSHOT_STATUS_FILE"])
changed_list = Path(os.environ["ANKI_GPT_CHANGED_LIST"])
compare_file = Path(os.environ["ANKI_GPT_COMPARE_FILE"])
manifest_file = Path(os.environ["ANKI_GPT_MANIFEST_FILE"])
state_file = Path(os.environ["ANKI_GPT_PUBLISH_STATE_FILE"])
media_dir = Path(os.environ["ANKI_GPT_MEDIA_DIR"])
snapshot_hash = os.environ.get("ANKI_GPT_SNAPSHOT_HASH", "").strip()

def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

old_manifest = load_json(manifest_file)
if not isinstance(old_manifest, dict):
    old_manifest = {}
old_files = old_manifest.get("files", {})
if not isinstance(old_files, dict):
    old_files = {}

publish_state = load_json(state_file)
if not isinstance(publish_state, dict):
    publish_state = {}
if not snapshot_hash:
    snapshot_status = load_json(snapshot_status_file)
    if snapshot_status:
        encoded = json.dumps(snapshot_status, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        snapshot_hash = "remote-status:" + hashlib.sha256(encoded).hexdigest()
previous_snapshot_hash = str(publish_state.get("snapshot_hash", "") or "")
snapshot_changed = bool(snapshot_hash and snapshot_hash != previous_snapshot_hash)
if not snapshot_hash:
    snapshot_changed = True

refs = [line.strip() for line in refs_file.read_text(encoding="utf-8").splitlines() if line.strip()]
changed = []
unchanged = 0
missing = 0
external_skipped = 0
local_refs = 0
new_files = {}

for ref in refs:
    if ref.startswith(("http://", "https://")):
        external_skipped += 1
        continue
    if ref.startswith("/") or ".." in Path(ref).parts:
        missing += 1
        continue

    local_refs += 1
    src = media_dir / ref
    if not src.exists() or not src.is_file():
        missing += 1
        continue

    st = src.stat()
    entry = {
        "path": ref,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
    }
    new_files[ref] = entry
    old_entry = old_files.get(ref)
    if force or old_entry != entry:
        changed.append(ref)
    else:
        unchanged += 1

changed_list.parent.mkdir(parents=True, exist_ok=True)
with changed_list.open("wb") as f:
    for ref in changed:
        f.write(ref.encode("utf-8"))
        f.write(b"\0")

summary = {
    "refs_total": len(refs),
    "local_refs": local_refs,
    "external_skipped": external_skipped,
    "missing": missing,
    "changed_files": len(changed),
    "unchanged_files": unchanged,
    "media_changed": bool(changed),
    "snapshot_changed": snapshot_changed,
    "manifest": {
        "version": 1,
        "files": new_files,
    },
    "publish_state": {
        "version": 1,
        "snapshot_hash": snapshot_hash,
    },
}
compare_file.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
print(json.dumps({k: v for k, v in summary.items() if k not in {"manifest", "publish_state"}}, ensure_ascii=False))
PY
)"
duration_log "$STEP_STARTED" "compare_manifest"

refs_total="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["refs_total"])' "$COMPARE_FILE")"
local_refs="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["local_refs"])' "$COMPARE_FILE")"
external_skipped="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["external_skipped"])' "$COMPARE_FILE")"
missing="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["missing"])' "$COMPARE_FILE")"
changed_files="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["changed_files"])' "$COMPARE_FILE")"
unchanged_files="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["unchanged_files"])' "$COMPARE_FILE")"
media_changed="$(python3 -c 'import json,sys; print(str(json.load(open(sys.argv[1], encoding="utf-8"))["media_changed"]).lower())' "$COMPARE_FILE")"
snapshot_changed="$(python3 -c 'import json,sys; print(str(json.load(open(sys.argv[1], encoding="utf-8"))["snapshot_changed"]).lower())' "$COMPARE_FILE")"

echo "refs_total=$refs_total"
echo "local_refs=$local_refs"
echo "external_skipped=$external_skipped"
echo "missing=$missing"
echo "changed_files=$changed_files"
echo "unchanged_files=$unchanged_files"
echo "media_changed=$media_changed"
echo "snapshot_changed=$snapshot_changed"

if [[ "$missing" != "0" ]]; then
  echo "missing media detected; continuing without delete"
fi

uploaded_files=0
if [[ "$changed_files" != "0" ]]; then
  STEP_STARTED="$(now_ms)"
  echo "[3/7] Enviando mídias novas/alteradas para a VPS..."
  RSYNC_ARGS=(-av --progress --from0 --files-from="$CHANGED_LIST")

  if (( DRY_RUN )); then
    RSYNC_ARGS=(-avvn --progress --from0 --files-from="$CHANGED_LIST")
  fi

  if (( DELETE_MODE )); then
    RSYNC_ARGS+=(--delete)
  fi

  if (( IGNORE_EXISTING )); then
    RSYNC_ARGS+=(--ignore-existing)
  fi

  printf 'rsync command: rsync'
  for arg in "${RSYNC_ARGS[@]}"; do
    printf ' %q' "$arg"
  done
  printf ' %q %q\n' "$MEDIA_DIR"/ "$REMOTE:$REMOTE_BASE/state/media/"

  rsync "${RSYNC_ARGS[@]}" "$MEDIA_DIR"/ "$REMOTE:$REMOTE_BASE/state/media/"
  uploaded_files="$changed_files"
  duration_log "$STEP_STARTED" "rsync"
else
  echo "[3/7] Nenhuma mídia local nova/alterada; rsync pulado."
  echo "duration_ms=0 step=rsync"
fi
echo "uploaded_files=$uploaded_files"

if (( DRY_RUN )); then
  echo "[4/7] Dry-run concluido; manifesto e rebuild nao foram atualizados."
  echo "duration_ms=0 step=rebuild_remote"
  echo "duration_ms=0 step=smoke_test"
  duration_log "$TOTAL_STARTED" "publish_total"
  exit 0
fi

notes_with_broken_media=""
total_broken_refs=""
rebuild_ran=0

if (( SKIP_REBUILD )); then
  echo "[4/7] Rebuild remoto pulado por flag."
  echo "duration_ms=0 step=rebuild_remote"
elif [[ "$media_changed" == "true" || "$snapshot_changed" == "true" ]]; then
  rebuild_ran=1
  STEP_STARTED="$(now_ms)"
  echo "[4/7] Rebuildando índices na VPS..."
  set +e
  REBUILD_OUTPUT="$(ssh "$REMOTE" "$REMOTE_BASE/scripts/rebuild_state.sh" 2>&1)"
  REBUILD_RC=$?
  set -e
  printf '%s\n' "$REBUILD_OUTPUT"
  duration_log "$STEP_STARTED" "rebuild_remote"
  if (( REBUILD_RC != 0 )); then
    echo "rebuild_failed exit=$REBUILD_RC" >&2
    exit "$REBUILD_RC"
  fi
  notes_with_broken_media="$(printf '%s\n' "$REBUILD_OUTPUT" | awk -F' = ' '/notes_with_broken_media/ {v=$2} END {print v}')"
  total_broken_refs="$(printf '%s\n' "$REBUILD_OUTPUT" | awk -F' = ' '/total_broken_refs/ {v=$2} END {print v}')"
else
  echo "[4/7] Snapshot e mídia sem alteração; rebuild remoto pulado."
  echo "duration_ms=0 step=rebuild_remote"
fi

if (( rebuild_ran )); then
  STEP_STARTED="$(now_ms)"
  echo "[5/7] Testando API autenticada..."
  ANKI_GPT_TOKEN_FILE="${ANKI_GPT_TOKEN_FILE:-$LOCAL_BASE/tagging_token.txt}" \
  python3 - <<'PY'
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

token = os.environ.get("ANKI_GPT_TAGGING_TOKEN", "").strip()
if not token:
    token_file = Path(os.environ["ANKI_GPT_TOKEN_FILE"])
    try:
        raw_token = token_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"authenticated smoke test failed: token unavailable ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1)
    lines = raw_token.splitlines()
    token = raw_token.strip()
    if len(lines) != 1 or not token or any(char.isspace() for char in token):
        print("authenticated smoke test failed: token file must contain exactly one token", file=sys.stderr)
        raise SystemExit(1)

request = Request(
    "https://gatsby-anki.137.131.191.66.nip.io/decks?limit=1",
    headers={"X-Tagging-Token": token},
    method="GET",
)
try:
    with urlopen(request, timeout=30) as response:
        status = response.status
except HTTPError as exc:
    print(f"authenticated smoke test failed: HTTP {exc.code}", file=sys.stderr)
    raise SystemExit(1)
except (OSError, URLError) as exc:
    print(f"authenticated smoke test failed: {type(exc).__name__}", file=sys.stderr)
    raise SystemExit(1)

if status != 200:
    print(f"authenticated smoke test failed: HTTP {status}", file=sys.stderr)
    raise SystemExit(1)
print("authenticated smoke test ok status=200")
PY
  duration_log "$STEP_STARTED" "smoke_test"
else
  echo "[5/7] Smoke test pulado porque rebuild nao rodou."
  echo "duration_ms=0 step=smoke_test"
fi

STEP_STARTED="$(now_ms)"
echo "[6/7] Atualizando manifesto local..."
COMPARE_FILE="$COMPARE_FILE" \
MANIFEST_FILE="$MANIFEST_FILE" \
PUBLISH_STATE_FILE="$PUBLISH_STATE_FILE" \
python3 - <<'PY'
import json
import os
from pathlib import Path

summary = json.loads(Path(os.environ["COMPARE_FILE"]).read_text(encoding="utf-8"))
manifest_file = Path(os.environ["MANIFEST_FILE"])
state_file = Path(os.environ["PUBLISH_STATE_FILE"])
manifest_file.parent.mkdir(parents=True, exist_ok=True)
state_file.parent.mkdir(parents=True, exist_ok=True)

tmp_manifest = manifest_file.with_suffix(manifest_file.suffix + ".tmp")
tmp_manifest.write_text(json.dumps(summary["manifest"], ensure_ascii=False, indent=2), encoding="utf-8")
tmp_manifest.replace(manifest_file)

publish_state = summary["publish_state"]
publish_state["last_success_at"] = __import__("datetime").datetime.now().astimezone().isoformat()
tmp_state = state_file.with_suffix(state_file.suffix + ".tmp")
tmp_state.write_text(json.dumps(publish_state, ensure_ascii=False, indent=2), encoding="utf-8")
tmp_state.replace(state_file)
PY
duration_log "$STEP_STARTED" "update_manifest"

if [[ -n "$notes_with_broken_media" ]]; then
  echo "notes_with_broken_media=$notes_with_broken_media"
fi
if [[ -n "$total_broken_refs" ]]; then
  echo "total_broken_refs=$total_broken_refs"
fi

echo "[7/7] Publicação concluída."
duration_log "$TOTAL_STARTED" "publish_total"
