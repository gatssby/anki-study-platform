# Legado preservado

- `services/anki-api/server.py` não foi importado para o serviço ativo. A cópia
  histórica permanece nos diretórios originais e no backup; ela abria `8766`
  sem o contrato/autenticação atuais e não pode integrar um bundle.
- `apps/cronograma-fo/cronograma_deploy_reset_db` permanece preservado no
  histórico e no diretório original, mas é excluído dos bundles do monorepo.
- scripts `fo_test_*`, `fo_inspect_*`, `fo_list_*` e equivalentes são
  diagnósticos legados. Não são chamados por crontab, systemd ou pelo deploy
  canônico.
- `GPT_BUILDER_PUBLICAR` e bundles históricos não foram importados. A fonte
  canônica é `apps/anki-gpt/gpt-knowledge` mais
  `contracts/openapi/gpt-action-compact.openapi.json`.

Nenhum original ou bundle de rollback foi apagado.
