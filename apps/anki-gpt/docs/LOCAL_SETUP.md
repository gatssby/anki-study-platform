# Setup local

1. Instale Anki compatível (validado: 25.9.4/Python 3.13/PyQt 6.9).
2. Crie symlink `addons21/anki_gpt_sync → <PROJECT_ROOT>/addon-local`.
3. Grave o token fora do código em `<PROJECT_ROOT>/files/tagging_token.txt`, modo 0600.
4. Não use a coleção principal para testes; use pytest e perfil descartável manual.
5. Execute `pytest -q` e `scripts/validate_openapi.py`.

O runtime `files/` é ignorado pelo Git. Não reinicie/troque perfil durante uma auditoria sem registrar que o hook de sync pode publicar snapshot.

