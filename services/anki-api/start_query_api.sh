#!/usr/bin/env bash
set -euo pipefail
umask 077

BASE=/home/ubuntu/anki-gpt-sync
SCRIPTS=$BASE/scripts
TAGGING_TOKEN_FILE=$BASE/tagging_token.txt
ANKI_GPT_REQUIRE_READ_AUTH=1
ANKI_GPT_DEFAULT_EXECUTION_MODE=direct
export ANKI_GPT_REQUIRE_READ_AUTH ANKI_GPT_DEFAULT_EXECUTION_MODE

if [ -f "$TAGGING_TOKEN_FILE" ]; then
  ANKI_GPT_TAGGING_TOKEN="$(tr -d '\r\n' < "$TAGGING_TOKEN_FILE")"
  export ANKI_GPT_TAGGING_TOKEN
fi

exec python3 "$SCRIPTS/query_api.py"
