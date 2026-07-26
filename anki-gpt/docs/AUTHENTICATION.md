# Autenticação

Header: `X-Tagging-Token`; nome do secret no GPT Builder: `TaggingToken`. Use `<READ_TOKEN>`/`<WRITE_TOKEN>` apenas como placeholders; atualmente a mesma credencial protege ambos os escopos.

Read auth é controlada por `ANKI_GPT_REQUIRE_READ_AUTH=1`; segredo por `ANKI_GPT_TAGGING_TOKEN` ou arquivo. Token ausente/inválido/duplicado retorna 401. Token não é aceito em query string nem registrado.

Rotação exige atualizar consumidores e reiniciar backend/addon de forma coordenada; valide o novo token antes de invalidar o antigo quando o desenho futuro suportar sobreposição.

