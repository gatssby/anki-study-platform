# API

API 3.0.0, OpenAPI 3.1.0. Schema completo público: `/openapi.json`; fonte
canônica do schema GPT compacto:
`contracts/openapi/gpt-action-compact.openapi.json`, exposta somente para
importação por GET em
`https://gatsby-anki.137.131.191.66.nip.io/openapi/gpt.json`.

`gpt-knowledge/schema gpt.json` é um symlink para a fonte canônica. A URL do
compacto não exige autenticação e usa `no-store`; as operações descritas
continuam protegidas no runtime e a autenticação da Action é configurada
separadamente no GPT Builder.

Operações organization novas usam schema 3 com `execution_mode: direct|preview`; o default do backend é `direct`. `dry_run` continua no contrato como compatibilidade e combinações incompatíveis são rejeitadas. O reporte técnico do add-on usa `/organization/operations/result`; `/confirm` é alias legado.

Categorias: decks/snapshot/cards/notes, FO/recorrência/UFPR, operações, ingestão interna e diagnósticos. `/health` é liveness pública; `/ready` e `/diagnostics` são autenticados. Paginação deve usar limits conservadores. `/cards/query-ids` sem filtro pode ser grande.

Rotas operacionais internas não são todas anunciadas ao GPT. A matriz completa está em `audits/2026-07-11-post-activation/ROUTES_MATRIX_FINAL.md`.

Migração local preparada: quatro wrappers mutáveis aceitam POST e mantêm GET com headers de depreciação até 2026-10-01. O schema GPT local tem 23 operações, mas produção/GPT Builder permanecem no contrato anterior até deploy coordenado. Request/correlation IDs também estão somente no patch local.
