# API

API 3.2.0, OpenAPI 3.1.0. Schema completo público: `/openapi.json`; fonte
canônica do schema GPT compacto:
`contracts/openapi/gpt-action-compact.openapi.json`, exposta somente para
importação por GET em
`https://gatsby-anki.137.131.191.66.nip.io/openapi/gpt.json`.

`gpt-knowledge/schema gpt.json` é um symlink para a fonte canônica. A URL do
compacto não exige autenticação e usa `no-store`; as operações descritas
continuam protegidas no runtime e a autenticação da Action é configurada
separadamente no GPT Builder.

Respostas que materializam notes usam `CanonicalNoteRead` e publicam
`expected_content_hash`, `expected_mod`, `expected_usn`,
`expected_model_id` e o objeto equivalente `precondition`. O contrato é
diretamente reutilizável em `NoteFieldUpdate`; nenhum consumidor deve calcular
o hash.

Operações organization novas usam schema 3 com `execution_mode: direct|preview`; o default do backend é `direct`. `dry_run` continua no contrato como compatibilidade e combinações incompatíveis são rejeitadas. O reporte técnico do add-on usa `/organization/operations/result`; `/confirm` é alias legado.

Categorias: decks/snapshot/cards/notes, FO/recorrência/UFPR, operações, ingestão interna e diagnósticos. `/health` é liveness pública; `/ready` e `/diagnostics` são autenticados. Paginação deve usar limits conservadores. `/cards/query-ids` sem filtro pode ser grande.

Rotas operacionais internas não são todas anunciadas ao GPT. A matriz completa está em `audits/2026-07-11-post-activation/ROUTES_MATRIX_FINAL.md`.

O schema GPT público tem 24 operações. Wrappers legados que ainda aceitam GET
mantêm headers de depreciação até 2026-10-01; a conversão Basic -> Cloze é
somente POST.
# Conversão Basic para Cloze

`POST /organization/convert-basic-to-cloze` cria a operação estrutural
`convert_basic_to_cloze`. Ela exige `note_id`, `source_front`, `source_back`,
`text`, `back_extra` e `expected_content_hash`; aceite também as precondições
canônicas opcionais publicadas pela leitura. A operação converte in-place e não
deve ser substituída por criação de uma nova Cloze ou por `update_note_fields`.

As regras semânticas pertencem exclusivamente a
`gpt-knowledge/07_conversao_basic_para_cloze.md`.
