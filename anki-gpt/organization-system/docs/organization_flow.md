# Fluxo de organizacao de decks/cards/notes

## Objetivo

Este fluxo permite que a VPS registre operacoes de organizacao para execucao futura pelo addon local do Anki. A VPS nao acessa a collection real do Anki; ela trabalha com snapshots materializados e uma fila persistente de operacoes.

Operacoes de escrita reais precisam rodar dentro do Anki, via addon local, usando `aqt.mw` / `mw.col`.

## Autenticacao

Os endpoints de organizacao usam o mesmo token operacional do fluxo de tagging:

- header: `X-Tagging-Token`
- variavel de ambiente: `ANKI_GPT_TAGGING_TOKEN`
- arquivo na VPS: `/home/ubuntu/anki-gpt-sync/tagging_token.txt`
- arquivo local do addon: `~/anki-gpt-files/tagging_token.txt`

A escolha por reutilizar o token evita introduzir uma segunda credencial para o mesmo canal VPS -> addon nesta etapa. A fila de organizacao continua separada da fila de tagging.

## Armazenamento remoto

As operacoes ficam em:

`/home/ubuntu/anki-gpt-sync/state/organization/operations/<operation_id>.json`

Formato inicial:

```json
{
  "operation_id": "orgop-20260505T120000000000Z-example",
  "operation_type": "create_deck",
  "operation_schema_version": 3,
  "execution_mode": "direct",
  "dry_run": false,
  "payload": {
    "deck_name": "#UFPR::Biologia::Citologia::Membrana Plasmatica"
  },
  "requested_by": "gpt",
  "reason": "Criar subdeck para reorganizacao futura",
  "created_at": "2026-05-05T12:00:00+00:00",
  "status": "pending",
  "result": null,
  "confirmed_by_user": false
}
```

Depois da execucao pelo addon:

```json
{
  "operation_id": "orgop-20260505T120000000000Z-example",
  "operation_type": "create_deck",
  "operation_schema_version": 3,
  "execution_mode": "direct",
  "dry_run": false,
  "payload": {
    "deck_name": "#UFPR::Biologia::Citologia::Membrana Plasmatica"
  },
  "requested_by": "gpt",
  "reason": "Criar subdeck para reorganizacao futura",
  "created_at": "2026-05-05T12:00:00+00:00",
  "status": "done",
  "result": {
    "deck": "#UFPR::Biologia::Citologia::Membrana Plasmatica",
    "created": true
  },
  "confirmed_by_user": false,
  "executed_at": "2026-05-05T12:01:00+00:00",
  "execution_result": {
    "ok": true,
    "status": "done",
    "executed_at": "2026-05-05T12:01:00+00:00",
    "addon_profile": "User 1",
    "errors": [],
    "metadata": {}
  }
}
```

## Endpoints

`POST /organization/operations`

Cria uma operacao pendente. Exige token. `execution_mode` usa `direct` por padrao; `confirmed_by_user` e somente metadado legado.

Payload:

```json
{
  "operation_type": "create_deck",
  "execution_mode": "direct",
  "payload": {
    "deck_name": "#UFPR::Biologia::Citologia::Membrana Plasmatica"
  },
  "requested_by": "gpt",
  "reason": "Criar subdeck para reorganizacao futura"
}
```

`GET /organization/operations`

Lista operacoes. Por padrao retorna apenas `status=pending`. Use `status=all` para auditoria.

`POST /organization/operations/result`

Reporta o resultado tecnico da execucao feita pelo addon local. Nao e confirmacao manual. O alias legado `/organization/operations/confirm` continua aceito. Status finais:

- `done`
- `failed`
- `skipped`

Exemplo de resultado:

```json
{
  "ok": true,
  "operation_id": "orgop-20260505T120000000000Z-example",
  "operation_type": "create_deck",
  "status": "done",
  "addon_profile": "User 1",
  "result": {
    "deck": "#UFPR::Biologia::Citologia::Membrana Plasmatica",
    "created": true
  },
  "errors": []
}
```

## Tipos de operacao suportados

O contrato atual do fluxo `organization` aceita estes `operation_type`:

- `create_deck`
- `move_cards_to_deck`
- `move_notes_to_deck`
- `get_reorganization_log`
- `undo_reorganization`
- `undo_last_reorganization`
- `mark_notes_as_ideal_deck`
- `mark_cards_as_ideal_deck`
- `check_ideal_deck_status`
- `list_note_types`
- `get_note_type_fields`
- `create_note`
- `create_notes`
- `replace_note_tags`
- `update_note_fields`

Operacoes desconhecidas sao rejeitadas pelo backend remoto e pelo dispatcher local do addon.

## Operacoes suportadas

Nos exemplos legados abaixo, `dry_run: true` representa uma preview explicitamente solicitada. Para o fluxo padrao, envie `execution_mode: "direct"` e `dry_run: false`; `confirmed_by_user` pode ser omitido.

### `create_deck`

Cria deck ou subdeck no Anki.

Regras:

- `payload.deck_name` deve ser string nao vazia.
- subdecks com `::` sao aceitos.
- se o deck ja existir, a operacao e sucesso com `created: false`.
- se o deck nao existir, o addon cria e retorna `created: true`.
- nao renomeia decks.
- nao apaga decks.
- nao move cards.
- nao cria notes/cards.
- nao altera conteudo nem tags.

### `move_cards_to_deck`

Move cards especificos existentes para outro deck/subdeck.

Payload:

```json
{
  "operation_type": "move_cards_to_deck",
  "confirmed_by_user": true,
  "payload": {
    "card_ids": [123, 456],
    "target_deck": "#UFPR::Biologia::Citologia::Membrana Plasmatica",
    "dry_run": true,
    "respect_ideal_deck": true,
    "force": false,
    "mark_ideal": false,
    "add_tags": []
  },
  "requested_by": "gpt",
  "reason": "Preview de movimentacao"
}
```

Regras:

- `execution_mode` e `direct` por padrao; `dry_run` e derivado como `false`.
- em `dry_run: true`, nada e alterado no Anki;
- em `dry_run: false`, apenas o deck dos cards existentes e alterado;
- `card_id`, `note_id`, scheduling, historico de revisao, fields, midia e tags existentes sao preservados;
- o card nao e recriado;
- o target deck e criado se nao existir;
- `add_tags`, quando informado, adiciona tags nas notes correspondentes aos cards.
- se `respect_ideal_deck: true` e `force: false`, cards cujas notes tenham `organizado::deck_ideal` nao sao movidos e aparecem em `skipped_ideal_deck`;
- se `force: true`, a protecao de deck ideal e ignorada para a operacao;
- se `mark_ideal: true` e `dry_run: false`, o addon adiciona tags de deck ideal apos mover com sucesso.

O preview inclui card atual, note, `ord`, template, note type, tags, deck atual/destino, dados de scheduling e informacoes sobre cards irmaos da mesma note.

### `move_notes_to_deck`

Move todos os cards gerados pelas notes informadas para outro deck/subdeck.

Payload:

```json
{
  "operation_type": "move_notes_to_deck",
  "confirmed_by_user": true,
  "payload": {
    "note_ids": [111, 222],
    "target_deck": "#UFPR::Biologia::Citologia::Membrana Plasmatica",
    "dry_run": true,
    "respect_ideal_deck": true,
    "force": false,
    "mark_ideal": false,
    "add_tags": []
  },
  "requested_by": "gpt",
  "reason": "Preview de movimentacao"
}
```

Diferença principal:

- `move_cards_to_deck` move somente os `card_ids` informados.
- `move_notes_to_deck` resolve cada `note_id` para todos os cards reais gerados por ela e move todos esses cards.

Isso importa especialmente para cloze e notes com multiplos templates: uma note pode gerar varios cards, e os cards podem estar em decks diferentes.

## Deck ideal

Decks/subdecks representam a taxonomia real de estudo: disciplina, assunto e subassunto. Tags de deck ideal sao apenas metadados de auditoria e protecao para evitar remanejamento acidental.

Tags padrao:

- `organizado::deck_ideal`;
- `organizado::movido_por_gpt`;
- `organizado::destino::<slug_do_deck>`.

Cada note deve ter no maximo uma tag ativa com prefixo `organizado::destino::`. Ao marcar um deck ideal, o addon remove somente tags antigas que comecem exatamente com esse prefixo antes de adicionar a nova tag de destino. Tags como `organizado::movido_por_gpt`, prioridade, origem, FO ou qualquer tag fora de `organizado::destino::` sao preservadas.

O slug preserva a hierarquia com `::`, porque o Anki aceita tags hierarquicas nesse formato. Cada componente do deck e normalizado para minusculas, sem acentos, com espacos e pontuacao convertidos para `_`, e caracteres problemáticos removidos.

Exemplo:

`#UFPR::Biologia::Citologia::Membrana Plasmática`

vira:

`organizado::destino::ufpr::biologia::citologia::membrana_plasmatica`

### `mark_cards_as_ideal_deck`

Resolve os cards para notes e adiciona as tags de deck ideal nas notes. Nao move cards.

```json
{
  "operation_type": "mark_cards_as_ideal_deck",
  "confirmed_by_user": true,
  "payload": {
    "card_ids": [1778016764561],
    "deck_name": "#UFPR::Teste GPT::Move Destino"
  },
  "requested_by": "gpt",
  "reason": "Marcar card como ja estando no deck ideal"
}
```

### `mark_notes_as_ideal_deck`

Adiciona as tags de deck ideal diretamente nas notes informadas. Nao move cards.

Se a note ja tinha outro destino ideal, por exemplo `organizado::destino::ufpr::bioquimica::lipidios`, essa tag e substituida pela nova tag `organizado::destino::<slug_do_deck>`. A tag `organizado::deck_ideal` e mantida.

```json
{
  "operation_type": "mark_notes_as_ideal_deck",
  "confirmed_by_user": true,
  "payload": {
    "note_ids": [1778016764561],
    "deck_name": "#UFPR::Teste GPT::Move Destino"
  },
  "requested_by": "gpt",
  "reason": "Marcar note como ja estando no deck ideal"
}
```

### `check_ideal_deck_status`

Retorna o status de protecao por card/note: deck atual, tags de destino encontradas, slug esperado e se o slug parece bater com o deck atual.

```json
{
  "operation_type": "check_ideal_deck_status",
  "confirmed_by_user": true,
  "payload": {
    "card_ids": [1778016764561],
    "note_ids": []
  },
  "requested_by": "gpt",
  "reason": "Verificar status de deck ideal"
}
```

## Criacao segura de notes/cards

O fluxo `organization` tambem suporta criacao segura de notes/cards. O default e `execution_mode: "direct"`; `preview` valida deck, note type, campos, cloze e duplicatas sem criar nada quando solicitada explicitamente.

Note types suportados nesta etapa:

- `prettify-minimal-basic`;
- `prettify-minimal-basic_reverse`;
- `prettify-minimal-cloze`.

No note type `prettify-minimal-cloze`, os campos reais validados sao `Text` e `Back Extra`; os exemplos abaixo usam esses nomes.

Quando `tags` e omitido em `create_note` ou em uma note de `create_notes`, a tag padrao aplicada e `GPT`. Tags adicionais devem ser enviadas apenas quando o usuario pedir explicitamente. Nao use `criado_por_gpt`, `teste_gpt` ou `origem:*` como tags automaticas.

Para novo conteudo com destaque discreto, use `<span class="kw">...</span>` em palavras-chave estruturantes e `<span class="hint">...</span>` em hints de Cloze. Nao use `<font color="...">` nem `<span style="color: ...">` em cards novos, salvo pedido explicito.

Image occlusion fica fora do escopo. Delete tambem continua fora do escopo.

### `list_note_types`

Lista os note types existentes no Anki e marca quais sao suportados para criacao.

```json
{
  "operation_type": "list_note_types",
  "confirmed_by_user": true,
  "payload": {},
  "requested_by": "gpt",
  "reason": "Listar note types disponiveis"
}
```

### `get_note_type_fields`

Retorna campos e templates reais do note type.

```json
{
  "operation_type": "get_note_type_fields",
  "confirmed_by_user": true,
  "payload": {
    "model_name": "prettify-minimal-cloze"
  },
  "requested_by": "gpt",
  "reason": "Verificar campos do note type"
}
```

### `create_note`

Cria uma note individual. Em `dry_run: true`, apenas valida e retorna preview.

```json
{
  "operation_type": "create_note",
  "confirmed_by_user": true,
  "payload": {
    "deck_name": "#UFPR::Teste GPT::Card Creation",
    "model_name": "prettify-minimal-cloze",
    "fields": {
      "Text": "A membrana plasmática é formada por uma {{c1::bicamada fosfolipídica}}.",
      "Back Extra": "Modelo mosaico fluido."
    },
    "tags": ["GPT"],
    "allow_duplicate": false,
    "dry_run": true
  },
  "requested_by": "gpt",
  "reason": "Preview de criacao de cloze"
}
```

Para Cloze, o addon exige ao menos uma lacuna no formato `{{cN::...}}` e retorna `cloze_numbers`, por exemplo `[1, 2]`. Sintaxe claramente quebrada, como braces desbalanceados ou `{{c::...}}`, e rejeitada.

Em criacao real (`dry_run: false`), o addon cria o deck se ele nao existir, cria a note via API do Anki, aplica as tags informadas e retorna `note_id` e `card_ids` gerados.

### `create_notes`

Cria ou valida lote de notes. O limite inicial e 20 notes por operacao.

```json
{
  "operation_type": "create_notes",
  "confirmed_by_user": true,
  "payload": {
    "notes": [
      {
        "deck_name": "#UFPR::Teste GPT::Card Creation",
        "model_name": "prettify-minimal-cloze",
        "fields": {
          "Text": "A membrana plasmática é formada por uma {{c1::bicamada fosfolipídica}}.",
          "Back Extra": "Modelo mosaico fluido."
        },
        "tags": ["GPT"],
        "allow_duplicate": false
      }
    ],
    "dry_run": true
  },
  "requested_by": "gpt",
  "reason": "Preview de criacao em lote"
}
```

### Checagem de duplicatas

A checagem e conservadora e roda dentro do Anki. O addon compara notes existentes do mesmo note type (`mid`) usando texto normalizado dos campos. Uma note e considerada possivel duplicata quando todos os campos normalizados batem ou quando o primeiro campo normalizado bate. Em `dry_run: true`, duplicata retorna warning. Em `dry_run: false` com `allow_duplicate: false`, a criacao e rejeitada antes de criar. Com `allow_duplicate: true`, cria e retorna warning.

### `replace_note_tags`

Substitui tags de notes existentes sem alterar fields, deck, cards, midia ou scheduling. O default e `direct`; use `preview` somente por pedido explicito.

```json
{
  "operation_type": "replace_note_tags",
  "confirmed_by_user": true,
  "payload": {
    "note_ids": [1778103173630, 1778103173772, 1778103173912],
    "remove_tags": ["criado_por_gpt", "origem:fo_bio1_aulas05_06"],
    "add_tags": ["GPT"],
    "dry_run": true
  },
  "requested_by": "gpt",
  "reason": "Padronizar tags dos cards recem-criados"
}
```

### `update_note_fields`

Atualiza campos informados de notes existentes. Esta operacao existe para edicoes esteticas controladas, como remover wrappers visuais antigos e regrifar com `<span class="kw">...</span>` e `<span class="hint">...</span>`.

Regras:

- exige request autenticado; `confirmed_by_user` e metadado legado;
- `execution_mode` e `direct` por padrao e deriva `dry_run: false`;
- altera apenas os campos informados;
- valida se a note existe;
- valida se os campos existem no note type;
- limite de 50 notes por operacao;
- nao altera deck;
- nao altera scheduling;
- nao altera tags;
- nao recria cards/notes;
- nao deleta nada;
- em execucao real, qualquer conflito impede todas as escritas; falhas durante persistencia acionam rollback compensatorio.

Payload:

```json
{
  "operation_type": "update_note_fields",
  "confirmed_by_user": true,
  "payload": {
    "note_updates": [
      {
        "note_id": 1778103173630,
        "fields": {
          "Text": "A {{c1::<span class=\"kw\">membrana plasmática</span>::<span class=\"hint\">estrutura</span>}} controla trocas com o meio.",
          "Back Extra": "Use poucos grifos com <span class=\"kw\">palavras-chave</span> estruturantes."
        }
      }
    ],
    "dry_run": true
  },
  "requested_by": "gpt",
  "reason": "Grifar card existente sem alterar deck, tags ou scheduling"
}
```

No direct/apply v2, `expected_content_hash` e obrigatorio e fica diretamente no item ao lado de `note_id` e `fields`; `expected_mod`, `expected_usn` e `expected_model_id` tambem ficam nesse nivel. O caminho padrao usa as precondicoes publicadas do note na primeira operacao. `dry_run_operation_id` permanece somente para promover uma preview explicitamente solicitada.

Para grifar cards existentes, o GPT deve buscar os notes alvo, limpar wrappers visuais antigos, reescrever `Text` e `Back Extra` quando existirem, e enfileirar `update_note_fields`. O comando explicito do usuario para grifar um escopo claro conta como confirmacao para a operacao estetica, mas a alteracao ainda deve passar pela fila `organization`.

Wrappers visuais removidos pela utilidade local `strip_visual_wrappers(html)`:

- `span`;
- `font`;
- `b`, `strong`;
- `u`;
- `s`, `strike`, `del`;
- `mark`.

A utilidade preserva o conteudo interno e nao remove HTML estrutural/semantico como `img`, `br`, `a`, tabelas, listas, `sub`, `sup`, `i`, `em`, entidades HTML, midia e sintaxe Cloze.

### `get_reorganization_log`

Consulta o log local das movimentacoes reais registradas pelo addon.

Payload:

```json
{
  "operation_type": "get_reorganization_log",
  "confirmed_by_user": true,
  "payload": {
    "limit": 20
  },
  "requested_by": "gpt",
  "reason": "Consultar ultimas movimentacoes"
}
```

O `limit` e opcional, deve ser inteiro, e e limitado a `200`.

### `undo_reorganization`

Desfaz um batch especifico de movimentacao real.

Payload:

```json
{
  "operation_type": "undo_reorganization",
  "confirmed_by_user": true,
  "payload": {
    "batch_id": "orgop-20260505T213500845356Z-c3620996"
  },
  "requested_by": "gpt",
  "reason": "Desfazer movimentacao"
}
```

Regras:

- move os cards existentes de volta para o `old_deck` registrado no log;
- nao recria cards;
- nao deleta cards;
- nao altera fields ou midia;
- nao remove tags automaticamente;
- antes de desfazer, verifica se cada card ainda esta no `new_deck_id` registrado;
- se algum card estiver em outro deck, aborta e retorna conflito.

### `undo_last_reorganization`

Desfaz o ultimo batch real ainda nao desfeito.

Payload:

```json
{
  "operation_type": "undo_last_reorganization",
  "confirmed_by_user": true,
  "payload": {},
  "requested_by": "gpt",
  "reason": "Desfazer ultima movimentacao"
}
```

## Preservacao de scheduling

A movimentacao nao recria cards ou notes. A execucao local altera somente o deck do card existente, preferindo APIs do Anki quando disponiveis:

- `mw.col.set_deck(card_ids, deck_id)`, se existir;
- caso contrario, atualiza `card.did` e persiste com `update_cards`/`update_card`;
- como ultimo fallback, usa `update cards set did = ? where id = ?`.

Campos como `due`, `ivl`, `factor`, `reps`, `lapses`, `queue`, `odue`, `odid` e o `revlog` nao sao resetados pela operacao.

## Log local de reorganizacao

Movimentacoes reais (`dry_run: false`) de `move_cards_to_deck` e `move_notes_to_deck` sao registradas localmente no Mac/addon em:

`~/anki-gpt-files/organization_move_log.jsonl`

Cada linha e um JSON independente. Para movimentacao, ha uma linha por card:

```json
{
  "event_type": "move",
  "status": "moved",
  "batch_id": "orgop-20260505T213500845356Z-c3620996",
  "operation_id": "orgop-20260505T213500845356Z-c3620996",
  "operation_type": "move_cards_to_deck",
  "timestamp": "2026-05-05T18:35:01",
  "card_id": 1778016764561,
  "note_id": 1778016764561,
  "old_deck": "#UFPR::Teste GPT::Move Origem",
  "old_deck_id": 123,
  "new_deck": "#UFPR::Teste GPT::Move Destino",
  "new_deck_id": 456,
  "ord": 0,
  "template_name": "Card 1",
  "note_type": "Basic",
  "tags_added": ["organizado::movido_por_gpt"],
  "scheduling_before": {
    "queue": 0,
    "type": 0,
    "due": 123,
    "ivl": 0,
    "factor": 0,
    "reps": 0,
    "lapses": 0,
    "left": 0,
    "odue": 0,
    "odid": 0
  },
  "scheduling_after": {
    "queue": 0,
    "type": 0,
    "due": 123,
    "ivl": 0,
    "factor": 0,
    "reps": 0,
    "lapses": 0,
    "left": 0,
    "odue": 0,
    "odid": 0
  },
  "error": ""
}
```

Undo tambem escreve uma linha por card, com `event_type: "undo"` e `undone_batch_id` apontando para o batch original. Essa escolha evita editar linhas antigas do JSONL e mantem o log append-only.

`dry_run: true` nao grava log de movimentacao real.

## Regra de seguranca

Nenhuma operacao de escrita deve ser criada sem pedido claro do usuario, mas a API autenticada trata a propria criacao `direct` como autorizacao. Ela nao exige `confirmed_by_user` nem confirmacao posterior.

Operacoes desconhecidas tambem sao rejeitadas no backend remoto e, se chegarem ao addon por qualquer motivo, sao confirmadas como `failed`.

## Fluxo

1. Usuario pede uma alteracao com escopo claro.
2. GPT cria `direct`; se o usuario pediu simulacao, cria `preview`.
3. VPS cria arquivo de operacao com `status: pending`.
4. Addon local busca `/organization/operations`.
5. Addon executa a operacao dentro do Anki.
6. Addon reporta o resultado em `/organization/operations/result`.
7. VPS marca a operacao como `done`, `failed` ou `skipped`.

## Processamento manual no Anki

O addon local adiciona o menu:

`Tools > Anki GPT > Processar fila organization agora`

Esse comando chama `process_organization_queue()` imediatamente, sem depender apenas do hook `sync_did_finish`. Ao final, o Anki mostra um dialog simples com:

- operacoes buscadas;
- operacoes processadas;
- sucessos;
- falhas;
- operacoes ignoradas;
- resultados tecnicos enviados para a VPS.

O processamento manual segue as mesmas regras de seguranca: executa apenas `status: pending`, respeita `execution_mode`, precondicoes e recibos, e so altera os objetos autorizados no payload. `confirmed_by_user` nao controla a fila.

## Note-level vs card-level

Historicamente a query API expunha endpoints chamados `/cards/*`, mas os objetos eram derivados de notes serializadas. Isso e suficiente para busca textual e triagem, mas nao e suficiente para operacoes futuras que precisam preservar scheduling, porque scheduling pertence aos cards reais.

O snapshot enviado pelo addon agora preserva compatibilidade com os campos antigos e adiciona, em cada note, a lista `cards` com os cards reais gerados por ela.

Campos de card incluidos quando disponiveis:

- `card_id`;
- `note_id`;
- `ord`;
- `deck_id` / `did`;
- `deck_name`;
- `queue`;
- `type`;
- `due`;
- `ivl`;
- `factor`;
- `reps`;
- `lapses`;
- `left`;
- `odid`;
- `odue`;
- `original_deck_name`;
- `flags`;
- `data`;
- `mod`;
- `usn`;
- `template_name`.

Esses campos sao somente leitura. O snapshot nao altera scheduling, cards, notes ou tags.

## Endpoints de inspecao card-level

`GET /cards/info?ids=123,456`

Retorna informacoes reais dos card ids encontrados no snapshot, incluindo note resumida.

`GET /notes/info?ids=111,222`

Retorna note type/model, fields, tags, cards reais da note, decks atuais desses cards e o booleano `cards_in_multiple_decks`.

`GET /notes/cards?note_ids=111,222`

Retorna o mapeamento `note_id -> cards reais`.

`GET /cards/notes?card_ids=123,456`

Retorna o mapeamento `card_id -> note_id`.

`GET /cards/search-real`

Busca card-level baseada no snapshot. Filtros minimos:

- `deck`;
- `text` ou `q`;
- `tag`;
- `nid`;
- `card_id`;
- `limit`, padrao `100`, maximo `500`.

Limitacao importante: essa busca nao executa o parser nativo do Anki. Ela usa somente os dados serializados no snapshot (`compare_text`, fields, tags, deck/card ids). Queries complexas do Anki ainda precisam rodar dentro do Anki em etapa futura.

## Ainda nao implementado

- image occlusion;
- delete;
- alteracao de conteudo de cards/notes existentes;
- undo de criacao de notes/cards;
- remocao automatica de tags adicionadas por uma movimentacao quando o undo e executado;
- undo de tags de deck ideal; undo nao remove nem restaura `organizado::destino::*`;
- undo de criacao de decks.
