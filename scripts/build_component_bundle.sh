#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPONENT="${1:?usage: build_component_bundle.sh COMPONENT [OUTPUT_DIR]}"
OUTPUT_DIR="${2:-$ROOT/dist/$COMPONENT}"

case "$OUTPUT_DIR" in
  "$ROOT"/dist/*|/tmp/anki-study-platform-*) ;;
  *) echo "Refusing output outside repository dist or a dedicated /tmp path" >&2; exit 2 ;;
esac

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

COMMON_EXCLUDES=(
  --exclude=.DS_Store --exclude=__pycache__/ --exclude='*.pyc'
  --exclude=.env --exclude='.env.*' --exclude=data/ --exclude=state/
  --exclude=work/ --exclude=files/ --exclude=output/ --exclude=uploads/
  --exclude=imports/ --exclude=backups/ --exclude=logs/
)

case "$COMPONENT" in
  anki-api)
    mkdir -p "$OUTPUT_DIR/scripts"
    rsync -aL "${COMMON_EXCLUDES[@]}" "$ROOT/services/anki-api/" "$OUTPUT_DIR/scripts/"
    install -m 0644 \
      "$ROOT/contracts/openapi/gpt-action-compact.openapi.json" \
      "$OUTPUT_DIR/scripts/gpt-action-compact.openapi.json"
    ;;
  cronograma-fo)
    rsync -aL "${COMMON_EXCLUDES[@]}" \
      --exclude=cronograma_deploy_reset_db \
      "$ROOT/apps/cronograma-fo/" "$OUTPUT_DIR/"
    ;;
  addon)
    mkdir -p "$OUTPUT_DIR/addon-local" "$OUTPUT_DIR/local-tools"
    rsync -aL "${COMMON_EXCLUDES[@]}" "$ROOT/apps/anki-gpt/addon-local/" "$OUTPUT_DIR/addon-local/"
    rsync -aL "$ROOT/apps/anki-gpt/local-tools/anki_publish.sh" "$OUTPUT_DIR/local-tools/"
    ;;
  *)
    echo "Unknown component: $COMPONENT" >&2
    exit 2
    ;;
esac

find "$OUTPUT_DIR" -type l -print -quit | grep -q . && {
  echo "Bundle contains unresolved symlinks" >&2
  exit 1
}
printf 'BUNDLE component=%s path=%s files=%s\n' \
  "$COMPONENT" "$OUTPUT_DIR" "$(find "$OUTPUT_DIR" -type f | wc -l | tr -d ' ')"
