# Anki Study Platform

Monorepo privado e modular para o addon do Anki, a API Anki, o Cronograma FO,
os jobs do portal/transcrições e seus contratos.

Esta migração preserva inicialmente os runtimes e os caminhos de produção:

- API Anki: `/home/ubuntu/anki-gpt-sync`, bind local `127.0.0.1:8767`;
- Cronograma FO: `/opt/cronograma-fo`, porta publicada `18000`;
- transcrições: `/home/ubuntu/fo-transcricoes-system` e
  `/home/ubuntu/fo-transcricoes`;
- addon: carregado pelo runtime do Anki como bundle autossuficiente.

Dados persistentes, credenciais, sessões, mídia, transcrições, bancos, índices e
uploads não pertencem ao Git. Cada componente continua com deploy independente.

## Estrutura

- `apps/anki-gpt`: addon, ferramentas locais e GPT Knowledge;
- `apps/cronograma-fo`: aplicação web e scripts compatíveis;
- `services/anki-api`: backend e indexação Anki;
- `jobs`: sincronização do portal, vídeos, transcrições e reprogramação;
- `packages`: contratos FO, I/O seguro e contratos Anki;
- `contracts`: OpenAPI, JSON Schema e fixtures artificiais;
- `ops`: bundles, deploys e rollbacks por componente;
- `tests`: compatibilidade entre produtores e consumidores.

Consulte `docs/ARCHITECTURE.md`, `docs/DEPENDENCIES.md` e os runbooks em
`ops/`. O resultado auditável do cutover inicial está em
`docs/migration/2026-07-26/FINAL_REPORT.md`.
