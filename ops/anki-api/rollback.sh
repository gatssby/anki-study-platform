#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${1:?usage: rollback.sh /home/ubuntu/anki-gpt-sync/backups/monorepo-TIMESTAMP}"
REMOTE_ROOT="${ANKI_GPT_REMOTE_ROOT:-/home/ubuntu/anki-gpt-sync}"
test -f "$BACKUP_DIR/scripts.tgz"
tmux kill-session -t anki-query-api 2>/dev/null || true
tar -C "$REMOTE_ROOT" -xzf "$BACKUP_DIR/scripts.tgz"
python3 -m py_compile "$REMOTE_ROOT"/scripts/*.py
tmux new-session -d -s anki-query-api "$REMOTE_ROOT/scripts/start_query_api.sh"
sleep 2
curl -fsS http://127.0.0.1:8767/health >/dev/null
