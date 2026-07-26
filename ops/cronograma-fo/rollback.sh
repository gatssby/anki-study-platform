#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${1:?usage: rollback.sh /opt/cronograma-fo/backups/monorepo-TIMESTAMP}"
REMOTE_ROOT="${CRONOGRAMA_REMOTE_ROOT:-/opt/cronograma-fo}"
test -f "$BACKUP_DIR/code.tgz"
cd "$REMOTE_ROOT"
docker compose stop cronograma-fo
tar -C "$REMOTE_ROOT" -xzf "$BACKUP_DIR/code.tgz"
docker compose config >/dev/null
docker compose build cronograma-fo
docker compose up -d cronograma-fo
sleep 3
curl -fsS http://127.0.0.1:18000/ >/dev/null
