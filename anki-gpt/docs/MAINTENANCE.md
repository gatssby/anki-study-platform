# Manutenção

Diário: health/ready, espaço e último snapshot. Semanal: diagnostics, FTS verify, logs/erros e processo único. Mensal: retention dry-run, backups, certificados e dependências/patches.

Nunca aplique cleanup sem revisar ativa/anterior, pending operations e logs necessários ao incidente. Registre hash antes/depois e rollback. Use pause de auto publish em janelas do Anki e remova somente ao concluir validação.

Política padrão: 14 dias para logs ativos, 20 snapshots, três gerações mais ativa/anterior, 90 dias para operações/receipts e backups, dois dias para staging e um dia para mídia temporária. Logs gzip históricos e backups exigem opt-in. Auditorias e FTS ativo nunca entram no cleanup automático.

Auto publish local usa `disabled`, `manual`, `after_anki_sync` ou `always`, com default `manual`. Produção local validada em modo explícito `manual`; remover `pause_auto_publish` não dispara publicação por si só.
