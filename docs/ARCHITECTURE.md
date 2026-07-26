# Arquitetura do monorepo

```text
apps/
  anki-gpt/
    addon-local/
    local-tools/
    gpt-knowledge/
  cronograma-fo/
services/
  anki-api/
jobs/
  fo-portal-sync/
  fo-video-sync/
  fo-transcription-index/
  cronograma-reprogramming/
packages/
  anki-contracts/
  fo-contracts/
  safe-io/
contracts/
  openapi/
  jsonschema/
  fixtures/
ops/
  anki-api/
  cronograma-fo/
  shared-fo/
tests/
```

## Fluxo de integração

O Cronograma agenda `run_fo_full_sync.sh`. O coletor produz
`aulas_index.tsv` de forma atômica e valida o contrato antes da promoção. O job
canônico de materiais protege o storage state e valida o manifesto. A API Anki
lê o índice em modo degradável e valida seu cabeçalho antes de confiar nas
linhas. A fila/transcrição continua em serviços systemd separados.

## Compatibilidade

- `apps/anki-gpt/remote-backend` aponta para `services/anki-api`, preservando
  testes e documentação antigos.
- os scripts FO antigos são symlinks para `jobs/fo-portal-sync`;
- bundles dereferenciam symlinks e preservam os layouts planos da VPS;
- o addon permanece autossuficiente e mantém `local-tools` como diretório irmão.
