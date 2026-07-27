# Autenticação

Header: `X-Tagging-Token`; nome do secret no GPT Builder: `TaggingToken`. Use `<READ_TOKEN>`/`<WRITE_TOKEN>` apenas como placeholders; atualmente a mesma credencial protege ambos os escopos.

Read auth é controlada por `ANKI_GPT_REQUIRE_READ_AUTH=1`; segredo por
`ANKI_GPT_TAGGING_TOKEN` ou arquivo. No addon, o ambiente tem precedência sobre
`~/Library/Application Support/Anki2/addon-data/anki_gpt_sync/tagging_token.txt`.
O arquivo deve conter um único token não vazio. Token ausente, inválido ou
divergente retorna 401. Token não é aceito em query string nem registrado.

Sincronização manual sem token mostra o caminho canônico e não materializa a
coleção. Sincronização automática registra `missing_authentication_token` sem
popup. Falhas de upload preservam uma causa estruturada:
`missing_authentication_token`, `authentication_rejected`, `network_error`,
`server_error` ou `invalid_response`.

Rotação exige atualizar consumidores e reiniciar backend/addon de forma coordenada; valide o novo token antes de invalidar o antigo quando o desenho futuro suportar sobreposição.
