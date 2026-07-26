# Segurança

TLS 1.2+, loopback, token em header, compare constante, limites de corpo, Content-Type JSON, no-store/nosniff, traversal/symlink guards, HTML ativo rejeitado e logs redigidos.

Não exponha tokens, collection, logs ou paths. Não aceite token em URL. Mantenha runtime em 0600/0700. `/health` e `/version` são públicos; dados, mídia, ready e diagnostics exigem token quando read auth está ativa.

Patches locais preparados cobrem default vhost de rejeição, HSTS, rate limit do vhost Anki e firewall mínimo, mas não estão aplicados. O host é compartilhado com n8n e containers nas portas 3000/18000; qualquer mudança exige janela e console OCI.

Logs novos não devem conter header/token, corpo, preview, HTML ou nomes de campos. Os dois gzip M2 contêm conteúdo histórico de cards, mas não apresentaram token/credencial nos classificadores; destino depende de autorização. Nunca execute cleanup de gzip/backups sem flags explícitas e revisão.
