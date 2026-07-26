# Relatório final da migração

Data da janela: 2026-07-26

Repositório: `gatssby/anki-study-platform`

Visibilidade: privada

Branch: `main`

## 1. Resultado geral

`SUCESSO`

O monorepo foi criado com histórico Git novo, publicado como repositório
privado, testado localmente e implantado por componente nos paths existentes.
O pipeline FO real controlado, incluindo merge, agenda, PDFs, vídeos e
healthcheck, terminou com `exit_code=0`. Os dados persistentes passaram pelas
verificações pós-cutover e nenhum rollback foi necessário.

## 2. Repositório e estrutura

URL: <https://github.com/gatssby/anki-study-platform>

```text
apps/
  anki-gpt/
    addon-local/
    local-tools/
    gpt-knowledge/
  cronograma-fo/
services/
  anki-api/
jobs/
  fo-portal-sync/
  fo-video-sync/
  fo-transcription-index/
  cronograma-reprogramming/
packages/
  anki-contracts/
  fo-contracts/
  safe-io/
contracts/
  fixtures/
  jsonschema/
  openapi/
ops/
  anki-api/
  cronograma-fo/
  shared-fo/
docs/
scripts/
tests/
.github/workflows/
```

Os runtimes continuam separados: API Anki em
`/home/ubuntu/anki-gpt-sync`, Cronograma em `/opt/cronograma-fo`,
transcrições em seus diretórios existentes e addon dentro do Anki.

## 3. Histórico criado

1. `252deda` — `chore: import sanitized production baselines`
2. `142ec77` — `refactor: modularize runtimes and shared contracts`
3. `b744ddd` — `ops: add component-specific deployment bundles`
4. `740c9dc` — `ci: add monorepo validation workflows`
5. `b5a8471` — `fix: tighten FO contract type validation`
6. `051f9a0` — `ops: add Cronograma container healthcheck`
7. `c0d9f20` — `fix: isolate FO runner dependencies and propagate failures`
8. `019ffbd` — `ci: allow manual validation runs`
9. `f5a524a` — `ci: fail security scan only on high severity`
10. `c2caf90` — `fix: extend FO index collection window`
11. `4ef498d` — `ops: validate only Cronograma runtime dependencies`
12. `51af8f9` — `docs: document architecture deployment and rollback`
13. `HEAD` — finalização das evidências de produção deste relatório.

Não houve force push. Os repositórios incompletos/ausentes dos diretórios
originais não foram reutilizados.

## 4. Baselines e divergências

| Componente | Baseline escolhido | Divergência tratada |
|---|---|---|
| Addon | addon local realmente carregado pelo Anki | fonte importada é byte a byte idêntica à instalação ativa |
| Backend Anki | produção, seguida da integração testada das melhorias locais | cache de busca normal existia apenas localmente e foi preservado |
| Cronograma | produção/local | fontes funcionais eram equivalentes; testes locais foram incorporados |
| Job portal FO | versão do Cronograma | contém proteção de sessão, gravação atômica e `fo_session_security` |
| Transcrições | produção | serviços e dados permaneceram externos, sem mudança de layout |
| OpenAPI | variante local mais nova, validada contra o runtime | variantes antigas foram preservadas em `legacy-production` |
| `aulas_index.tsv` | contrato de produção com 18 colunas | ganhou schema, fixture e validação sem quebrar v1 |
| Manifesto FO | formato de produção compatível | ganhou `schema_version=1` nas novas escritas; ausência antiga é tratada como v1 |

A matriz detalhada está em `BASELINE_MATRIX.md`. Nenhuma variante importante
foi descartada silenciosamente.

## 5. Integrações e consolidações

Fluxo validado:

```text
Portal FO
  -> coletor do Cronograma
  -> aulas_index filtrado e aulas_index.tsv
  -> merge SQLite do Cronograma
  -> agenda adaptativa
  -> materiais/manifesto e vídeos
  -> fila e serviços de transcrição
  -> índice FTS
  -> API Anki
  -> addon Anki
```

Consolidações executadas:

- uma implementação canônica de
  `fo_sync_materials_from_portal_materiais.py`, baseada na versão segura;
- contratos compartilhados para índice e manifesto FO;
- fonte canônica para OpenAPI completa, wrapper e schema compacto;
- escrita atômica, JSON/hash canônico e validação segura de paths em
  `packages/safe-io`;
- normalizações e contratos Anki organizados em pacote próprio;
- manifests de dependência separados para API, Cronograma, jobs, testes e
  runtime do addon;
- bundles independentes para API Anki, Cronograma e addon.

Wrappers/compatibilidades mantidos:

- `apps/anki-gpt/remote-backend` aponta para `services/anki-api`;
- caminhos antigos dos jobs FO apontam para as implementações canônicas;
- os bundles dereferenciam symlinks e preservam o layout plano da VPS;
- addon e `local-tools/anki_publish.sh` continuam irmãos;
- paths de produção continuam como defaults configuráveis;
- variantes OpenAPI históricas permanecem preservadas;
- legado perigoso não é incluído nos bundles de deploy.

O asset de favicon duplicado foi mantido nos dois caminhos exigidos pelo
runtime web. Ele não representa implementação divergente.

## 6. Código legado

- `remote-backend/server.py`: preservado nos originais/backups, marcado como
  legado e fora do deploy;
- `cronograma_deploy_reset_db`: preservado, mas excluído do bundle;
- backups de scripts, bundles históricos e `GPT_BUILDER_PUBLICAR`: preservados
  fora do código implantável;
- wrappers e paths antigos: mantidos durante o período de estabilização.

## 7. Segurança e conteúdo Git

O repositório possui 272 arquivos rastreados antes deste relatório. O maior
blob é um favicon de aproximadamente 1,5 MiB; nenhum blob supera 10 MiB.

Entraram no Git:

- código-fonte e testes;
- templates e assets públicos necessários ao runtime;
- schemas OpenAPI/JSON Schema;
- fixtures exclusivamente artificiais;
- manifests de dependência;
- Dockerfile/Compose;
- scripts de build, deploy e rollback;
- workflows de CI e documentação.

Ficaram fora do Git:

- `.env`, tokens, cookies, storage states, sessões e `rclone.conf`;
- bancos SQLite reais, coleção Anki e índices persistentes;
- mídia, transcrições, uploads e materiais privados;
- PDFs e planilhas privadas;
- logs, network logs, checkpoints e outputs;
- `data`, `state`, `work`, `files`, `imports` e backups brutos;
- ambientes virtuais, caches e artefatos Playwright;
- bundles gerados em `dist`.

As exceções permitem schemas/fixtures artificiais JSON, TSV e CSV e assets
PNG necessários. Varreduras do estado atual e de todo o histórico não
encontraram segredos; não há blobs grandes inesperados.

## 8. Dependências e variáveis

Dependências de sistema formalizadas: Python, Node.js, Docker/Compose,
Playwright/Chromium, FFmpeg, rclone e Nginx. Dependências Python ficaram
separadas por runtime. O host do Cronograma usa `.venv` isolado com acesso
controlado aos pacotes do sistema exigidos pela automação existente.

Somente nomes de variáveis, nunca valores:

- API Anki: `ANKI_GPT_ACTION_LOG_BACKUP_COUNT`,
  `ANKI_GPT_ACTION_LOG_MAX_BYTES`, `ANKI_GPT_BASE_DIR`,
  `ANKI_GPT_CARD_BATCH_SIZE`, `ANKI_GPT_DEFAULT_EXECUTION_MODE`,
  `ANKI_GPT_FAIL_ON_DECK_DIVERGENCE`, `ANKI_GPT_HOST`,
  `ANKI_GPT_MAX_BATCH_BYTES`, `ANKI_GPT_MAX_JSON_BODY_BYTES`,
  `ANKI_GPT_MAX_SYNC_BODY_BYTES`, `ANKI_GPT_OPERATION_MAX_AGE_SECONDS`,
  `ANKI_GPT_OPERATION_RECEIPT_TTL_SECONDS`,
  `ANKI_GPT_REQUEST_SOCKET_TIMEOUT_SECONDS`, `ANKI_GPT_PORT`,
  `ANKI_GPT_REQUIRE_READ_AUTH`, `ANKI_GPT_SCRIPTS_DIR`,
  `ANKI_GPT_STATE_DIR`, `ANKI_GPT_TAGGING_TOKEN`,
  `ANKI_GPT_TAGGING_TOKEN_FILE`, `ANKI_GPT_UFPR_2027_DIR`,
  `FO_AULAS_INDEX_PATH`, `FO_PDF_WATERMARKS`, `FO_SEARCH_INDEX_PATH`,
  `FO_TRANSCRIPTS_DB_PATH`, `FO_TRANSCRIPTS_ROOT`.
- Cronograma/jobs: `COMPOSE_SERVICE`, `CONTAINER_NAME`,
  `CRONOGRAMA_DB_PATH`, `CRONOGRAMA_DEPLOY_LIB_ONLY`,
  `CRONOGRAMA_PORT`, `CRONOGRAMA_PYTHON`, `DB_PATH`,
  `FEDERAL_ONLINE_EMAIL`, `FEDERAL_ONLINE_LOGIN_URL`,
  `FEDERAL_ONLINE_PASSWORD`, `HEALTHCHECK_HTTP_TIMEOUT_SECONDS`,
  `HEALTHCHECK_INTERVAL_SECONDS`, `HEALTHCHECK_MAX_ATTEMPTS`,
  `HEALTHCHECK_URL`, `HTTP_URL`, `INDEX_TIMEOUT`, `LOCAL_DB`,
  `MATERIALS_DIR`, `MAX_INDEX_AGE_HOURS`, `MERGE_AMBIGUOUS`,
  `PROJECT_DIR`, `REMOTE_APP_URL`, `REMOTE_BACKUP_DIR`, `REMOTE_DB`,
  `REMOTE_DEPLOY_LIB_ONLY`, `REMOTE_DIR`, `REMOTE_HOST`,
  `REMOTE_UPLOADS_ROOT`, `RUN_ID`, `STATE_DIR`.

## 9. Testes locais e builds

| Escopo/comando | Aprovados | Falhas | Ignorados/avisos |
|---|---:|---:|---:|
| `python -m pytest -q apps/anki-gpt/tests` | 119 | 0 | 2 avisos de depreciação do jsonschema |
| `python -m pytest -q apps/cronograma-fo/tests` | 83 | 0 | 0 |
| `python -m pytest -q tests` | 6 | 0 | 0 |
| `node apps/cronograma-fo/tests/dashboard_scroll_state_test.js` | 1 | 0 | 0 |
| `python scripts/validate_openapi.py` | 3 contratos | 0 | 0 |
| `ruff` em erros críticos | aprovado | 0 | 0 |
| `mypy` nos pacotes compartilhados | aprovado | 0 | 0 |
| `bandit` | 0 alto | 0 alto | 3 médios e 29 baixos documentados |
| varredura de segredos/arquivos grandes | aprovada | 0 | 0 |
| sintaxe Bash e compilação Python | aprovada | 0 | 0 |

Contratos OpenAPI: API completa com 42 operações, wrapper com 6 e compacto
com 23. Testes de contrato índice/manifesto: 6 aprovados. O índice implantado
pré-cutover validou 1.160 linhas; o manifesto validou 87 itens.

Bundles:

- API Anki: 23 arquivos, aproximadamente 656 KiB;
- Cronograma: 120 arquivos, aproximadamente 4,9 MiB;
- addon: 6 arquivos no bundle e ZIP validado com 5 arquivos.

O build Docker do Cronograma foi aprovado na VPS. O smoke test usou um SQLite
temporário inicializado/migrado apenas no diretório descartável e obteve HTTP
200 em `/` e `/database`.

O `pip check` global do venv com `--system-site-packages` encontrou
`pygobject` sem `pycairo`, dependência gráfica do host que não pertence a
nenhum runtime deste monorepo. O gate de deploy passou a importar
explicitamente todos os módulos exigidos pelos manifests da aplicação e jobs,
evitando tanto esse falso bloqueio quanto uma validação genérica silenciosa.

GitHub Actions:

- Repository Safety: aprovado;
- Contracts: aprovado;
- Cronograma FO + Docker build: aprovado;
- Anki GPT: aprovado depois de ajustar o gate Bandit para bloquear achados
  altos e manter médios/baixos documentados.

Os quatro workflows foram disparados manualmente no commit `51af8f9` depois do
cutover e concluíram com sucesso.

## 10. Deploy, serviços e configuração

Alterados:

- código/scripts da API em `/home/ubuntu/anki-gpt-sync`, sem tocar em
  `state`, `data`, snapshots, mídia, tokens, logs, bancos ou transcrições;
- código/container do Cronograma em `/opt/cronograma-fo`, preservando mounts e
  dados persistentes;
- runner FO para Python isolado e propagação correta de falhas;
- healthcheck do container do Cronograma.

Parados/reiniciados:

- container `cronograma-fo`, recriado pelos deploys e saudável;
- tmux `anki-query-api`, reiniciado após o deploy da API.

Permaneceram ativos e não foram reconfigurados:

- `fo-queue-api.service`;
- `fo-vps-worker.service`;
- Nginx, cuja configuração não foi alterada;
- crontabs existentes;
- diretórios e serviços de transcrição.

Nginx validou com `nginx -t`. Não houve mudança de DNS, certificados ou
domínio. A API continua no bind de loopback esperado em 8767, sem listener
indevido em 8766. O Cronograma continua publicado na porta 18000 para o
upstream existente.

## 11. Backups e restauração

Backup local seletivo:

`/Users/gatsby/Workspace/Anki Study Platform Migration Backups/20260726T190824Z`

Backup inicial da VPS:

`/home/ubuntu/migration-backups/anki-study-platform-20260726T190824Z`

Backups de deploy do Cronograma:

- `/opt/cronograma-fo/backups/monorepo-20260726T193203Z`;
- `/opt/cronograma-fo/backups/monorepo-20260726T193247Z`;
- `/opt/cronograma-fo/backups/monorepo-20260726T193751Z`;
- `/opt/cronograma-fo/backups/monorepo-20260726T211745Z`;
- `/opt/cronograma-fo/backups/monorepo-20260726T211843Z`.

Backup do deploy da API:

`/home/ubuntu/anki-gpt-sync/backups/monorepo-20260726T193317Z/scripts.tgz`

Os snapshots SQLite iniciais de Cronograma, fila e FTS passaram por
`integrity_check=ok`; o bundle inicial possui `SHA256SUMS`. Para restaurar:

1. parar apenas o componente afetado;
2. validar `SHA256SUMS`;
3. usar `ops/<componente>/rollback.sh` ou restaurar o tar correspondente;
4. restaurar SQLite somente se integridade/contagens exigirem;
5. validar Compose/Nginx/imports;
6. iniciar o runtime anterior;
7. repetir healthchecks, integridade e contagens.

O pipeline final criou ainda:

- `cronograma-pre-fo-merge-20260726T211931Z-3921136.db`;
- `cronograma-pre-adaptive-20260726T211931Z-3921136.db`;
- `cronograma-pre-adaptive-20260726-211931-985998.db`;
- backup privado do índice anterior e do checkpoint após timeout em
  `/home/ubuntu/migration-backups/anki-study-platform-20260726T190824Z/pre-fo-index-promotion`.

O target anterior do addon foi registrado antes do cutover:

```text
/Users/gatsby/Library/Application Support/Anki2/addons21/anki_gpt_sync
-> /Users/gatsby/Workspace/Anki GPT/addon-local
```

Depois que o Anki foi fechado e os hashes/bundle foram revalidados, o symlink
foi atualizado para:

```text
/Users/gatsby/Library/Application Support/Anki2/addons21/anki_gpt_sync
-> /Users/gatsby/Workspace/Anki Study Platform/apps/anki-gpt/addon-local
```

## 12. Validação end-to-end

### API Anki

- `/health`, `/version` e `/ready` autenticado: HTTP 200;
- schemas completo e wrapper: HTTP 200;
- autenticação sem credencial em `/decks`: HTTP 401; com credencial: HTTP 200;
- snapshot atual: 564 decks, 23.132 cards e 18.699 notas;
- leitura de transcrições: HTTP 200;
- ausência controlada de transcrição: HTTP 404
  `fo_transcript_not_available`, com metadados da aula e estado degradado;
- `/ready` confirmou `fts_integrity=ok`;
- OpenAPI completa com 42 operações e wrapper com 6 operações.

O snapshot inicial tinha 565 decks, 23.127 cards e 18.694 notas. A diferença
foi rastreada a dois eventos normais `anki_sync_did_finish` no Anki principal:
o addon publicou o snapshot atual às 18:22:29 BRT. Não houve despacho ou
execução de operação de organização depois das 17:00 BRT, e nenhum teste desta
migração escreveu na coleção principal. O snapshot mais novo foi preservado,
pois restaurar o anterior descartaria atividade legítima do Anki.

### Cronograma

- `/` e `/database`: HTTP 200;
- container saudável e sem reinícios após o deploy;
- banco antes do pipeline: 3.026 aulas, 114 atribuições diárias,
  11 questões de revisão e 5 tentativas;
- banco depois do pipeline: 3.026 aulas, 121 atribuições diárias,
  11 questões de revisão e 5 tentativas;
- o acréscimo de 7 atribuições foi a saída esperada do gerador adaptativo;
- `integrity_check=ok`, `/` e `/database` HTTP 200;
- container `running/healthy`, zero reinícios;
- Nginx válido; API em `127.0.0.1:8767`, sem listener 8766, Cronograma em
  18000 conforme o Compose existente.

### Pipeline FO e transcrições

- dry-run completo: aprovado;
- a primeira coleta real atingiu o timeout antigo de 90 minutos sem promover
  o checkpoint parcial; o índice anterior permaneceu ativo;
- o checkpoint de 647 itens foi salvo e retomado pelo mecanismo nativo com
  stdout redirecionado para arquivo privado e janela de 3 horas;
- índice promovido atomicamente: 707 linhas, schema v1, sendo 695 `ok`,
  11 erros transitórios de stream e 1 aula ainda não lançada;
- merge dry-run/aplicação: `ambiguous=0`, 636 atualizações, 3 registros
  mantidos com metadados anteriores por erro, 1 mantido por `not_launched` e
  108 sem correspondência; nenhuma aula foi removida;
- índice geral: 1.160 linhas/18 colunas, contrato aprovado;
- manifesto de materiais: schema v1, 87 itens, contrato aprovado;
- PDFs: etapa concluída com `exit_code=0`;
- vídeos: 2 baixados, 2 enviados, 0 falhas, 81 `not_launched` ignorados e
  4 sem URL ignorados de forma explícita;
- fila depois do pipeline: 1.070 itens e 15 issues, inalterada;
- FTS depois do pipeline: 1.017 documentos, inalterado;
- `fo-queue-api.service` e `fo-vps-worker.service`: ativos;
- degradação controlada pós-pipeline: HTTP 404
  `lesson_indexed_but_transcript_not_in_queue`, com metadados da aula;
- healthcheck final de container, HTTP e serviços: aprovado.

### Addon

- 119 testes automatizados, incluindo contratos, autenticação, snapshots,
  idempotência, concorrência, atomicidade, main thread e localização do script;
- pacote gerado e verificado;
- os quatro arquivos executáveis principais são idênticos aos do addon ativo;
- evidência anterior do mesmo código no perfil descartável
  `Anki GPT Validation`: 16/16 testes e probe de main thread aprovado;
- a coleção principal não foi alterada;
- symlink atualizado para o monorepo com o Anki fechado;
- carregamento do novo path aguarda apenas o reinício manual do Anki.

## 13. Preservação de dados

Confirmado:

- banco do Cronograma íntegro, 3.026 aulas preservadas;
- mounts `data` e `imports` preservados; o path opcional `uploads` não existia
  no runtime e continuou fora do bundle;
- coleção Anki não foi usada por testes; sua mudança de contagens foi
  explicada por sincronizações normais do Anki principal;
- mídia existente não foi apagada; o publicador continuou usando
  `--no-delete`;
- diretórios de transcrição preservados; fila com 1.070 itens;
- FTS íntegro com 1.017 documentos;
- sessões/storage states permaneceram fora do Git, com modo `0600`;
- índices e manifestos validados após a escrita atômica;
- 9,7 GiB livres na VPS ao final.

## 14. Passos manuais restantes

1. Abrir primeiro o perfil descartável `Anki GPT Validation`, executar o
   harness e confirmar todos os testes.

   No console de debug do Anki, com esse perfil aberto:

   ```python
   import importlib
   addon = importlib.import_module("anki_gpt_sync")
   harness = importlib.import_module("anki_gpt_sync.validation_harness")
   harness.run_disposable_validation(addon, addon.organization_module)
   ```

2. Só então reiniciar no perfil principal; não executar operações de escrita
   durante o primeiro smoke test.
3. Autorizar o novo repositório privado no conector GitHub do Codex/ChatGPT.
4. Atualizar manualmente o GPT Builder com os contratos/Knowledge canônicos.
5. Se workflows futuros precisarem de deploy, cadastrar no GitHub somente os
   secrets documentados; nenhum valor foi copiado nesta migração.

Rollback do addon: com o Anki fechado, restaurar o symlink para
`/Users/gatsby/Workspace/Anki GPT/addon-local` e reabrir o perfil descartável
antes do principal.

## 15. Riscos residuais e próxima etapa

- os achados Bandit médios são uso de URL em ferramenta diagnóstica e SQL
  dinâmico montado por fragmentos internos; revisar/hardening futuro;
- os 29 achados baixos são principalmente cleanup tolerante, subprocess sem
  shell e tratamento de artefatos legados;
- o addon ainda depende de um reinício manual do Anki para mudar de path;
- o GPT Builder e o conector GitHub exigem ação manual externa;
- wrappers, layouts antigos e backups devem permanecer durante o período de
  estabilização;
- acompanhar logs/healthchecks e contagens por alguns ciclos de sincronização
  antes de qualquer limpeza definitiva.

Próxima etapa recomendada: observar uma semana de uso, executar novamente a
matriz de contratos/healthchecks e só então planejar remoção de wrappers ou
legado, nunca na mesma mudança.
