#!/usr/bin/env bash
set -euo pipefail

cd /home/ubuntu/anki-gpt-sync

python3 scripts/fo_sync_materials_from_portal_materiais.py --sync-onedrive
