# Setup VPS

- Base: `/home/ubuntu/anki-gpt-sync`.
- Scripts: `scripts/`; estado: `state/`; dados: `data/`; token: `tagging_token.txt` modo 0600.
- `start_query_api.sh` define umask 077, ativa read auth e inicia Python.
- tmux: `anki-query-api`; bind: `127.0.0.1:8767`.
- Nginx deve expor somente `<DOMAIN>:443`, redirecionar 80 e encaminhar headers padrão.

Antes de ativar: validar Python/SQLite FTS5, schema, token presente sem imprimi-lo, `nginx -t`, bind e firewall/NSG. Não exponha 8767 diretamente.

