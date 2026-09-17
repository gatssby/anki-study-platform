#!/bin/bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BRAVE="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
USER_DATA_DIR="$HOME/Library/Application Support/BraveSoftware/Brave-Browser"
LOCAL_STATE="$USER_DATA_DIR/Local State"
PYTHON="$HERE/.venv/bin/python"

if [[ ! -x "$BRAVE" ]]; then
  echo "Brave não encontrado em: $BRAVE" >&2
  exit 2
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "Venv não encontrado. Rode primeiro:" >&2
  echo "  cd \"$HERE\"" >&2
  echo "  python3 -m venv .venv" >&2
  echo "  source .venv/bin/activate" >&2
  echo "  python3 -m pip install -r requirements.txt" >&2
  exit 2
fi

list_profiles() {
  "$PYTHON" - "$LOCAL_STATE" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.exists():
    raise SystemExit(f"Local State não encontrado: {p}")
data = json.loads(p.read_text(encoding="utf-8"))
info = data.get("profile", {}).get("info_cache", {})
last = data.get("profile", {}).get("last_used", "")
print("Perfis Brave encontrados:")
for directory, meta in info.items():
    mark = "  <== last_used" if directory == last else ""
    print(f"  {directory}\t{meta.get('name','')}\t{meta.get('user_name','')}{mark}")
print(f"\nlast_used={last or '<desconhecido>'}")
PY
}

if [[ "${1:-}" == "--list-profiles" ]]; then
  list_profiles
  exit 0
fi

PROFILE_DIRECTORY="${ANKI_CLASSIFIER_BRAVE_PROFILE_DIRECTORY:-}"

if [[ -z "$PROFILE_DIRECTORY" ]]; then
  PROFILE_DIRECTORY="$($PYTHON - "$LOCAL_STATE" <<'PY'
import json, sys, unicodedata
from pathlib import Path
p = Path(sys.argv[1])
data = json.loads(p.read_text(encoding="utf-8"))
profile = data.get("profile", {})
info = profile.get("info_cache", {})

def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower().strip()
    return s

personal = [
    directory
    for directory, meta in info.items()
    if norm(meta.get("name", "")) in {"personal", "pessoal"}
]
if len(personal) == 1:
    print(personal[0])
else:
    print(profile.get("last_used", "Default"))
PY
)"
fi

PROFILE_NAME="$($PYTHON - "$LOCAL_STATE" "$PROFILE_DIRECTORY" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
directory = sys.argv[2]
data = json.loads(p.read_text(encoding="utf-8"))
meta = data.get("profile", {}).get("info_cache", {}).get(directory, {})
print(meta.get("name", ""))
PY
)"

if [[ ! -d "$USER_DATA_DIR/$PROFILE_DIRECTORY" ]]; then
  echo "Perfil Brave não encontrado: $PROFILE_DIRECTORY" >&2
  list_profiles >&2
  echo >&2
  echo "Defina explicitamente, por exemplo:" >&2
  echo "  export ANKI_CLASSIFIER_BRAVE_PROFILE_DIRECTORY='Profile 1'" >&2
  exit 2
fi

# Playwright precisa ser o dono exclusivo do profile enquanto estiver rodando.
if pgrep -f "$BRAVE" >/dev/null 2>&1; then
  echo "O Brave já está aberto." >&2
  echo "Feche TODAS as janelas do Brave antes de iniciar a automação para evitar lock/corrupção do profile." >&2
  echo "Depois rode novamente este script." >&2
  exit 3
fi

WRAPPER="${TMPDIR:-/tmp}/anki-nid-classifier-brave-wrapper.sh"
cat > "$WRAPPER" <<EOF
#!/bin/bash
exec "$BRAVE" --profile-directory="$PROFILE_DIRECTORY" "\$@"
EOF
chmod 700 "$WRAPPER"

cleanup() {
  rm -f "$WRAPPER"
}
trap cleanup EXIT

echo "Usando Brave pessoal existente:"
echo "  user-data-dir: $USER_DATA_DIR"
echo "  profile-directory: $PROFILE_DIRECTORY"
echo "  nome do perfil: ${PROFILE_NAME:-<sem nome>}"
echo

exec "$PYTHON" "$HERE/classify.py" \
  --profile-dir "$USER_DATA_DIR" \
  --browser-executable "$WRAPPER" \
  "$@"
