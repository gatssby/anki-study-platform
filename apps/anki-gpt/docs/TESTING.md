# Testes

Comando principal: `pytest -q`. A suíte usa fixtures artificiais, servidores locais e temporários; não depende da coleção principal, internet ou token real.

Também execute `py_compile`, `bash -n`, `scripts/validate_openapi.py`, secret check e Ruff crítico. Mypy cobre o núcleo tipado; Bandit high severity roda em Python 3.12/3.10. Testes Qt/Anki e GPT são manuais.

A suíte cobre logs, propostas Nginx, ignore/CI, compatibilidade GET/POST, auto publish, equivalência da busca, receipts, observabilidade e retenção. Bandit 1.8.5 não é compatível com o AST do Python 3.14; use a matriz CI ou Python 3.12, sem atualizar a dependência de produção.

Smoke de produção nesta rodada é somente leitura: health/version/ready/diagnostics/auth/contagens. Não executar sync, apply, processamento de fila ou reload do Anki.
