#!/usr/bin/env bash
set -euo pipefail

BASE="http://127.0.0.1:8767"
LOG="/home/ubuntu/anki-gpt-sync/state/fo_transcripts_api_test.txt"
REL="Extensivo UFPR 2026/Aulas Gerais/Biologia/Biologia I/Aula 08 - Respiração celular.transcricao.md"

{
  echo "## listar Biologia I disponíveis"
  curl -sS -G "$BASE/fo/transcripts" \
    --data-urlencode "materia=Biologia" \
    --data-urlencode "frente=Biologia I" \
    --data-urlencode "limit=5" | python3 -m json.tool

  echo
  echo "## buscar Aula 03 de Biologia I na fila/metadados"
  curl -sS -G "$BASE/fo/transcripts" \
    --data-urlencode "materia=Biologia" \
    --data-urlencode "frente=Biologia I" \
    --data-urlencode "status=done" \
    --data-urlencode "exists=false" \
    --data-urlencode "aula_number=3" \
    --data-urlencode "limit=5" | python3 -m json.tool

  echo
  echo "## ler uma transcrição done disponível"
  curl -sS -G "$BASE/fo/transcript" \
    --data-urlencode "relative_path=$REL" \
    --data-urlencode "max_chars=1200" | python3 -m json.tool

  echo
  echo "## buscar termo dentro das transcrições"
  curl -sS -G "$BASE/fo/transcripts/search" \
    --data-urlencode "q=respiração" \
    --data-urlencode "materia=Biologia" \
    --data-urlencode "limit=5" | python3 -m json.tool

  echo
  echo "## endpoint antigo /fo/materials continua respondendo"
  curl -sS -G "$BASE/fo/materials" \
    --data-urlencode "limit=1" | python3 -m json.tool

  echo
  echo "## openapi contém endpoints novos"
  curl -sS "$BASE/openapi.json" | python3 -m json.tool | grep -E "fo/transcripts|fo/transcript" || true

  echo
  echo "## path traversal bloqueado"
  curl -sS -G "$BASE/fo/transcript" \
    --data-urlencode "relative_path=../queue.sqlite" | python3 -m json.tool
} | tee "$LOG"

echo "log=$LOG"
