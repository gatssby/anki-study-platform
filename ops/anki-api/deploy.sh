#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE="${REMOTE_HOST:-oracle-vps}"
REMOTE_ROOT="${ANKI_GPT_REMOTE_ROOT:-/home/ubuntu/anki-gpt-sync}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BUNDLE="/tmp/anki-study-platform-anki-api-$STAMP"

"$ROOT/scripts/build_component_bundle.sh" anki-api "$BUNDLE"
ssh "$REMOTE" "set -euo pipefail; umask 077; mkdir -p '$REMOTE_ROOT/backups/monorepo-$STAMP'; tar -C '$REMOTE_ROOT' -czf '$REMOTE_ROOT/backups/monorepo-$STAMP/scripts.tgz' scripts"
rsync -az --itemize-changes \
  --exclude='*.env' --exclude='*token*' --exclude='state/' --exclude='data/' \
  --exclude='logs/' --exclude='backups/' \
  "$BUNDLE/scripts/" "$REMOTE:$REMOTE_ROOT/scripts/"
ssh "$REMOTE" "set -euo pipefail; python3 -m py_compile '$REMOTE_ROOT'/scripts/*.py; tmux kill-session -t anki-query-api 2>/dev/null || true; tmux new-session -d -s anki-query-api '$REMOTE_ROOT/scripts/start_query_api.sh'; sleep 2; curl -fsS http://127.0.0.1:8767/health >/dev/null"
echo "ANKI_API_DEPLOYED backup=$REMOTE_ROOT/backups/monorepo-$STAMP/scripts.tgz"
