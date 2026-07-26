# Fluxo Para Decks Grandes

Use este arquivo quando auditoria, normalizacao, grifo, classificacao ou reorder envolver deck/query com muitos cards. Lotes servem apenas para leitura. A decisao editorial e a operacao devem ser globais.

## Leitura Obrigatoria

1. Use `/cards/query-ids` para buscar todos os `card_ids`, `note_ids` e a relacao `card_to_note` do deck/query.
2. Divida apenas a leitura/materializacao em lotes, preferencialmente com 10 a 20 cards. Aumente somente quando a resposta permanecer confortavelmente abaixo de `max_materialize_batch_bytes` e do limite de contexto do consumidor; o limite tecnico da API nao garante que a resposta seja adequada ao GPT.
3. Use `/cards/materialize` para ler cada lote.
4. Se qualquer lote falhar, pare. Nao aplique operacao real, nao gere reorder parcial e relate o lote/IDs que falharam.
5. Consolide todos os lotes em uma unica estrutura global antes de auditar, normalizar, grifar, classificar ou reordenar.
6. Para target por note, deduplicate por `note_id` somente depois da consolidacao.
7. Nao trate a primeira amostra como retrato do deck inteiro. Se a materializacao foi parcial, diga que o diagnostico e parcial.

## Auditoria Global

Ao auditar decks grandes, entregue achados por padrao, nao lista exaustiva:

- total encontrado em `/cards/query-ids`;
- total materializado;
- quantidade de lotes;
- deck/query consultado;
- cards/notes ignorados e motivo;
- padroes de erro, duplicidade, lacuna e excesso de custo;
- exemplos concretos com `nid:<id>` quando a API retornar dados.

Nao recomende exclusao, migracao ou criacao em massa com base em hipotese. Se faltar fonte, material FO ou lote materializado, consulte antes ou bloqueie a etapa operacional.

## Normalizacao E Grifo Em Massa

Para normalizacao/grifo em muitos cards:

1. Materialize todos os lotes.
2. Consolide por `note_id`.
3. Preserve conteudo conceitual. Remova apenas wrappers visuais antigos e reaplique `.kw`/`.hint` conforme `04_estetica_e_exatas.md`.
4. Gere `createUpdateNoteFieldsOperation` apenas com notes que realmente mudariam.
5. Use `execution_mode: "direct"` e `dry_run: false` por padrao, com precondicoes v2 por note. Use `preview` somente se o usuario pedir explicitamente.
6. Respeite limites do schema e do payload. Se forem necessarios varios payloads, mantenha um plano global; varias operacoes pendentes podem ser consumidas na mesma sincronizacao do addon.

## Reorder Por Materiais, Created Ou Added

Reorder de decks grandes deve ser global. O objetivo operacional e alterar a ordem de cards New pelo campo de criacao exibido no Anki Browser como `Created` e, quando a API expuser esse mesmo valor como `Added`, trate `Created/Added` como o mesmo eixo temporal derivado de IDs. Nao prometa alterar `due`, intervalo, facilidade, revlog ou historico de revisao.

Fluxo obrigatorio:

1. Use `/cards/query-ids` para obter o conjunto completo.
2. Materialize todos os lotes com `/cards/materialize`.
3. Monte uma unica lista global de `ordered_note_ids`, completa, deduplicada, nao vazia e sem notes fora do deck/query.
4. Para reorder por material, coloque primeiro cards claramente presentes nos materiais FO, seguindo a ordem logica das aulas/paginas. Depois entram cards sem correspondencia clara, por complexidade inferida: definicao basica, identificacao/classificacao, mecanismo/processo, comparacao, excecoes, aplicacoes, detalhes avancados/overkill.
5. Para reorder por Created/Added sem material, use a ordem editorial global solicitada pelo usuario ou a ordem inferida apos auditoria completa; nao ordene por lote.
6. Use `createReorderCardsByMaterialOperation` quando disponivel, com `target_created_column: "note"`, `scope: "currently_new_cards"`, `apply_created_date: true`, `multi_card_note_policy: "group_if_all_new"` e `require_global_order: true`.
7. Inclua `expected_eligible_count` e `expected_eligible_card_ids` quando a API fornecer esses dados.
8. Crie a operacao com `execution_mode: "direct"` e `dry_run: false`, preservando expected counts/IDs e a ordem global. Uma unica consulta/sincronizacao do addon deve bastar.
9. Se o usuario pedir preview explicitamente, use `execution_mode: "preview"` e `dry_run: true`, relate skips, primeiros/ultimos itens e contagens; uma aplicacao posterior usa nova operacao direct equivalente pelo fluxo legado.

Nunca ordene lote 1, depois lote 2, e junte. Nunca envie `ordered_note_ids` parcial. Nunca faça reorder se qualquer lote falhou, se ha duplicata em `ordered_note_ids`, se o total de notes elegiveis nao bate ou se o material esperado nao foi consultado.

## Relatorio Final

O relatorio final deve informar:

- total encontrado;
- total materializado;
- quantidade de lotes;
- confirmacao de consolidacao global antes do reorder/normalizacao;
- fonte usada para ordenar;
- primeiros e ultimos itens da ordem global;
- itens posicionados por complexidade inferida;
- itens ignorados com motivo;
- modo (`direct` ou `preview`) separado do estado;
- operation IDs e resultado de cada operacao;
- IDs problematicos para permitir correcao localizada sem reprocessar o deck.
