# Pre-flight da migração

Captura iniciada em 2026-07-26, antes de mudanças de produção.

## Local

- GitHub CLI autenticado na conta `gatssby`; repositório ainda não criado.
- macOS 27.0 arm64; Python 3.14.6; Node 26.5.0; Git 2.54.0.
- Docker CLI não está instalado no Mac.
- volume local com aproximadamente 15 GiB livres no início.
- Anki principal estava aberto.
- addon ativo por symlink:
  `/Users/gatsby/Library/Application Support/Anki2/addons21/anki_gpt_sync`
  -> `/Users/gatsby/Workspace/Anki GPT/addon-local`.
- `Anki GPT`: Git incompleto, sem commits e sem remote; não reutilizado.
- `Cronograma FO`: sem repositório Git.

## VPS

- host lógico SSH: `oracle-vps`; Ubuntu arm64; Python 3.10.12.
- volume raiz com aproximadamente 4,3 GiB livres antes dos backups.
- Nginx 1.18.0 com configuração válida.
- API Anki em tmux `anki-query-api`, bind `127.0.0.1:8767`.
- Cronograma no container `cronograma-fo`, aplicação em `8000`, publicada em
  `18000`, com mounts persistentes de `data` e `imports`.
- `fo-queue-api.service` e `fo-vps-worker.service` ativos.
- cron ativo para watchdog da API Anki e sincronização FO.
- diretórios ativos:
  `/home/ubuntu/anki-gpt-sync`, `/opt/cronograma-fo`,
  `/home/ubuntu/fo-transcricoes-system` e `/home/ubuntu/fo-transcricoes`.

## Saúde e integridade iniciais

- API Anki: `/health`, `/version` e leitura autenticada de decks responderam.
- snapshot: 565 decks, 23.127 cards e 18.694 notas.
- Cronograma: páginas `/` e `/database` responderam HTTP 200.
- banco do Cronograma: `integrity_check=ok`; 3.026 aulas.
- fila de transcrições: `integrity_check=ok`; 1.070 itens.
- índice FTS: `integrity_check=ok`; 1.017 documentos.
- `aulas_index.tsv`: 1.160 linhas de dados, contrato de 18 colunas.

Nenhuma coleção Anki foi alterada durante esta captura.
