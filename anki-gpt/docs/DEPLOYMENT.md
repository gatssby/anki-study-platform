# Deploy

1. Registrar PID/hashes/geração/health.
2. Rodar testes, OpenAPI, compile e shell.
3. Criar backup remoto com timestamp e SHA-256.
4. Copiar somente arquivos necessários para `scripts/`.
5. Compilar remotamente.
6. Reiniciar somente tmux `anki-query-api`.
7. Validar health, auth, ready, diagnostics, contagens e hash.

Docs/CI não precisam ir à VPS. Mudança do addon requer restart manual separado. Nunca troque token ou schema GPT implicitamente.

Mudanças locais desta consolidação devem ser separadas por risco:

1. backend compatível: headers/observabilidade e GET→POST;
2. addon: política auto publish e receipts, com pausa e restart manual;
3. busca: cache por geração, benchmark/equivalência e rollback independente;
4. Nginx/firewall: janela separada, `nginx -t`, sem deploy junto do backend.

Não publicar receipts apenas em um lado: backend deve aceitar o receipt antes do addon ser recarregado. Não remover GET enquanto o GPT Builder não usar POST e o período Sunset não terminar.
