# Cronograma FO

App web local para acompanhar o cronograma da planilha **Cronogramas Extensivo UFPR 2026 - Federal Online.xlsx** (aba `02mar (30S)`).

## O que tem hoje

- Dashboard diario minimalista em tabela (6 slots do dia)
- Segundo quadro diario para Universo Narrado (UN) em fila sequencial
- Regra de atraso por disciplina/modulo no FO
- Ritmo separado por trilha (FO e UN)
- Slots do dia fixos: ao marcar vista, a linha continua na mesma aula
- Ate 3 aulas atrasadas recomendadas para compensacao
- Aba **Base de dados** com filtros e marcacao manual fora de ordem
- Barra de progresso geral (todas as aulas)
- Dark mode com toggle (preferencia salva no navegador)
- Download de backup da base (`.zip` com banco + uploads de revisão) pela interface
- Reimportacao da planilha sem perder historico de aulas vistas

## Requisitos (rodar local)

- Python 3.11+
- `pip`

## 1) Instalar dependencias

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) Importar planilha (primeira carga)

```bash
python3 scripts/import_schedule.py \
  --source fo \
  --xlsx "Cronogramas Extensivo UFPR 2026 - Federal Online.xlsx" \
  --sheet "02mar (30S)"
```

Banco criado em `data/cronograma.db`.

## 2.1) Importar Universo Narrado

Importe o FO primeiro. Depois:

```bash
python3 scripts/import_schedule.py \
  --source un \
  --csv "work/gpe_bridge/output/gpe_un_app_import_with_durations.csv"
```

O UN e distribuido automaticamente em dias uteis entre `2026-03-13` e a data final do FO.
Na interface, ele aparece como fila sequencial: o dashboard mostra sempre as proximas aulas nao vistas em ordem, e o ritmo esperado continua sendo calculado para terminar junto com o FO.
Por padrao, o primeiro dia do UN fica ancorado em `2026-03-13`.
O CSV oficial do app para o UN e o cronograma final filtrado gerado pelo pipeline `gpe_bridge`, ja com `lesson`/`list` e duracoes.
Se quiser sobrescrever manualmente essa data em algum import especifico, use:

```bash
python3 scripts/import_schedule.py \
  --source un \
  --csv "work/gpe_bridge/output/gpe_un_app_import_with_durations.csv" \
  --start-date "2026-03-13"
```

## 3) Rodar localmente

```bash
python3 main.py
```

Abrir: [http://127.0.0.1:8000](http://127.0.0.1:8000)

## 4) Backup e restore

### Baixar backup

- Na interface (Dashboard ou Base de dados), clique em **Baixar backup**.
- O arquivo baixado sera algo como `cronograma-backup-YYYYMMDD-HHMMSS.zip`.
- O backup inclui um snapshot consistente do banco SQLite, criado pela API de backup do SQLite, e a pasta persistente `data/review_questions/uploads/`, entao cobre:
  - aulas vistas (`lessons.is_seen`, `lessons.seen_at`)
  - exercicios salvos e exercicios feitos (`exercise_tasks`)
  - configuracoes e indisponibilidades da reprogramacao
  - questoes de revisao (`review_questions`, `review_question_attempts`)
  - imagens/anexos de questoes salvos em `data/`

### Restaurar backup

```bash
python3 scripts/restore_backup.py --backup /caminho/cronograma-backup-YYYYMMDD-HHMMSS.zip
```

- O script substitui `data/cronograma.db` pelo backup.
- Se o arquivo for `.zip`, ele tambem restaura `data/review_questions/uploads/`.
- Antes, ele cria backup de seguranca do banco atual (`*.pre-restore-...db`).

### Validar backup

```bash
python3 scripts/reprogram_schedule.py validate-backup
```

Ou validar um arquivo especifico:

```bash
python3 scripts/reprogram_schedule.py validate-backup \
  --backup data/backups/cronograma-backup-YYYYMMDD-HHMMSS.zip
```

## 4.1) Boot, criação e migração do banco

O boot normal do app apenas abre o banco em modo somente leitura e valida se o schema atual é compatível. Ele não cria tabelas, não executa `ALTER TABLE`, não normaliza dados e não migra arquivos legados. Se o banco estiver ausente ou incompatível, o app falha com diagnóstico e permanece sem alterar o arquivo.

Criação e migração são operações explícitas:

```bash
python3 scripts/init_or_migrate_db.py --db data/cronograma.db --check
python3 scripts/init_or_migrate_db.py --db data/cronograma.db --apply
```

`--check` não escreve. Use `--apply` somente após snapshot e revisão do diagnóstico.

### Hardening futuro de `daily_assignments`

A aplicação garante por transação que um mesmo `assigned_lesson_code` não seja
materializado em mais de um slot no mesmo `dashboard_date`. Um índice parcial
equivalente pode reforçar essa regra no banco:

```sql
CREATE UNIQUE INDEX idx_daily_assignments_unique_lesson_per_date
ON daily_assignments(dashboard_date, assigned_lesson_code)
WHERE assigned_lesson_code IS NOT NULL
  AND assigned_lesson_code <> '';
```

Esse índice não deve ser aplicado enquanto existirem snapshots históricos
duplicados preservados. Antes de promovê-lo, audite e trate o histórico em uma
migração explícita, valide o rollback sobre backup e teste concorrência entre os
workers. Até lá, a proteção canônica é a reserva transacional da home, a
verificação defensiva antes do commit e o validador rígido para a data atual e
datas futuras.

## 4.2) Reprogramacao auto-adaptavel

### Ver configuracoes atuais

```bash
python3 scripts/reprogram_schedule.py show-settings
```

### Definir prova e termino

```bash
python3 scripts/reprogram_schedule.py set-exam-date 2026-11-01
python3 scripts/reprogram_schedule.py set-target-finish-date 2026-10-18
```

Ou usar termino relativo:

```bash
python3 scripts/reprogram_schedule.py set-finish-offset-days 14
```

### Definir flags e teto diario

```bash
python3 scripts/reprogram_schedule.py set-flags \
  --include-weekends \
  --include-vacations \
  --cut-review-free \
  --preserve-english-cut \
  --auto-adapt

python3 scripts/reprogram_schedule.py set-capacity \
  --weekday 300 \
  --saturday 240 \
  --sunday 240
```

### Cadastrar indisponibilidades futuras

Dia unico indisponivel:

```bash
python3 scripts/reprogram_schedule.py add-unavailability \
  --date 2026-07-10 \
  --unavailable \
  --reason "compromisso"
```

Intervalo indisponivel:

```bash
python3 scripts/reprogram_schedule.py add-unavailability \
  --start-date 2026-07-15 \
  --end-date 2026-07-20 \
  --unavailable \
  --reason "viagem"
```

Capacidade parcial:

```bash
python3 scripts/reprogram_schedule.py add-unavailability \
  --date 2026-08-02 \
  --capacity-percent 50
```

Listar e remover:

```bash
python3 scripts/reprogram_schedule.py list-unavailability
python3 scripts/reprogram_schedule.py remove-unavailability 3
```

### Cortar ou restaurar aulas manualmente

```bash
python3 scripts/reprogram_schedule.py cut-lesson FIS1A12 --reason "baixo retorno"
python3 scripts/reprogram_schedule.py uncut-lesson FIS1A12
python3 scripts/reprogram_schedule.py list-cuts
```

### Dry-run

```bash
python3 scripts/reprogram_schedule.py \
  --exam-date 2026-11-01 \
  --target-finish-date 2026-10-18 \
  --include-weekends \
  --include-vacations \
  --cut-review-free \
  --preserve-english-cut \
  --auto-adapt \
  --dry-run
```

O relatório separa, por track, aulas com duração real e aulas estimadas. FO sem duração usa o fallback conservador de 45 minutos; UN usa 10 minutos, correspondente ao quartil superior observado nos itens UN com duração real (mediana próxima de 7–8 minutos). Para auditar uma sequência pedagógica específica, incluindo aulas já vistas ou cortadas:

```bash
python3 scripts/reprogram_schedule.py \
  --as-of-date 2026-09-05 \
  recalculate --dry-run \
  --diagnose-lesson-prefix POR2
```

O diagnóstico imprime código, status, data atual e data projetada em ordem pedagógica. A contagem `aulas_nao_alocadas_FO` pertence ao plano compartilhado FO + UN; `diagnostico_fo_isolado_sem_competicao_UN_nao_alocadas` é apenas a simulação FO isolada, sem consumo de capacidade pela UN.

### Apply

```bash
python3 scripts/reprogram_schedule.py \
  --exam-date 2026-11-01 \
  --target-finish-date 2026-10-18 \
  --include-weekends \
  --include-vacations \
  --cut-review-free \
  --preserve-english-cut \
  --auto-adapt \
  --max-daily-minutes-weekday 300 \
  --max-daily-minutes-saturday 240 \
  --max-daily-minutes-sunday 240 \
  --apply
```

O `apply` só prossegue quando toda a carga FO + UN cabe na capacidade diária configurada até a data-alvo. Em cenário inviável ele aborta antes do backup e de qualquer alteração. Quando viável, cria backup automático antes de alterar datas recomendadas e limpa os snapshots diários para a agenda ser reconstruída com o novo plano.

### Recalculo manual com configuracao persistida

```bash
python3 scripts/reprogram_schedule.py recalculate --dry-run
python3 scripts/reprogram_schedule.py recalculate --apply
```

## 5) Quando sairem aulas que hoje estao como "aguardando edital"

Processo simples:

1. Baixe a planilha atualizada do cursinho.
2. Substitua o arquivo antigo no projeto (ou passe o novo caminho no `--xlsx`).
3. Rode de novo:

```bash
python3 scripts/import_schedule.py --source fo --sheet "02mar (30S)"
```

Pronto. O import faz merge por slot do cronograma e preserva o historico das aulas ja marcadas.

No FO, o comando imprime um preflight antes de escrever e aborta em conflitos de identidade ou ocupação de slot. Por padrão, a reimportação preserva vistos, `seen_at`, cortes, agenda adaptativa, exercícios, assignments e itens UN; aulas ausentes na planilha também são preservadas. A substituição das datas da agenda só ocorre quando `--replace-schedule-dates` é informado explicitamente.

### 5.1 Sync semanal FO

O fluxo operacional fica em:

```bash
scripts/run_fo_full_sync.sh --dry-run
scripts/run_fo_full_sync.sh
```

O modo normal usa lock, cria snapshots SQLite consistentes antes das etapas que escrevem, atualiza metadados FO, reaplica a agenda adaptativa, valida assignments e exercícios e então chama os syncs de materiais. O `--dry-run` não aplica merge nem agenda e não executa o download real de PDFs; o fluxo de vídeos é chamado no modo dry-run suportado pelo próprio script.

## 6) Deploy 24/7 na sua VPS OCI (Ubuntu 22.04 ARM64)

Arquitetura atual de deploy:

- `cronograma_deploy.sh`: preflight, sincronização protegida e coordenação remota
- `scripts/remote_deploy_live.sh`: rebuild/recreate, validação do banco e healthcheck
- `cronograma_deploy`: wrapper para o deploy normal, que preserva a DB remota
- `Dockerfile`
- `docker-compose.yml`

### 6.1 Preparação inicial da VPS

Conectar:

```bash
ssh oracle-vps
```

Instalar Docker + Compose plugin (uma vez):

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker ubuntu
```

Saia e entre novamente no SSH para aplicar grupo `docker`.

Preparar a pasta remota:

```bash
sudo mkdir -p /opt/cronograma-fo
sudo chown -R ubuntu:ubuntu /opt/cronograma-fo
mkdir -p /opt/cronograma-fo/data /opt/cronograma-fo/imports
```

### 6.2 Instalar os comandos de deploy no Mac

Os wrappers locais ficam no projeto e podem ser apontados para o seu `PATH` via `~/bin`.
Se precisar reinstalar manualmente:

```bash
cd /caminho/para/anki-study-platform/apps/cronograma-fo
mkdir -p "$HOME/bin"
ln -sf "$(pwd -P)/cronograma_deploy" "$HOME/bin/cronograma_deploy"
```

Não há suporte funcional a `--reset-db`. O wrapper legado `cronograma_deploy_reset_db` não faz parte do fluxo suportado e não deve ser usado.

### 6.3 Deploy normal (preserva DB)

```bash
cronograma_deploy
```

Esse modo:

- roda preflight local e remoto
- valida a sintaxe Python local
- sincroniza o projeto com `rsync`, sem `--delete`
- exclui bancos SQLite e não envia `data/cronograma.db`
- cria snapshot remoto consistente antes do rebuild/recreate
- executa `docker compose up -d --build --force-recreate`
- só conclui depois de confirmar container ativo, processo principal disponível e HTTP 200 em `/database?track=FO`
- mostra os logs recentes e retorna erro se o healthcheck esgotar as tentativas
- não importa FO ou UN, não limpa diretórios e não executa prune

As exclusões do rsync normal incluem `.git/`, ambientes virtuais, caches Python, `node_modules/`, `backups/`, `state/`, `output/`, `work/`, `imports/`, `data/backups/`, bancos `*.db`/`*.sqlite*`, planilhas, CSVs, PDFs e arquivos temporários. Como não há `--delete`, arquivos remotos fora do conjunto enviado não são removidos automaticamente.

### 6.4 Operações explícitas com banco

Para baixar uma cópia atual de produção:

```bash
cronograma_deploy --pull-db
```

Esse fluxo cria um snapshot consistente do banco remoto, valida a cópia baixada e só então atualiza `data/cronograma.db` local. Se já houver um banco local, cria também um snapshot dele antes da substituição; a ausência do arquivo local é aceita.

O envio de banco é excepcional:

```bash
cronograma_deploy --with-db
```

`--with-db` valida o banco candidato, cria e valida snapshot remoto, compara exatamente o estado protegido, verifica uploads referenciados e gera relatório antes da confirmação `DEPLOY_DB`. A troca ocorre com o app parado; depois há validação, recreate e healthcheck, com rollback automático do snapshot em caso de falha.

Por padrão, qualquer perda ou regressão de estado aborta. `--force-db-overwrite` permite ultrapassar esse bloqueio somente junto de `--with-db` e exige a confirmação distinta `FORCE_DEPLOY_DB_LOSS`; é uma opção emergencial com risco explícito de perda.

Não existe modo suportado para apagar e reconstruir automaticamente o banco. Importações e migrações continuam sendo comandos explícitos e separados do deploy.

### 6.5 Personalização opcional

O script aceita override via variáveis de ambiente:

```bash
REMOTE_HOST=oracle-vps REMOTE_DIR=/opt/cronograma-fo cronograma_deploy
```

As configurações centrais ficam no topo de `cronograma_deploy.sh`:

- `PROJECT_DIR`
- `REMOTE_HOST`
- `REMOTE_DIR`
- `REMOTE_DB`
- `REMOTE_BACKUP_DIR`
- `LOCAL_DB`
- `REMOTE_UPLOADS_ROOT`
- `REMOTE_APP_URL`
- `COMPOSE_SERVICE`

O healthcheck remoto também aceita `CONTAINER_NAME`, `HEALTHCHECK_URL`, `HEALTHCHECK_MAX_ATTEMPTS`, `HEALTHCHECK_INTERVAL_SECONDS` e `HEALTHCHECK_HTTP_TIMEOUT_SECONDS`.

### 6.6 Nginx (reverse proxy)

Exemplo `/etc/nginx/sites-available/cronograma-fo`:

```nginx
server {
    listen 80;
    server_name 137.131.191.66;

    location / {
        proxy_pass http://127.0.0.1:18000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Ativar:

```bash
sudo ln -s /etc/nginx/sites-available/cronograma-fo /etc/nginx/sites-enabled/cronograma-fo
sudo nginx -t
sudo systemctl reload nginx
```

## 7) Como atualizar depois

Fluxo recomendado:

1. Ajuste o código localmente.
2. Rode os testes e um `cronograma_deploy --dry-run`.
3. Rode `cronograma_deploy`; o banco não será enviado.
4. Use `--pull-db` ou `--with-db` somente quando a operação de banco for intencional e revisada.

Os dados de progresso permanecem no volume `./data` quando você usa o modo normal.

Não apague `data/`, `data/backups/`, `backups/`, `state/`, `work/` ou `imports/` sem uma operação de manutenção explicitamente planejada. Esses diretórios podem conter banco, uploads, snapshots, estado de sync, artefatos de trabalho ou fontes de importação que o deploy normal deliberadamente preserva.
