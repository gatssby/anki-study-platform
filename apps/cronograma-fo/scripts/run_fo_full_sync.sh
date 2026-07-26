#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/cronograma-fo}"
MATERIALS_DIR="${MATERIALS_DIR:-/home/ubuntu/anki-gpt-sync}"
DB_PATH="${DB_PATH:-$PROJECT_DIR/data/cronograma.db}"
STATE_DIR="${STATE_DIR:-$PROJECT_DIR/state/federal_online}"
CRONOGRAMA_STATE_DIR="$STATE_DIR/cronograma_fo"
LOG_DIR="$STATE_DIR/logs"
LOCK_FILE="$CRONOGRAMA_STATE_DIR/fo_full_sync.lock"
HTTP_URL="${HTTP_URL:-http://127.0.0.1:18000/}"
CONTAINER_NAME="${CONTAINER_NAME:-cronograma-fo}"
INDEX_JSON="$CRONOGRAMA_STATE_DIR/aulas_index.filtered_aulas_gerais.json"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="$LOG_DIR/fo_full_sync_${RUN_ID}.log"
LATEST_LOG="$LOG_DIR/fo_full_sync.latest.log"
BACKUP_DIR="$PROJECT_DIR/data/backups"
DRY_RUN=0
REFRESH_INDEX=0
MAX_INDEX_AGE_HOURS="${MAX_INDEX_AGE_HOURS:-168}"
INDEX_TIMEOUT="${INDEX_TIMEOUT:-90m}"
MERGE_AMBIGUOUS=""

usage() {
  cat <<'EOF'
Uso:
  scripts/run_fo_full_sync.sh [--dry-run] [--refresh-index] [--max-index-age-hours N]

Por padrao, usa o indice promovido em:
  state/federal_online/cronograma_fo/aulas_index.filtered_aulas_gerais.json

Sem --dry-run, coleta indice de aulas gerais somente se --refresh-index for usado,
se o indice promovido nao existir, ou se ele estiver mais velho que
--max-index-age-hours. Depois aplica merge de metadados FO, reaplica o cronograma
adaptativo, valida o banco e chama os syncs existentes de PDFs e videos.

Com --dry-run, roda auditorias, merge dry-run, adaptive dry-run, validacoes e
healthchecks. Nao coleta indice por padrao, nao aplica merge, nao aplica
cronograma adaptativo e nao executa sync real de PDFs; videos sao chamados com
--dry-run porque o script suporta.
EOF
}

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

die() {
  log "ERRO: $*"
  exit 1
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run)
        DRY_RUN=1
        ;;
      --refresh-index)
        REFRESH_INDEX=1
        ;;
      --max-index-age-hours)
        [ "${2:-}" ] || die "--max-index-age-hours exige um valor"
        MAX_INDEX_AGE_HOURS="$2"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Argumento desconhecido: $1"
        ;;
    esac
    shift
  done
}

setup_logging() {
  mkdir -p "$LOG_DIR" "$CRONOGRAMA_STATE_DIR"
  touch "$LOG_FILE"
  ln -sfn "$LOG_FILE" "$LATEST_LOG"
  exec > >(tee -a "$LOG_FILE") 2>&1
}

setup_lock() {
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "lock ocupado: $LOCK_FILE"
    if command -v fuser >/dev/null 2>&1; then
      fuser -v "$LOCK_FILE" || true
    fi
    die "Outro run_fo_full_sync.sh parece estar em execucao; lock nao foi removido automaticamente"
  fi
  log "lock adquirido: $LOCK_FILE"
}

on_exit() {
  local status=$?
  log "fim run_id=$RUN_ID exit_code=$status log=$LOG_FILE"
}

quote_cmd() {
  local quoted=()
  local arg
  for arg in "$@"; do
    quoted+=("$(printf '%q' "$arg")")
  done
  printf '%s' "${quoted[*]}"
}

run_step() {
  local name="$1"
  shift
  local start
  local status
  start="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  log "inicio etapa=$name start=$start cmd=$(quote_cmd "$@")"
  set +e
  "$@"
  status=$?
  set -e
  log "fim etapa=$name exit_code=$status"
  return "$status"
}

require_file() {
  local path="$1"
  local label="$2"
  [ -f "$path" ] || die "$label nao encontrado: $path"
}

audit_environment() {
  log "auditoria ambiente"
  log "hostname=$(hostname)"
  log "project_dir=$PROJECT_DIR"
  log "materials_dir=$MATERIALS_DIR"
  log "db_path=$DB_PATH"
  log "dry_run=$DRY_RUN"
  log "refresh_index=$REFRESH_INDEX"
  log "max_index_age_hours=$MAX_INDEX_AGE_HOURS"
  log "index_timeout=$INDEX_TIMEOUT"

  log "crontab atual:"
  crontab -l || true

  local required_paths=(
    "$PROJECT_DIR/scripts/fo_collect_aulas_index.py"
    "$PROJECT_DIR/scripts/fo_merge_aulas_metadata.py"
    "$PROJECT_DIR/scripts/generate_adaptive_schedule.py"
    "$PROJECT_DIR/scripts/sqlite_safe_backup.py"
    "$PROJECT_DIR/scripts/run_fo_video_upload_weekly.sh"
    "$PROJECT_DIR/scripts/fo_download_upload_videos.py"
    "$MATERIALS_DIR/scripts/run_fo_daily.sh"
    "$MATERIALS_DIR/scripts/fo_sync_materials_from_portal_materiais.py"
  )
  local path
  for path in "${required_paths[@]}"; do
    if [ -e "$path" ]; then
      log "ok path=$path"
    else
      die "arquivo obrigatorio ausente: $path"
    fi
  done

  if [ -e "$PROJECT_DIR/scripts/run_fo_weekly.sh" ]; then
    log "ok path=$PROJECT_DIR/scripts/run_fo_weekly.sh"
  else
    log "warn: crontab pode apontar para script ausente: $PROJECT_DIR/scripts/run_fo_weekly.sh"
  fi

  require_file "$DB_PATH" "Banco cronograma"

  python3 - "$MAX_INDEX_AGE_HOURS" <<'PY'
import sys

try:
    value = float(sys.argv[1])
except ValueError:
    raise SystemExit("--max-index-age-hours deve ser numerico")
if value < 0:
    raise SystemExit("--max-index-age-hours nao pode ser negativo")
PY
}

backup_db() {
  local label="$1"
  local timestamp
  local backup_path
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$BACKUP_DIR"
  backup_path="$BACKUP_DIR/cronograma-${label}-${timestamp}-$$.db"
  run_step "backup_db_${label}" \
    python3 "$PROJECT_DIR/scripts/sqlite_safe_backup.py" backup \
      --source "$DB_PATH" \
      --output "$backup_path"
  log "backup_db label=$label path=$backup_path"
}

index_is_valid() {
  python3 - "$INDEX_JSON" <<'PY' >/dev/null 2>&1
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(1)
payload = json.loads(path.read_text(encoding="utf-8"))
aulas = payload.get("aulas")
if not isinstance(aulas, list) or not aulas:
    raise SystemExit(1)
PY
}

index_age_hours() {
  python3 - "$INDEX_JSON" <<'PY'
import time
import sys
from pathlib import Path

path = Path(sys.argv[1])
age = (time.time() - path.stat().st_mtime) / 3600
print(f"{age:.2f}")
PY
}

index_is_older_than_limit() {
  python3 - "$INDEX_JSON" "$MAX_INDEX_AGE_HOURS" <<'PY' >/dev/null 2>&1
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
limit = float(sys.argv[2])
age = (time.time() - path.stat().st_mtime) / 3600
raise SystemExit(0 if age > limit else 1)
PY
}

collect_index() {
  local reason=""
  local age_hours=""

  if index_is_valid; then
    age_hours="$(index_age_hours)"
    log "indice_promovido ok path=$INDEX_JSON age_hours=$age_hours"
  else
    log "indice_promovido ausente_ou_invalido path=$INDEX_JSON"
    reason="missing_or_invalid"
  fi

  if [ -z "$reason" ] && [ "$REFRESH_INDEX" -eq 1 ]; then
    reason="refresh_index"
  fi

  if [ -z "$reason" ] && index_is_older_than_limit; then
    reason="stale"
  fi

  if [ -z "$reason" ]; then
    log "indice_promovido sera reutilizado; coleta nao necessaria"
    return 0
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    log "dry-run: coleta real do portal nao sera executada reason=$reason"
    log "dry-run: seria executado: timeout $INDEX_TIMEOUT python3 scripts/fo_collect_aulas_index.py --no-resume --path-filter 'Aulas Gerais'"
    require_file "$INDEX_JSON" "Indice filtrado existente para dry-run"
    index_is_valid || die "Indice filtrado existente e invalido: $INDEX_JSON"
    return 0
  fi

  command -v timeout >/dev/null 2>&1 || die "Comando obrigatorio ausente para coleta controlada: timeout"
  log "coleta_indice iniciando reason=$reason timeout=$INDEX_TIMEOUT checkpoint_parcial_ignorado=$CRONOGRAMA_STATE_DIR/checkpoint.filtered_aulas_gerais.json"
  set +e
  (
    cd "$PROJECT_DIR"
    timeout "$INDEX_TIMEOUT" python3 scripts/fo_collect_aulas_index.py --no-resume --path-filter "Aulas Gerais"
  )
  local status=$?
  set -e

  if [ "$status" -eq 0 ] && index_is_valid; then
    log "coleta_indice ok; usando indice promovido path=$INDEX_JSON age_hours=$(index_age_hours)"
    return 0
  fi

  log "coleta_indice falhou_ou_nao_promoveu status=$status; checkpoint parcial nao sera usado"
  if index_is_valid; then
    log "fallback: usando indice promovido anterior valido path=$INDEX_JSON age_hours=$(index_age_hours)"
    return 0
  fi

  die "Coleta do indice falhou e nao existe indice promovido valido para fallback"
}

run_merge_dry_run() {
  local output_file="$CRONOGRAMA_STATE_DIR/fo_full_sync_merge_dry_run_${RUN_ID}.out"
  log "inicio etapa=merge_dry_run cmd=python3 scripts/fo_merge_aulas_metadata.py --index-json $INDEX_JSON"
  set +e
  (
    cd "$PROJECT_DIR"
    python3 scripts/fo_merge_aulas_metadata.py --index-json "$INDEX_JSON"
  ) 2>&1 | tee "$output_file"
  local status=${PIPESTATUS[0]}
  set -e
  log "fim etapa=merge_dry_run exit_code=$status output=$output_file"
  [ "$status" -eq 0 ] || return "$status"

  MERGE_AMBIGUOUS="$(
    awk -F': *' '/^[[:space:]]*ambiguous:/ {print $2}' "$output_file" | tail -1
  )"
  MERGE_AMBIGUOUS="${MERGE_AMBIGUOUS:-0}"
  case "$MERGE_AMBIGUOUS" in
    ''|*[!0-9]*)
      die "Nao foi possivel interpretar ambiguous no dry-run do merge: '$MERGE_AMBIGUOUS'"
      ;;
  esac
  log "merge_dry_run ambiguous=$MERGE_AMBIGUOUS"
  if [ "$MERGE_AMBIGUOUS" -ne 0 ]; then
    die "Merge FO abortado porque ambiguous=$MERGE_AMBIGUOUS"
  fi
}

run_merge_apply_if_allowed() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log "dry-run: merge --apply nao sera executado"
    return 0
  fi

  [ "${MERGE_AMBIGUOUS:-}" = "0" ] || die "merge --apply exige ambiguous=0; atual=${MERGE_AMBIGUOUS:-indefinido}"
  backup_db "pre-fo-merge"
  (
    cd "$PROJECT_DIR"
    python3 scripts/fo_merge_aulas_metadata.py --index-json "$INDEX_JSON" --apply
  )
}

run_adaptive_schedule() {
  if [ "$DRY_RUN" -eq 1 ]; then
    (
      cd "$PROJECT_DIR"
      python3 scripts/generate_adaptive_schedule.py --dry-run
    )
    return 0
  fi

  backup_db "pre-adaptive"
  (
    cd "$PROJECT_DIR"
    python3 scripts/generate_adaptive_schedule.py --apply
  )
}

validate_schedule() {
  python3 - "$DB_PATH" "$DRY_RUN" "$PROJECT_DIR" <<'PY'
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

db_path = sys.argv[1]
dry_run = sys.argv[2] == "1"
project_dir = sys.argv[3]
strict = not dry_run
sp_tz = ZoneInfo("America/Sao_Paulo")
sys.path.insert(0, project_dir)

from scripts.validate_fo_daily_assignments import validate_daily_assignments


def seen_at_local_date(seen_at: str | None) -> str | None:
    normalized = str(seen_at or "").strip().replace("Z", "+00:00")
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(normalized.replace(" ", "T", 1))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(sp_tz).date().isoformat()


def fail(message: str) -> None:
    print(f"validation_error={message}")
    if strict:
        raise SystemExit(message)


print(f"validation_mode={'dry-run-advisory' if dry_run else 'strict'}")
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
cur = conn.cursor()

assignment_report = validate_daily_assignments(conn)
print(f"daily_assignment_total={assignment_report.total_count}")
print(f"daily_assignment_exact={assignment_report.exact_count}")
print(f"daily_assignment_substitution={assignment_report.substitution_count}")
print(
    "daily_assignment_compatible_duplicate="
    f"{assignment_report.compatible_duplicate_count}"
)
print(f"daily_assignment_contract_errors={len(assignment_report.errors)}")
for error in assignment_report.errors:
    print(f"daily_assignment_error={error}")
if assignment_report.errors:
    fail(f"daily_assignment_contract_errors={len(assignment_report.errors)}")

cur.execute("""
SELECT lesson_code, recommended_date, seen_at
FROM lessons
WHERE track_code = 'FO'
  AND lesson_type = 'lesson'
  AND is_seen = 1
  AND seen_at IS NOT NULL
  AND seen_at != ''
""")
seen_mismatches = []
for lesson_code, recommended_date, seen_at in cur.fetchall():
    expected_date = seen_at_local_date(seen_at)
    if expected_date is None or recommended_date != expected_date:
        seen_mismatches.append((lesson_code, recommended_date, seen_at, expected_date))
print(f"seen_mismatch_local_sp={len(seen_mismatches)}")
for lesson_code, recommended_date, seen_at, expected_date in seen_mismatches[:20]:
    print(
        f"seen_mismatch lesson_code={lesson_code} recommended_date={recommended_date} "
        f"seen_at={seen_at} expected={expected_date}"
    )
if seen_mismatches:
    fail("seen_mismatch_local_sp != 0")

cur.execute("""
SELECT
  SUM(CASE WHEN lessons.is_seen = 1 AND exercise_tasks.is_active = 0 THEN 1 ELSE 0 END),
  SUM(CASE WHEN lessons.is_seen = 0 AND exercise_tasks.is_active = 1 THEN 1 ELSE 0 END)
FROM exercise_tasks
JOIN lessons ON lessons.lesson_code = exercise_tasks.source_lesson_code
WHERE lessons.track_code = 'FO'
  AND lessons.lesson_type = 'lesson'
""")
active_seen_bad, active_unseen_bad = cur.fetchone()
active_seen_bad = int(active_seen_bad or 0)
active_unseen_bad = int(active_unseen_bad or 0)
print(f"active_seen_bad={active_seen_bad}")
print(f"active_unseen_bad={active_unseen_bad}")
if active_seen_bad != 0:
    fail("active_seen_bad != 0")
if active_unseen_bad != 0:
    fail("active_unseen_bad != 0")
PY
}

sync_pdfs() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log "dry-run: /home/ubuntu/anki-gpt-sync/scripts/run_fo_daily.sh nao suporta dry-run; sync real de PDFs seria executado aqui"
    return 0
  fi

  "$MATERIALS_DIR/scripts/run_fo_daily.sh"
}

sync_videos() {
  if [ "$DRY_RUN" -eq 1 ]; then
    "$PROJECT_DIR/scripts/run_fo_video_upload_weekly.sh" --dry-run
    return 0
  fi

  "$PROJECT_DIR/scripts/run_fo_video_upload_weekly.sh"
}

healthcheck() {
  docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" | grep -qx 'true'
  curl -fsSIL --max-time 5 "$HTTP_URL" >/dev/null
  systemctl is-active --quiet fo-queue-api.service
  systemctl is-active --quiet fo-vps-worker.service
  log "healthcheck ok container=$CONTAINER_NAME http=$HTTP_URL services=fo-queue-api.service,fo-vps-worker.service"
}

main() {
  parse_args "$@"
  setup_logging
  trap on_exit EXIT
  setup_lock

  log "inicio run_id=$RUN_ID script=$0"
  run_step "auditoria" audit_environment
  run_step "coletar_indice_aulas_gerais" collect_index
  run_step "merge_dry_run" run_merge_dry_run
  run_step "merge_apply" run_merge_apply_if_allowed
  run_step "adaptive_schedule" run_adaptive_schedule
  run_step "validar_cronograma" validate_schedule
  run_step "sync_pdfs" sync_pdfs
  run_step "sync_videos" sync_videos
  run_step "healthcheck" healthcheck
  log "run_fo_full_sync concluido com sucesso"
}

main "$@"
