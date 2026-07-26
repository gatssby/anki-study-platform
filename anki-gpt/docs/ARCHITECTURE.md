# Arquitetura

`GPT → HTTPS <DOMAIN>:443 → Nginx → 127.0.0.1:8767 → query_api.py → geração/FTS/filesystem`

`Anki main thread → snapshot destacado → worker HTTP → /sync/full → geração temporária → hashes/manifest → current.json`

Leituras usam `GenerationStateCache` e cache derivado por generation ID. Publicação grava quatro JSONs, valida bytes/hashes, renomeia o diretório e troca o ponteiro atomicamente. FTS5 é um SQLite separado, reconstruído por arquivo temporário.

Escritas são operações persistidas na VPS e aplicadas pelo addon somente após confirmação, idade e precondições. Objetos vivos do Anki nunca atravessam workers.

