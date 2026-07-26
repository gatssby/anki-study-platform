# Rollback

Código: restaurar backup validado, compilar, reiniciar somente backend e testar. Estado: validar geração anterior completa antes de trocar `current.json`; nunca apontar para staging. FTS: preservar índice atual e substituir somente por rebuild verificado.

Após rollback, confirmar um listener loopback, read auth, 590/21.660/17.903, FTS 1.017 e GPT funcional. O relatório de cada deploy contém o backup exato.

Patches desta consolidação não foram implantados. O rollback local e os blobs baseline estão em `audits/2026-07-12-final-consolidation/ROLLBACK.md`; Nginx deve ser restaurado e validado com `nginx -t` antes de qualquer reload. Cleanup executado em dry-run não exige rollback de dados.
