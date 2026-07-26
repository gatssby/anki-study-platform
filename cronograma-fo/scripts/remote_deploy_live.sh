#!/usr/bin/env bash
set -Eeuo pipefail

REMOTE_DIR="/opt/cronograma-fo"
DB_PATH="/opt/cronograma-fo/data/cronograma.db"
RESTART=1
RESTART_ONLY=0
CONTAINER_NAME="${CONTAINER_NAME:-cronograma-fo}"
HEALTHCHECK_URL="${HEALTHCHECK_URL:-http://127.0.0.1:18000/database?track=FO}"
HEALTHCHECK_MAX_ATTEMPTS="${HEALTHCHECK_MAX_ATTEMPTS:-30}"
HEALTHCHECK_INTERVAL_SECONDS="${HEALTHCHECK_INTERVAL_SECONDS:-2}"
HEALTHCHECK_HTTP_TIMEOUT_SECONDS="${HEALTHCHECK_HTTP_TIMEOUT_SECONDS:-5}"

usage() {
  cat <<'EOF'
Uso:
  remote_deploy_live.sh --remote-dir /opt/cronograma-fo --db-path /opt/cronograma-fo/data/cronograma.db [--restart|--no-restart|--restart-only]

Este script roda no host remoto. Ele não importa cronogramas e não altera o banco.
Quando habilitado, o deploy rebuilda a imagem e recria o container Docker.
Depois do recreate, valida container, processo principal e HTTP 200 em rota somente leitura.
EOF
}

log() {
  printf '\n[remote] %s\n' "$*"
}

die() {
  printf '\n[remote][ERRO] %s\n' "$*" >&2
  exit 1
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --remote-dir)
        [ "${2:-}" ] || die "--remote-dir exige um valor"
        REMOTE_DIR="$2"
        shift 2
        ;;
      --db-path)
        [ "${2:-}" ] || die "--db-path exige um valor"
        DB_PATH="$2"
        shift 2
        ;;
      --restart)
        RESTART=1
        shift
        ;;
      --no-restart)
        RESTART=0
        shift
        ;;
      --restart-only)
        RESTART_ONLY=1
        RESTART=1
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
  done
}

show_processes() {
  log "Processos relevantes antes do rebuild/recreate"
  ps aux | grep -Ei "cronograma|uvicorn|gunicorn|streamlit|python" | grep -v grep || true
}

detect_restart_command() {
  if [ -f "$REMOTE_DIR/docker-compose.yml" ] || [ -f "$REMOTE_DIR/compose.yml" ]; then
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
      printf 'cd %q && docker compose up -d --build --force-recreate' "$REMOTE_DIR"
      return 0
    fi
    if command -v docker-compose >/dev/null 2>&1; then
      printf 'cd %q && docker-compose up -d --build --force-recreate' "$REMOTE_DIR"
      return 0
    fi
  fi

  return 1
}

restart_app() {
  local restart_command
  if ! restart_command="$(detect_restart_command)"; then
    cat >&2 <<EOF
[remote][ERRO] Não foi possível detectar como rebuildar/recriar o app Docker.
Verifique manualmente em $REMOTE_DIR:
  - docker-compose.yml/compose.yml com plugin 'docker compose'; ou
  - docker-compose.yml/compose.yml com binário 'docker-compose'.
EOF
    return 1
  fi

  log "Rebuild/recreate app remoto"
  printf '[remote] comando: %s\n' "$restart_command"
  bash -lc "$restart_command"
}

validate_healthcheck_config() {
  case "$HEALTHCHECK_MAX_ATTEMPTS" in
    ''|*[!0-9]*) die "HEALTHCHECK_MAX_ATTEMPTS deve ser inteiro positivo" ;;
  esac
  case "$HEALTHCHECK_INTERVAL_SECONDS" in
    ''|*[!0-9]*) die "HEALTHCHECK_INTERVAL_SECONDS deve ser inteiro não negativo" ;;
  esac
  case "$HEALTHCHECK_HTTP_TIMEOUT_SECONDS" in
    ''|*[!0-9]*) die "HEALTHCHECK_HTTP_TIMEOUT_SECONDS deve ser inteiro positivo" ;;
  esac
  [ "$HEALTHCHECK_MAX_ATTEMPTS" -gt 0 ] \
    || die "HEALTHCHECK_MAX_ATTEMPTS deve ser maior que zero"
  [ "$HEALTHCHECK_HTTP_TIMEOUT_SECONDS" -gt 0 ] \
    || die "HEALTHCHECK_HTTP_TIMEOUT_SECONDS deve ser maior que zero"
}

container_is_running() {
  [ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" = "true" ]
}

app_process_is_running() {
  docker exec "$CONTAINER_NAME" sh -c 'kill -0 1' >/dev/null 2>&1
}

http_status() {
  local status
  status="$(
    curl --silent --show-error --output /dev/null \
      --write-out '%{http_code}' \
      --max-time "$HEALTHCHECK_HTTP_TIMEOUT_SECONDS" \
      "$HEALTHCHECK_URL" 2>/dev/null || true
  )"
  printf '%s' "${status:-000}"
}

show_recent_container_logs() {
  log "Últimos logs do container $CONTAINER_NAME"
  docker logs --tail 100 "$CONTAINER_NAME" >&2 || true
}

healthcheck_app() {
  local attempt
  local status="000"
  local reason="container ainda não está em execução"

  validate_healthcheck_config
  command -v docker >/dev/null 2>&1 || die "docker não encontrado para healthcheck"
  command -v curl >/dev/null 2>&1 || die "curl não encontrado para healthcheck HTTP"
  log "Healthcheck pós-deploy: container=$CONTAINER_NAME url=$HEALTHCHECK_URL tentativas=$HEALTHCHECK_MAX_ATTEMPTS"

  for ((attempt = 1; attempt <= HEALTHCHECK_MAX_ATTEMPTS; attempt += 1)); do
    if ! container_is_running; then
      reason="container não está em execução"
    elif ! app_process_is_running; then
      reason="processo principal do container não está disponível"
    else
      status="$(http_status)"
      if [ "$status" = "200" ]; then
        log "Healthcheck aprovado na tentativa $attempt: container e processo ativos, HTTP 200"
        return 0
      fi
      reason="HTTP retornou ${status:-000}"
    fi

    printf '[remote] healthcheck tentativa %d/%d: %s\n' \
      "$attempt" "$HEALTHCHECK_MAX_ATTEMPTS" "$reason" >&2
    if [ "$attempt" -lt "$HEALTHCHECK_MAX_ATTEMPTS" ]; then
      sleep "$HEALTHCHECK_INTERVAL_SECONDS"
    fi
  done

  show_recent_container_logs
  printf '[remote][ERRO] Healthcheck falhou após %d tentativas: %s\n' \
    "$HEALTHCHECK_MAX_ATTEMPTS" "$reason" >&2
  return 1
}

validate_db() {
  log "Validação básica do banco remoto"
  [ -s "$DB_PATH" ] || die "Banco remoto ausente ou vazio: $DB_PATH"

  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 -header -column "$DB_PATH" <<'SQL'
SELECT track_code, COUNT(*) total,
       SUM(CASE WHEN is_seen = 1 THEN 1 ELSE 0 END) vistos,
       SUM(CASE WHEN is_cut = 1 THEN 1 ELSE 0 END) cortados
FROM lessons
GROUP BY track_code
ORDER BY track_code;

SELECT 'daily_assignments' tabela, COUNT(*) total FROM daily_assignments;
SELECT 'un_daily_assignments' tabela, COUNT(*) total FROM un_daily_assignments;
SELECT 'review_questions' tabela, COUNT(*) total FROM review_questions;
SELECT 'review_question_attempts' tabela, COUNT(*) total FROM review_question_attempts;
SQL
    return 0
  fi

  command -v python3 >/dev/null 2>&1 || die "Nem sqlite3 nem python3 encontrados para validar o banco."
  python3 - "$DB_PATH" <<'PY'
import sqlite3
import sys

db_path = sys.argv[1]
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
cur = conn.cursor()

print("track_code total vistos cortados")
for row in cur.execute("""
SELECT track_code, COUNT(*) total,
       SUM(CASE WHEN is_seen = 1 THEN 1 ELSE 0 END) vistos,
       SUM(CASE WHEN is_cut = 1 THEN 1 ELSE 0 END) cortados
FROM lessons
GROUP BY track_code
ORDER BY track_code
"""):
    print(*row)

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
    if not exists:
        print(table, "MISSING")
        continue
    total = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(table, total)
PY
}

preflight_remote() {
  log "Preflight remoto"
  [ -d "$REMOTE_DIR" ] || die "Diretório remoto não existe: $REMOTE_DIR"
  [ -s "$DB_PATH" ] || die "Banco remoto não existe ou está vazio: $DB_PATH"
  mkdir -p "$(dirname "$DB_PATH")/backups/manual"
  printf '[remote] app: %s\n' "$REMOTE_DIR"
  ls -lh "$DB_PATH"
}

main() {
  parse_args "$@"
  preflight_remote
  show_processes

  if [ "$RESTART" -eq 1 ]; then
    if ! restart_app; then
      show_recent_container_logs
      die "Rebuild/recreate falhou"
    fi
    healthcheck_app || die "Aplicação não ficou saudável após rebuild/recreate"
  else
    log "Rebuild/recreate pulado por --no-restart"
  fi

  validate_db

  if [ "$RESTART_ONLY" -eq 1 ]; then
    log "Rebuild/recreate-only concluído"
  else
    log "Validação remota concluída"
  fi
}

if [ "${REMOTE_DEPLOY_LIB_ONLY:-0}" -ne 1 ]; then
  main "$@"
fi
