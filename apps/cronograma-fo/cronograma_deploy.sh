#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_PATH="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "${BASH_SOURCE[0]}")"
PROJECT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

REMOTE_HOST="${REMOTE_HOST:-oracle-vps}"
REMOTE_DIR="${REMOTE_DIR:-/opt/cronograma-fo}"
REMOTE_DB="${REMOTE_DB:-$REMOTE_DIR/data/cronograma.db}"
REMOTE_BACKUP_DIR="${REMOTE_BACKUP_DIR:-$REMOTE_DIR/data/backups/manual}"
LOCAL_DB="${LOCAL_DB:-$PROJECT_DIR/data/cronograma.db}"
REMOTE_DEPLOY_SCRIPT="$PROJECT_DIR/scripts/remote_deploy_live.sh"
SQLITE_BACKUP_TOOL="$PROJECT_DIR/scripts/sqlite_safe_backup.py"
DB_COMPARE_TOOL="$PROJECT_DIR/scripts/compare_sqlite_state.py"
REMOTE_SQLITE_BACKUP_TOOL="$REMOTE_DIR/scripts/sqlite_safe_backup.py"
REMOTE_DB_COMPARE_TOOL="$REMOTE_DIR/scripts/compare_sqlite_state.py"
REMOTE_UPLOADS_ROOT="${REMOTE_UPLOADS_ROOT:-$REMOTE_DIR/data}"
REMOTE_APP_URL="${REMOTE_APP_URL:-http://127.0.0.1:18000/database?track=FO}"
COMPOSE_SERVICE="${COMPOSE_SERVICE:-cronograma-fo}"

DRY_RUN=0
WITH_DB=0
FORCE_DB_OVERWRITE=0
PULL_DB=0
RESTART=1
RESTART_ONLY=0
BACKUP_CREATED=0
DB_COMPARE_LOSS=0
REMOTE_DB_SNAPSHOT=""
REMOTE_DB_CANDIDATE=""
REMOTE_DB_REPORT=""

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)

usage() {
  cat <<'EOF'
Uso:
  cronograma_deploy [opções]

Opções:
  --dry-run       Mostra o que seria enviado por rsync, sem alterar remoto.
  --with-db       Também envia data/cronograma.db, mas só após comparação exata do
                  estado protegido, snapshot remoto e confirmação DEPLOY_DB.
  --force-db-overwrite
                  Emergência: permite sobrescrever mesmo se o banco local parecer
                  mais pobre. Exige confirmação FORCE_DEPLOY_DB_LOSS.
  --pull-db       Baixa o banco remoto para data/cronograma.db, salvando backup local antes.
  --no-restart    Faz deploy sem rebuild/recreate do app remoto.
  --restart-only  Apenas rebuilda/recria o app remoto e roda validação básica.
  -h, --help      Mostra esta ajuda.

Padrão:
  Deploy de código, templates, scripts e configuração.
  O banco local data/cronograma.db NÃO é enviado por padrão.

Banco:
  --pull-db sincroniza o banco local com produção de forma segura.
  --with-db só deve ser usado depois de --pull-db e validação local.
  --force-db-overwrite é apenas para emergência com perda intencional.

Variáveis opcionais:
  REMOTE_HOST=oracle-vps
  REMOTE_DIR=/opt/cronograma-fo
  LOCAL_DB=/caminho/local/cronograma.db
EOF
}

log() {
  printf '\n==> %s\n' "$*"
}

warn() {
  printf '\n[AVISO] %s\n' "$*" >&2
}

die() {
  printf '\n[ERRO] %s\n' "$*" >&2
  exit 1
}

require_command() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || die "Comando local obrigatório não encontrado: $cmd"
}

shell_quote_join() {
  local quoted=()
  local arg
  for arg in "$@"; do
    quoted+=("$(printf '%q' "$arg")")
  done
  printf '%s' "${quoted[*]}"
}

run_ssh() {
  local command_string
  command_string="$(shell_quote_join "$@")"
  ssh "${SSH_OPTS[@]}" "$REMOTE_HOST" "$command_string"
}

run_remote_shell() {
  local script="$1"
  local command_string
  command_string="$(shell_quote_join bash -lc "$script")"
  ssh "${SSH_OPTS[@]}" "$REMOTE_HOST" "$command_string"
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run)
        DRY_RUN=1
        ;;
      --with-db)
        WITH_DB=1
        ;;
      --force-db-overwrite)
        FORCE_DB_OVERWRITE=1
        ;;
      --pull-db)
        PULL_DB=1
        ;;
      --no-restart)
        RESTART=0
        ;;
      --restart-only)
        RESTART_ONLY=1
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

  if [ "$RESTART_ONLY" -eq 1 ] && [ "$WITH_DB" -eq 1 ]; then
    die "--restart-only não pode ser combinado com --with-db"
  fi
  if [ "$RESTART_ONLY" -eq 1 ] && [ "$PULL_DB" -eq 1 ]; then
    die "--restart-only não pode ser combinado com --pull-db"
  fi
  if [ "$RESTART_ONLY" -eq 1 ] && [ "$DRY_RUN" -eq 1 ]; then
    die "--restart-only não pode ser combinado com --dry-run"
  fi
  if [ "$PULL_DB" -eq 1 ] && [ "$WITH_DB" -eq 1 ]; then
    die "--pull-db não pode ser combinado com --with-db"
  fi
  if [ "$PULL_DB" -eq 1 ] && [ "$DRY_RUN" -eq 1 ]; then
    die "--pull-db não pode ser combinado com --dry-run"
  fi
  if [ "$FORCE_DB_OVERWRITE" -eq 1 ] && [ "$WITH_DB" -eq 0 ]; then
    die "--force-db-overwrite só pode ser usado junto com --with-db"
  fi
  if [ "$WITH_DB" -eq 1 ] && [ "$RESTART" -eq 0 ]; then
    die "--with-db exige restart para validar a aplicação e permitir rollback automático"
  fi
}

check_local_prerequisites() {
  log "Preflight local"
  require_command ssh
  require_command rsync
  require_command python3

  [ -d "$PROJECT_DIR/app" ] || die "Diretório app/ não encontrado em $PROJECT_DIR"
  [ -f "$LOCAL_DB" ] || die "Banco local não encontrado: $LOCAL_DB"
  [ -f "$REMOTE_DEPLOY_SCRIPT" ] || die "Script remoto local não encontrado: $REMOTE_DEPLOY_SCRIPT"
  [ -f "$SQLITE_BACKUP_TOOL" ] || die "Utilitário de snapshot não encontrado: $SQLITE_BACKUP_TOOL"
  [ -f "$DB_COMPARE_TOOL" ] || die "Comparador de estado não encontrado: $DB_COMPARE_TOOL"

  if [ "$(cd "$PROJECT_DIR" && pwd)" != "/Users/gatsby/Workspace/Anki Study Platform/apps/cronograma-fo" ]; then
    warn "Raiz detectada diferente do caminho esperado: $PROJECT_DIR"
  fi

  printf 'Projeto local: %s\n' "$PROJECT_DIR"
  printf 'Banco local:   %s (não enviado por padrão)\n' "$LOCAL_DB"
}

compile_python_files() {
  log "Checando sintaxe Python local"
  local files=()

  if [ -d "$PROJECT_DIR/app" ]; then
    while IFS= read -r file; do
      files+=("$file")
    done < <(find "$PROJECT_DIR/app" -type f -name '*.py' | sort)
  fi

  [ -f "$PROJECT_DIR/main.py" ] && files+=("$PROJECT_DIR/main.py")
  [ -f "$PROJECT_DIR/scripts/build_un_gpe_csv.py" ] && files+=("$PROJECT_DIR/scripts/build_un_gpe_csv.py")
  [ -f "$PROJECT_DIR/scripts/check_un_gpe_migration.py" ] && files+=("$PROJECT_DIR/scripts/check_un_gpe_migration.py")

  if [ "${#files[@]}" -eq 0 ]; then
    warn "Nenhum arquivo Python encontrado para compilar."
    return 0
  fi

  python3 -m py_compile "${files[@]}"
}

check_remote_readonly() {
  log "Preflight remoto"
  run_remote_shell "
    set -Eeuo pipefail
    test -d $(printf '%q' "$REMOTE_DIR")
    test -f $(printf '%q' "$REMOTE_DB")
    test -s $(printf '%q' "$REMOTE_DB")
    echo 'Remoto OK: $(printf '%q' "$REMOTE_DIR")'
    ls -lh $(printf '%q' "$REMOTE_DB")
  " || die "Preflight remoto falhou. Verifique SSH, $REMOTE_DIR e $REMOTE_DB."
}

ensure_remote_backup_dir() {
  run_ssh mkdir -p "$REMOTE_BACKUP_DIR"
}

print_rsync_plan() {
  cat <<EOF
Destino remoto:
  host: $REMOTE_HOST
  app:  $REMOTE_DIR
  db:   $REMOTE_DB

Política de banco:
  data/cronograma.db não será enviado neste modo.
  Qualquer *.db, *.sqlite e *.sqlite3 é excluído do rsync padrão.
EOF
}

rsync_project() {
  local opts=(-az --itemize-changes)

  if [ "$DRY_RUN" -eq 1 ]; then
    opts+=(--dry-run)
  fi

  opts+=(
    --exclude ".git/"
    --exclude "node_modules/"
    --exclude "__pycache__/"
    --exclude ".venv/"
    --exclude "venv/"
    --exclude "backups/"
    --exclude "state/"
    --exclude "output/"
    --exclude "work/"
    --exclude "imports/"
    --exclude "data/backups/"
    --exclude "data/cronograma.db"
    --exclude "*.pdf"
    --exclude "*.xlsx"
    --exclude "*.csv"
    --exclude "*.db"
    --exclude "*.sqlite"
    --exclude "*.sqlite3"
    --exclude "*.pyc"
    --exclude ".DS_Store"
    --exclude "*~"
    --exclude "*.tmp"
    --exclude "*.temp"
    --exclude ".pytest_cache/"
    --exclude ".mypy_cache/"
  )

  log "Rsync do projeto sem banco"
  print_rsync_plan
  rsync "${opts[@]}" "$PROJECT_DIR/" "$REMOTE_HOST:$REMOTE_DIR/"
}

confirm_db_deploy() {
  if [ "$DB_COMPARE_LOSS" -eq 1 ]; then
    cat <<EOF

ATENÇÃO: --force-db-overwrite está ativo e a comparação indicou perda provável
de histórico/cache ao sobrescrever o banco remoto.

Para continuar, digite exatamente: FORCE_DEPLOY_DB_LOSS
EOF
    local force_confirmation
    read -r force_confirmation
    [ "$force_confirmation" = "FORCE_DEPLOY_DB_LOSS" ] || die "Confirmação inválida. Banco remoto preservado."
    return 0
  fi

  cat <<EOF

ATENÇÃO: --with-db vai substituir o banco remoto:
  remoto: $REMOTE_DB
  local:  $LOCAL_DB

Antes da substituição, será criado backup remoto em:
  $REMOTE_DB_SNAPSHOT

Para continuar, digite exatamente: DEPLOY_DB
EOF
  local confirmation
  read -r confirmation
  [ "$confirmation" = "DEPLOY_DB" ] || die "Confirmação inválida. Banco remoto preservado."
}

collect_local_db_stats() {
  python3 - "$LOCAL_DB" <<'PY'
import sqlite3
import sys

db_path = sys.argv[1]
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
cur = conn.cursor()

for track_code, total, seen, cut in cur.execute("""
SELECT track_code, COUNT(*) total,
       SUM(CASE WHEN is_seen = 1 THEN 1 ELSE 0 END) seen,
       SUM(CASE WHEN is_cut = 1 THEN 1 ELSE 0 END) cut
FROM lessons
GROUP BY track_code
ORDER BY track_code
"""):
    print("LESSONS", track_code, total or 0, seen or 0, cut or 0, sep="\t")

for table in [
    "daily_assignments",
    "un_daily_assignments",
    "review_questions",
    "review_question_attempts",
]:
    exists = cur.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()[0]
    total = 0 if not exists else cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print("TABLE", table, total or 0, sep="\t")
PY
}

compare_db_state_before_upload() {
  local compare_status=0

  log "Validando banco local antes de --with-db"
  python3 "$SQLITE_BACKUP_TOOL" validate --db "$LOCAL_DB" \
    || die "Banco local inválido; nada foi enviado."

  REMOTE_DB_CANDIDATE="$REMOTE_DB.uploading.$$"
  REMOTE_DB_REPORT="$REMOTE_BACKUP_DIR/with-db-comparison-$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
  log "Enviando candidato para área temporária remota"
  rsync -az --itemize-changes "$LOCAL_DB" "$REMOTE_HOST:$REMOTE_DB_CANDIDATE"

  log "Validando candidato e comparando estado protegido com o snapshot remoto"
  set +e
  run_remote_shell "
    set -Eeuo pipefail
    python3 $(printf '%q' "$REMOTE_SQLITE_BACKUP_TOOL") validate --db $(printf '%q' "$REMOTE_DB_CANDIDATE")
    python3 $(printf '%q' "$REMOTE_DB_COMPARE_TOOL") \\
      --baseline-db $(printf '%q' "$REMOTE_DB_SNAPSHOT") \\
      --candidate-db $(printf '%q' "$REMOTE_DB_CANDIDATE") \\
      --uploads-root $(printf '%q' "$REMOTE_UPLOADS_ROOT") \\
      --report $(printf '%q' "$REMOTE_DB_REPORT")
  "
  compare_status=$?
  set -e

  if [ "$compare_status" -eq 10 ]; then
    DB_COMPARE_LOSS=1
    if [ "$FORCE_DB_OVERWRITE" -eq 0 ]; then
      cleanup_remote_candidate
      die "O relatório detectou divergência em estado protegido. Banco remoto preservado. Relatório: $REMOTE_DB_REPORT"
    fi
    warn "Estado protegido diverge. Override de emergência exigirá FORCE_DEPLOY_DB_LOSS. Relatório: $REMOTE_DB_REPORT"
    return 0
  fi
  if [ "$compare_status" -ne 0 ]; then
    cleanup_remote_candidate
    die "Falha ao validar ou comparar candidato. Banco remoto preservado."
  fi
  DB_COMPARE_LOSS=0
  printf 'Relatório de comparação: %s\n' "$REMOTE_DB_REPORT"
}

backup_remote_db() {
  local label="${1:-before-deploy}"
  local output
  output="$REMOTE_BACKUP_DIR/cronograma-${label}-$(date -u +%Y%m%dT%H%M%SZ)-$$.db"
  log "Criando snapshot remoto consistente do banco"
  run_remote_shell "
    set -Eeuo pipefail
    mkdir -p $(printf '%q' "$REMOTE_BACKUP_DIR")
    test -s $(printf '%q' "$REMOTE_DB")
    python3 $(printf '%q' "$REMOTE_SQLITE_BACKUP_TOOL") backup \\
      --source $(printf '%q' "$REMOTE_DB") \\
      --output $(printf '%q' "$output")
  "
  REMOTE_DB_SNAPSHOT="$output"
  BACKUP_CREATED=1
  printf 'Snapshot remoto: %s\n' "$REMOTE_DB_SNAPSHOT"
}

cleanup_remote_candidate() {
  if [ -n "$REMOTE_DB_CANDIDATE" ]; then
    run_ssh rm -f "$REMOTE_DB_CANDIDATE" || true
    REMOTE_DB_CANDIDATE=""
  fi
}

stop_remote_app_for_db_replace() {
  log "Parando o app antes da substituição do banco"
  run_remote_shell "
    set -Eeuo pipefail
    cd $(printf '%q' "$REMOTE_DIR")
    docker compose stop $(printf '%q' "$COMPOSE_SERVICE")
    running=\$(docker compose ps --status running --services)
    if printf '%s\n' \"\$running\" | grep -Fxq $(printf '%q' "$COMPOSE_SERVICE"); then
      echo 'service ainda ativo após docker compose stop' >&2
      exit 1
    fi
    python3 $(printf '%q' "$REMOTE_SQLITE_BACKUP_TOOL") assert-exclusive --db $(printf '%q' "$REMOTE_DB")
  "
}

restore_remote_db_from() {
  local source="$1"
  run_remote_shell "
    set -Eeuo pipefail
    python3 $(printf '%q' "$REMOTE_SQLITE_BACKUP_TOOL") validate --db $(printf '%q' "$source")
    python3 $(printf '%q' "$REMOTE_SQLITE_BACKUP_TOOL") restore \\
      --source $(printf '%q' "$source") \\
      --destination $(printf '%q' "$REMOTE_DB")
    chmod 664 $(printf '%q' "$REMOTE_DB")
  "
}

validate_remote_app_after_db_replace() {
  log "Validando container e resposta HTTP após substituição"
  run_remote_shell "
    set -Eeuo pipefail
    cd $(printf '%q' "$REMOTE_DIR")
    running=\$(docker compose ps --status running --services)
    printf '%s\n' \"\$running\" | grep -Fxq $(printf '%q' "$COMPOSE_SERVICE")
    curl --fail --silent --show-error --max-time 15 $(printf '%q' "$REMOTE_APP_URL") >/dev/null
  "
}

rollback_remote_db() {
  warn "Falha após substituição; restaurando snapshot remoto automaticamente."
  stop_remote_app_for_db_replace \
    || die "Não foi possível parar o app para rollback seguro. Snapshot preservado em $REMOTE_DB_SNAPSHOT"
  cleanup_remote_candidate
  restore_remote_db_from "$REMOTE_DB_SNAPSHOT" \
    || die "Rollback automático falhou. Snapshot preservado em $REMOTE_DB_SNAPSHOT"
  if ! run_remote_script || ! validate_remote_app_after_db_replace; then
    die "Banco anterior foi restaurado, mas a aplicação não voltou saudável. Snapshot: $REMOTE_DB_SNAPSHOT"
  fi
  die "Aplicação falhou com o candidato; rollback do banco concluído a partir de $REMOTE_DB_SNAPSHOT"
}

deploy_db_candidate() {
  if ! stop_remote_app_for_db_replace; then
    run_remote_script || true
    cleanup_remote_candidate
    die "Não foi possível garantir app parado e acesso exclusivo; banco remoto preservado."
  fi
  if ! restore_remote_db_from "$REMOTE_DB_CANDIDATE"; then
    rollback_remote_db
  fi
  cleanup_remote_candidate

  if ! run_remote_script; then
    rollback_remote_db
  fi
  if ! validate_remote_app_after_db_replace; then
    rollback_remote_db
  fi
  log "Substituição protegida do banco concluída"
}

pull_remote_db() {
  local backup_dir="$PROJECT_DIR/data/backups/manual"
  local timestamp
  local backup_path
  local tmp_local

  timestamp="$(date +%Y%m%d-%H%M%S)"
  backup_path="$backup_dir/cronograma-local-before-pull-$timestamp.db"
  tmp_local="$LOCAL_DB.pulled.$$"

  log "Baixando banco remoto para local"
  mkdir -p "$backup_dir"
  python3 "$SQLITE_BACKUP_TOOL" backup --source "$LOCAL_DB" --output "$backup_path"
  printf 'Backup local criado: %s\n' "$backup_path"

  backup_remote_db "before-pull"
  rsync -az --itemize-changes "$REMOTE_HOST:$REMOTE_DB_SNAPSHOT" "$tmp_local"
  python3 "$SQLITE_BACKUP_TOOL" validate --db "$tmp_local"
  python3 "$SQLITE_BACKUP_TOOL" restore --source "$tmp_local" --destination "$LOCAL_DB"
  rm -f "$tmp_local"
  printf 'Banco local atualizado a partir de produção: %s\n' "$LOCAL_DB"

  log "Resumo do banco local após pull"
  collect_local_db_stats | awk -F '\t' '
    BEGIN {
      printf "%-30s %10s %10s %10s\n", "métrica", "total", "vistos", "cortados"
    }
    $1 == "LESSONS" {
      printf "%-30s %10d %10d %10d\n", $2, $3, $4, $5
    }
    $1 == "TABLE" {
      printf "%-30s %10d\n", $2, $3
    }
  '
}

run_remote_script() {
  local restart_arg="--restart"
  [ "$RESTART" -eq 0 ] && restart_arg="--no-restart"

  log "Executando validação/rebuild/recreate remoto"
  ssh "${SSH_OPTS[@]}" "$REMOTE_HOST" \
    bash -s -- \
      --remote-dir "$REMOTE_DIR" \
      --db-path "$REMOTE_DB" \
      "$restart_arg" \
      < "$REMOTE_DEPLOY_SCRIPT"
}

run_restart_only() {
  log "Rebuild/recreate-only remoto"
  check_remote_readonly
  backup_remote_db
  ssh "${SSH_OPTS[@]}" "$REMOTE_HOST" \
    bash -s -- \
      --remote-dir "$REMOTE_DIR" \
      --db-path "$REMOTE_DB" \
      --restart-only \
      < "$REMOTE_DEPLOY_SCRIPT"
}

main() {
  parse_args "$@"
  check_local_prerequisites

  if [ "$PULL_DB" -eq 1 ]; then
    check_remote_readonly
    pull_remote_db
    log "Pull do banco concluído. Nenhum deploy foi executado."
    exit 0
  fi

  compile_python_files

  if [ "$RESTART_ONLY" -eq 1 ]; then
    run_restart_only
    log "Rebuild/recreate remoto concluído"
    exit 0
  fi

  check_remote_readonly

  if [ "$DRY_RUN" -eq 1 ]; then
    rsync_project
    if [ "$WITH_DB" -eq 1 ]; then
      warn "--with-db em dry-run: banco local só seria enviado após comparação local/remoto, backup e confirmação."
    fi
    log "Dry-run concluído. Nada foi alterado no remoto."
    exit 0
  fi

  ensure_remote_backup_dir
  rsync_project

  if [ "$WITH_DB" -eq 1 ]; then
    backup_remote_db "before-with-db"
    compare_db_state_before_upload
    confirm_db_deploy
    deploy_db_candidate
    log "Deploy com banco concluído. Snapshot de rollback: $REMOTE_DB_SNAPSHOT"
    exit 0
  else
    log "Banco remoto preservado. Nenhum arquivo SQLite foi enviado."
  fi

  if [ "$RESTART" -eq 1 ] && [ "$BACKUP_CREATED" -eq 0 ]; then
    backup_remote_db
    log "Backup criado antes do rebuild/recreate remoto."
  fi

  run_remote_script
  log "Deploy concluído"
}

if [ "${CRONOGRAMA_DEPLOY_LIB_ONLY:-0}" -ne 1 ]; then
  main "$@"
fi
