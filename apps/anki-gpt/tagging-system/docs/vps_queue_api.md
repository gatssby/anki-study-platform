# Fila de tagging na VPS

## Objetivo

A query API da VPS registra operacoes de tagging pendentes para execucao futura pelo addon local do Anki. Esta etapa nao aplica tags; ela apenas cria, lista e confirma operacoes.

## Armazenamento

As operacoes ficam em arquivos JSON auditaveis:

`/home/ubuntu/anki-gpt-sync/state/tagging/operations/<operation_id>.json`

Cada operacao preserva tipo, alvo, tags, origem, confirmacao explicita, status e eventual confirmacao de execucao enviada pelo addon.

## Endpoints

Todos os endpoints de tagging exigem o header `X-Tagging-Token`.
O valor esperado vem da variavel de ambiente `ANKI_GPT_TAGGING_TOKEN`.
Se a variavel nao estiver definida, a API responde `503 tagging_token_not_configured`.
Se o header estiver ausente ou incorreto, a API responde `401 unauthorized`.

`POST /tagging/operations`

Cria operacao pendente. Requer `confirmed_by_user: true`.

`GET /tagging/operations`

Lista operacoes. Por padrao retorna apenas `pending_addon_execution`. Use `status=all` para listar tudo.

`POST /tagging/operations/confirm`

Confirma execucao futura feita pelo addon. Status finais aceitos: `applied`, `partially_applied`, `failed`.

## Regra de seguranca

A API recusa criacao se `confirmed_by_user` nao for exatamente `true`. O GPT deve obter confirmacao explicita do usuario antes de chamar o endpoint de criacao.
