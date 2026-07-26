# Operações

Operações remotas começam em `pending`. No schema 3, `execution_mode` é canônico e separado de `status`: `direct` aplica na próxima consulta do add-on e `preview` apenas simula. O default configurável único é `ANKI_GPT_DEFAULT_EXECUTION_MODE` no backend, com fallback `direct`; respostas de criação e `GET /organization/operations` publicam `default_execution_mode`. O add-on não tem preferência concorrente. `dry_run` é derivado (`direct=false`, `preview=true`) para compatibilidade. Combinações incompatíveis retornam erro. `confirmed_by_user` permanece apenas como metadado legado.

Operações antigas sem `execution_mode` inferem o modo de `dry_run`. Se ambos faltarem em um tipo historicamente compatível com dry-run, o leitor usa `preview` para não converter uma operação antiga em escrita real.

`update_note_fields` valida o lote inteiro antes da primeira escrita, faz commit após sucesso e rollback compensatório em falha. `failed` significa zero efeito residual confirmado; `partially_applied` indica rollback incompleto/efeito possível. Scheduling é excluído do hash porque update de campo não o altera.

## Direct/apply v2 de `update_note_fields`

O caminho padrão cria a operação direct já com as precondições publicadas do note:

```json
{
  "note_id": 123,
  "expected_content_hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "expected_mod": 42,
  "expected_usn": 7,
  "expected_model_id": 1607392319
}
```

Envie esses valores diretamente em cada `note_update`; nenhuma preview anterior é necessária. O fluxo abaixo permanece somente para uma preview explicitamente solicitada que depois será promovida:

```json
{
  "dry_run_operation_id": "orgop-20260722T212942358959Z-90e85578"
}
```

`POST /organization/note-field-updates-create` recupera o `updates_id` original, combina cada update com `result.apply_preconditions` por `note_id` e devolve outro `updates_id` com `ready_for_apply_v2: true`. Esse novo ID deve ser usado em `POST /organization/update-note-fields-create` com `execution_mode: "direct"` e `dry_run: false`.

No formato manual, cada item é plano:

```json
{
  "note_id": 123,
  "fields": {"Text": "valor novo"},
  "expected_content_hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "expected_mod": 42,
  "expected_usn": 7,
  "expected_model_id": 1607392319
}
```

`expected_content_hash` é obrigatório no apply v2. Os outros três valores são incluídos quando o runtime do Anki os disponibiliza. Qualquer conflito impede todas as escritas do lote; falha durante persistência aciona rollback compensatório.

Wrappers GET legados que criam operação foram migrados para POST de forma retrocompatível. Os GETs continuam disponíveis durante a transição e retornam headers `Deprecation`, `Sunset`, `Link` e `Warning`.

Receipts locais cobrem `done` e `partially_applied`: operação, modo, resultado e precondições têm SHA-256 canônico, TTL de 90 dias e replay do reporte técnico sem segundo apply. O add-on envia resultados por `POST /organization/operations/result`; `/confirm` continua como alias legado, não como autorização manual. Receipt expirado ou divergente bloqueia.

## Auditoria, correção localizada e composição

Resultados compactados preservam IDs de notes/cards, campos alterados, erros/warnings por item, precondições e hashes antes/depois. `update_note_fields` permanece atômico por lote: um conflito de item impede as escritas desse lote, evitando estado parcial; uma correção posterior deve criar outra operação apenas com os IDs problemáticos.

O runtime ainda não armazena indefinidamente os valores completos anteriores de cada campo. Portanto, os hashes permitem auditoria e detecção, mas não uma reversão automática por campo. Uma reversão futura exigirá armazenamento criptografado/limitado por retenção dos valores anteriores ou um log append-only separado; isso não foi misturado à mudança de modo.

Não há contrato de etapas compostas dentro de um único `operation_id`. O add-on já busca até dez operações pendentes por ciclo, então normalização, grifo e reorder podem ser operações ordenadas separadas e consumidas na mesma sincronização, mas não têm transação única entre etapas. Uma composição futura precisará de `steps[]`, ordem, resultado por etapa, política de falha e recibo da operação principal.
