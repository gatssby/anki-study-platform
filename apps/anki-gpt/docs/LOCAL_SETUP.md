# Setup local

1. Instale Anki compatível (validado: 26.5/Python 3.13.11/macOS arm64).
2. Crie symlink `addons21/anki_gpt_sync → <PROJECT_ROOT>/addon-local`.
3. Grave o token fora do código em
   `~/Library/Application Support/Anki2/addon-data/anki_gpt_sync/tagging_token.txt`,
   modo 0600.
4. Não use a coleção principal para testes; use pytest e perfil descartável manual.
5. Execute `pytest -q` e `scripts/validate_openapi.py`.

O runtime mutável fica em
`~/Library/Application Support/Anki2/addon-data/anki_gpt_sync`, fora do Git e
fora do symlink do código. `ANKI_GPT_RUNTIME_DIR` permite override explícito
para testes/diagnóstico. Não reinicie/troque perfil durante uma auditoria sem
registrar que o hook de sync pode publicar snapshot.
