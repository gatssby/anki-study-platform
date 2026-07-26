# Anki Study Platform

Monorepo privado e modular para o addon do Anki, a API Anki, o Cronograma FO,
os jobs do portal/transcrições e seus contratos.

Esta migração preserva inicialmente os runtimes e os caminhos de produção:

- API Anki: `/home/ubuntu/anki-gpt-sync`, bind local `127.0.0.1:8767`;
- Cronograma FO: `/opt/cronograma-fo`, porta publicada `18000`;
- transcrições: `/home/ubuntu/fo-transcricoes-system` e
  `/home/ubuntu/fo-transcricoes`;
- addon: carregado pelo runtime do Anki como bundle autossuficiente.

Dados persistentes, credenciais, sessões, mídia, transcrições, bancos, índices e
uploads não pertencem ao Git. Cada componente continua com deploy independente.

## Baseline importado

- `anki-gpt/`: addon e conhecimento locais, backend efetivamente ativo na VPS,
  ferramentas locais e testes artificiais;
- `cronograma-fo/`: aplicação local, confirmada como equivalente à implantação
  ativa, sem banco, sessão, materiais ou artefatos privados;
- `contracts/`, `ops/`, `scripts/` e `tests/`: áreas de consolidação progressiva.

A arquitetura final, os comandos de validação e os runbooks de implantação
ficam em `docs/`.
