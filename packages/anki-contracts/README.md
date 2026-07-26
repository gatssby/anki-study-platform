# Anki contracts

Os contratos OpenAPI canônicos ficam em `contracts/openapi`. As normalizações
que precisam executar dentro do Anki continuam vendorizadas no addon; não há
dependência de pacote Python global. A extração adicional só deve ocorrer com
teste de equivalência addon/backend.
