# Sincronização

Snapshot contém notes, cards, decks e índice de mídia destacados. A VPS publica uma geração schema 3 com quatro arquivos e manifest SHA-256; `current.json` é trocado somente ao final.

“Sincronizar tudo” materializa na main thread, envia em worker, processa fila na main thread e publica snapshot final apenas se houve mudança. Publicação de mídia é coalescida.

Uma operação `direct` pendente é recebida, aplicada e reportada nesse mesmo ciclo; não existe sincronização intermediária de preview nem confirmação manual. `preview` continua executando sem escrita quando solicitado explicitamente. Várias operações pendentes podem ser consumidas no mesmo ciclo, respeitando o limite da fila e a idempotência por operação.

Não teste na coleção principal. Para shadow, use objetos artificiais/diretório temporário. Cancelamento antes da troca do ponteiro não ativa geração parcial.

Política local ativa: `disabled`, `manual`, `after_anki_sync` e `always`, default e recomendação `manual`. Toda publicação verifica `~/anki-gpt-files/state/pause_auto_publish` imediatamente antes de rede/mídia, e uma resposta de snapshot só é sucesso com `generation_id`.

Estado validado em 2026-07-12: addon 3.0.0 carregado com política explícita `manual`. Remover `pause_auto_publish` não dispara publicação por si só; a publicação automática pós-sync continua bloqueada em modo `manual`. Não execute sync manual durante manutenção.
