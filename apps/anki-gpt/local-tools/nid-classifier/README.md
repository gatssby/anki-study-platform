# Anki NID classifier automation

Automação local para reclassificar **do zero** o mesmo universo do CSV anterior, em lotes de 40, usando o Anki GPT no ChatGPT web.

## Estado desta versão

- universo: lido de `~/Desktop/anki_nid_classifications.csv` e validado em **2457 NIDs únicos**
- resultados antigos são ignorados; o CSV antigo serve apenas como lista-semente de NIDs
- prioridade inicial: **416 NIDs**, exatamente na ordem solicitada
- destinos possíveis: **21**
- novo destino: `#UFPR::Biologia::• Patologia`
- evita `• Histologia Animal`, `• Fisiologia Animal` e `• Embriologia` como raiz, salvo notes realmente multitemáticas
- batch padrão: 40
- batch atômico: só grava quando todos os NIDs do batch retornam e passam na validação
- ao detectar **quota / usage limit / message limit / rate limit / teto**, para imediatamente com exit code `75`
- o batch atingido pela quota não é gravado e fica como primeiro da retomada
- saída nova: `~/Desktop/anki_nid_classifications_v2.csv`
- searches para o Anki: `~/Desktop/anki_move_searches_v2.txt`
- estado: `~/Desktop/anki_nid_classifier_state_v2.json`

## Atualizar o código local

```bash
cd "/Users/gatsby/Workspace/Anki Study Platform"
git pull --ff-only
```

## Instalação

```bash
cd "/Users/gatsby/Workspace/Anki Study Platform/apps/anki-gpt/local-tools/nid-classifier"
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

O script tenta usar o Brave do macOS primeiro; o Chromium do Playwright é fallback.

## URL do Anki GPT

```bash
export ANKI_CLASSIFIER_GPT_URL='COLE_AQUI_A_URL_DO_ANKI_GPT'
```

Para persistir:

```bash
echo "export ANKI_CLASSIFIER_GPT_URL='COLE_AQUI_A_URL_DO_ANKI_GPT'" >> ~/.zshrc
source ~/.zshrc
```

## Primeiro login

```bash
source .venv/bin/activate
python3 classify.py --login-only
```

Faça login na janela aberta; volte ao Terminal e pressione Enter.

## Validar antes de rodar

Com o CSV antigo ainda em `~/Desktop/anki_nid_classifications.csv`:

```bash
python3 classify.py --validate-config
```

Esperado:

```text
universe_nids: 2457
priority_nids: 416
destinations: 21
queue unique: 2457
primeiro NID: 1770673921578
último NID prioritário: 1619621797108
Patologia: SIM
```

Conferir os primeiros 80 da fila sem consumir GPT:

```bash
python3 classify.py --dry-run
```

## Começar do zero

```bash
python3 classify.py --reset
```

O `--reset` apaga apenas os arquivos `v2`. O CSV antigo, usado como universo-semente, não é alterado.

## Retomar após quota

Quando a quota voltar:

```bash
python3 classify.py
```

A retomada é derivada dos NIDs já gravados no CSV `v2`; o batch que encontrou a quota volta inteiro.

## Trocar a lista de prioridade

Copie a nova lista para o clipboard e rode:

```bash
pbpaste | python3 update_lists.py --priority-from-stdin --validate
```

Ou:

```bash
python3 update_lists.py --priority-from-file ~/Desktop/prioridade.txt --validate
```

## Alterar destinos

Adicionar:

```bash
python3 update_lists.py --add-destination '#UFPR::Biologia::• Patologia' --validate
```

Remover:

```bash
python3 update_lists.py --remove-destination '#UFPR::Biologia::• Patologia' --validate
```

## Trocar o universo de cards

Por padrão o universo é extraído do CSV antigo:

```text
~/Desktop/anki_nid_classifications.csv
```

Para usar outro CSV:

```bash
python3 classify.py --seed-csv ~/Desktop/outro.csv --expected-universe 0 --reset
```

Ou uma lista explícita de NIDs:

```bash
python3 classify.py --all-nids ~/Desktop/todos_nids.txt --expected-universe 0 --reset
```

`--expected-universe 0` desativa a trava de 2457 e só deve ser usado quando a mudança for intencional.

## Versionar alterações nas listas

```bash
cd "/Users/gatsby/Workspace/Anki Study Platform"
git add apps/anki-gpt/local-tools/nid-classifier
git commit -m "Update Anki NID classifier lists"
git push
```
