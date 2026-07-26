# Contracts

Fontes canônicas de OpenAPI, JSON Schema e fixtures artificiais compartilhadas.

## OpenAPI

- `openapi/anki-api.openapi.json`: contrato completo da API.
- `openapi/gpt-organization-wrappers.openapi.json`: variante dos wrappers de
  organization.
- `openapi/gpt-action-compact.openapi.json`: fonte canônica editável do schema
  compacto do GPT Builder, com exatamente 23 operações.

`apps/anki-gpt/gpt-knowledge/schema gpt.json` é um symlink para o compacto
canônico. O bundle `anki-api` copia essa fonte para
`scripts/gpt-action-compact.openapi.json`, que é servido sem autenticação e sem
cache em `GET /openapi/gpt.json`. Execute `python scripts/validate_openapi.py`
antes de publicar qualquer variante.
