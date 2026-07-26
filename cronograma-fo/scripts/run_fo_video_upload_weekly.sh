#!/usr/bin/env bash
set -euo pipefail

cd /opt/cronograma-fo

LOCK_FILE="/opt/cronograma-fo/state/federal_online/cronograma_fo/fo_video_upload_weekly.lock"
LOG_DIR="/opt/cronograma-fo/state/federal_online/logs"
mkdir -p "$(dirname "$LOCK_FILE")" "$LOG_DIR"

exec flock -n "$LOCK_FILE" \
  python3 scripts/fo_download_upload_videos.py "$@"
