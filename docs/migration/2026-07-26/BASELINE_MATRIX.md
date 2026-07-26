# Matriz de baseline

| Componente | Local | Produção | Baseline provisório | Justificativa |
|---|---|---|---|---|
| Addon | symlink para `Anki GPT/addon-local` | executa dentro do Anki local | local ativo | é a instalação realmente carregada |
| Backend Anki | contém cache adicional de busca normal | ativo em `127.0.0.1:8767` | produção | menor mudança inicial; melhoria local preservada para commit testado |
| OpenAPI | contrato direct/preview mais novo | variante anterior | local, após validação | acompanha comportamento já validado do modo direto |
| Cronograma | fonte local com testes | fonte implantada equivalente | produção/local idênticos | comparação de arquivos não encontrou diferenças funcionais |
| Job portal FO | duas cópias quase iguais | ambas presentes nos runtimes | Cronograma | inclui proteção de sessão e gravação atômica |
| Transcrições | integração indireta | serviços systemd ativos | produção | runtime separado e não será movido nesta fase |
| `aulas_index.tsv` | consumidor no backend | produtor no Cronograma | contrato observado em produção | preserva 18 colunas e versão inicial compatível |

Nenhuma divergência importante é descartada. A variante local do backend fica
preservada nos diretórios originais e no backup da migração.
