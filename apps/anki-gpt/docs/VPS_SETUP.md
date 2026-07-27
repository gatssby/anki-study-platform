# Setup VPS

- Base: `/home/ubuntu/anki-gpt-sync`.
- Scripts: `scripts/`; estado: `state/`; dados: `data/`; token: `tagging_token.txt` modo 0600.
- `rebuild_state.sh` publica uma nova geração por troca atômica de `current.json`;
  não reinicia a query API. O cache recarrega a geração no próximo request,
  preservando conexões e uploads em andamento.
- `POST /sync/full` serializa publicações e deduplica o mesmo conteúdo por
  `snapshot_content_hash`, devolvendo o `generation_id` já confirmado em retry.
- `start_query_api.sh` define umask 077, ativa read auth e inicia Python.
- tmux: `anki-query-api`; bind: `127.0.0.1:8767`.
- Nginx deve expor somente `<DOMAIN>:443`, redirecionar 80 e encaminhar headers padrão.

Antes de ativar: validar Python/SQLite FTS5, schema, token presente sem imprimi-lo, `nginx -t`, bind e firewall/NSG. Não exponha 8767 diretamente.
