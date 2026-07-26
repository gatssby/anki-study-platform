#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE="${REMOTE_HOST:-oracle-vps}"
REMOTE_ROOT="${CRONOGRAMA_REMOTE_ROOT:-/opt/cronograma-fo}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BUNDLE="/tmp/anki-study-platform-cronograma-$STAMP"

"$ROOT/scripts/build_component_bundle.sh" cronograma-fo "$BUNDLE"
ssh "$REMOTE" "set -euo pipefail; umask 077; mkdir -p '$REMOTE_ROOT/backups/monorepo-$STAMP'; tar -C '$REMOTE_ROOT' -czf '$REMOTE_ROOT/backups/monorepo-$STAMP/code.tgz' --exclude=data --exclude=state --exclude=work --exclude=output --exclude=uploads --exclude=imports --exclude=backups .; sqlite3 '$REMOTE_ROOT/data/cronograma.db' '.backup $REMOTE_ROOT/backups/monorepo-$STAMP/cronograma.db'"
rsync -az --itemize-changes \
  --exclude='.env' --exclude='data/' --exclude='state/' --exclude='work/' \
  --exclude='output/' --exclude='uploads/' --exclude='imports/' --exclude='backups/' \
  "$BUNDLE/" "$REMOTE:$REMOTE_ROOT/"
ssh "$REMOTE" "set -euo pipefail; cd '$REMOTE_ROOT'; if [ ! -x .venv/bin/python ]; then python3 -m venv --system-site-packages .venv; fi; .venv/bin/python -m pip install -q -r requirements.txt; .venv/bin/python -m pip check; docker compose config >/dev/null; docker compose build cronograma-fo; docker compose up -d cronograma-fo; sleep 3; curl -fsS http://127.0.0.1:18000/ >/dev/null"
echo "CRONOGRAMA_DEPLOYED backup=$REMOTE_ROOT/backups/monorepo-$STAMP"
