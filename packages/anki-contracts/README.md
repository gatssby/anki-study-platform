# Anki contracts

`note_preconditions.py` define o hash/precondicoes compartilhados pelo backend
e addon. `basic_to_cloze.py` concentra as validacoes estruturais da operacao
Basic -> Cloze; a reescrita semantica continua pertencendo ao GPT/Knowledge.

Os contratos OpenAPI canônicos ficam em `contracts/openapi`.
`note_preconditions.py` é a única implementação do material canônico e do
SHA-256 usados por leituras e pelo executor apply-v2. Os bundles copiam esse
módulo para o addon e para a API, sem dependência de pacote Python global.
