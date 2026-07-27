# Addon

Versão 3.0.0. `__init__.py` registra menu/hook, coleta snapshot e coordena workers. `organization.py` normaliza/aplica operações e rollback. `html_utils.py` fornece normalização auxiliar.

A janela “Anki GPT - Operações em andamento” mostra `Modo` e `Estado` em colunas separadas. Operações `direct` aparecem como “Aplicação real” e nunca oferecem confirmação manual posterior; previews e operações antigas são identificadas por `execution_mode` ou pelo `dry_run` legado. O add-on consome ambas na mesma fila, aplica direct uma única vez com recibo idempotente e reporta o resultado técnico à VPS.

Diagnóstico privado:
`~/Library/Application Support/Anki2/addon-data/anki_gpt_sync/state/addon_runtime.json`
com versão/hash/load time. Coleta e apply devem ocorrer na main thread; workers
fazem rede, serialização, filesystem/subprocesso. O hook `sync_did_finish` pode
publicar automaticamente apenas se a política permitir; use
`state/pause_auto_publish` durante manutenção controlada.

Na inicialização, o addon migra automaticamente dados do antigo
`~/anki-gpt-files`. Diretórios são movidos/mesclados sem sobrescrever arquivos
canônicos; symlinks válidos são copiados e removidos sem alterar o alvo;
symlinks quebrados são removidos quando possível; arquivos que ocupem o caminho
legado são preservados e ignorados. Falhas de migração, criação de diretório ou
logging são enviadas a stderr e nunca bloqueiam o import.

Estado validado em 2026-07-12: addon 3.0.0 carregado com política explícita `manual`, hashes `483619e5...` e `395b0456...`, menu registrado e sem publicação automática na reabertura.

Correção ativa: escritas de `addon_runtime.json` derivam `auto_publish_paused` diretamente do filesystem, registram também `auto_publish_mode` e `auto_publish_configured`, e não bloqueiam o Anki se a escrita do diagnóstico falhar. Reload final validado com `__init__.py` hash `36a2b657...`.

Autenticação do snapshot consulta primeiro `ANKI_GPT_TAGGING_TOKEN` e depois o
arquivo canônico `tagging_token.txt`. Ausência conhecida não é convertida em
erro inesperado: o fluxo manual explica a configuração necessária e o fluxo
automático apenas registra a causa, sem popup repetitivo.

Snapshot, fila organization e publicação de mídia compartilham um lock de
pipeline. Cliques manuais e hooks automáticos concorrentes são coalescidos no
pipeline ativo. Respostas HTTP 502/503/504 usam no máximo três tentativas com
backoff curto, reutilizando o payload já materializado; a fila e a publicação
de mídia só começam depois da confirmação do snapshot.

Após mudança do addon, reinicie manualmente o Anki e confirme hash; nunca force reload durante operação em voo.
