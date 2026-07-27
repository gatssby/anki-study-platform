# Troubleshooting

- 401: conferir nome do secret/header e restart após rotação; não imprimir token.
- 404: comparar método/path com `/openapi.json`; `/schema` não existe.
- decks errados: usar `total_deck_count`, não número de chaves/partições.
- índice stale: conferir diagnostics, generation ID e horário do snapshot; não forçar sync principal.
- addon antigo: comparar `addon_runtime.json` com hashes em disco e reiniciar manualmente.
- diagnóstico de pausa divergente: conferir `~/Library/Application Support/Anki2/addon-data/anki_gpt_sync/state/pause_auto_publish` e `addon_runtime.json`. O diagnóstico deve registrar `auto_publish_paused` a partir do filesystem na próxima escrita; se o código foi alterado em disco, reload manual é necessário para atualizar o hash carregado.
- runtime local: conferir `~/Library/Application Support/Anki2/addon-data/anki_gpt_sync`; um erro de filesystem deve aparecer em stderr sem impedir o carregamento do addon.
- FTS ausente/corrupto: verify, depois rebuild atômico conforme runbook.
- operação parcial: parar fila, preservar logs e reconciliar notes artificiais/IDs mascarados antes de retry.
- confirmação perdida: verificar receipt e hashes; receipt válido reenvia confirmação sem apply, expirado/divergente deve bloquear. A produção ainda não recebeu esse patch.
- busca lenta: comparar geração e hit/miss do cache normalizado; fallback linear preserva contrato.
- correlation ID ausente: confirmar que o backend com observabilidade nova foi implantado; não inferir pelo código local.
- cleanup lista ativa/anterior: não aplicar; validar `current.json` e manifest antes de investigar o script.
- Nginx inválido: restaurar somente os arquivos do backup, executar `nginx -t` e não recarregar até passar.
