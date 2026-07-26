#!/usr/bin/env bash
set -euo pipefail
umask 077

LOG="/home/ubuntu/anki-gpt-sync/logs/query_api_watchdog.log"
URL="http://127.0.0.1:8767/health"
MAX_BYTES=$((5 * 1024 * 1024))
BACKUPS=5

ts() {
  date -Is
}

rotate_log() {
  local size=0
  if [[ -f "$LOG" ]]; then
    size="$(stat -c '%s' "$LOG" 2>/dev/null || echo 0)"
  fi
  if (( size < MAX_BYTES )); then
    return
  fi
  rm -f "$LOG.$BACKUPS"
  local index
  for ((index=BACKUPS-1; index>=1; index--)); do
    if [[ -f "$LOG.$index" ]]; then
      mv "$LOG.$index" "$LOG.$((index+1))"
    fi
  done
  mv "$LOG" "$LOG.1"
}

rotate_log
status_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$URL" || true)"

if [[ "$status_code" == "200" ]]; then
  echo "[$(ts)] ok status=$status_code" >> "$LOG"
  exit 0
fi

echo "[$(ts)] API unhealthy status=$status_code; restarting tmux session anki-query-api" >> "$LOG"
tmux kill-session -t anki-query-api 2>/dev/null || true
sleep 2
tmux new-session -d -s anki-query-api 'bash /home/ubuntu/anki-gpt-sync/scripts/start_query_api.sh'
sleep 5

status_code_after="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$URL" || true)"
if [[ "$status_code_after" == "200" ]]; then
  echo "[$(ts)] restart ok status=$status_code_after" >> "$LOG"
else
  echo "[$(ts)] restart attempted but API unhealthy status=$status_code_after" >> "$LOG"
fi
