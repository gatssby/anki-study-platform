# Construção do recurrence_index.json

## Fontes usadas

O arquivo `structured/recurrence_index.json` foi construído a partir dos arquivos Markdown copiados para `raw/`:

- `biologia.md`
- `biologia_segunda_fase.md`
- `fisica.md`
- `geografia.md`
- `historia.md`
- `matematica.md`
- `portugues.md`
- `quimica.md`
- `quimica_segunda_fase.md`

Cada bloco em negrito com percentual foi tratado como um tema. Cada item listado abaixo do tema foi tratado como subtema.

## Normalizações aplicadas

- `fase` foi normalizada para `primeira_fase` ou `segunda_fase`.
- `peso_recorrencia` foi calculado como o percentual do tema dividido por 100.
- `peso_percentual` preserva o percentual original do Markdown.
- `subtemas[].estrelas` é a contagem literal de estrelas no subtema.
- `estrelas` no nível do tema é a maior contagem de estrelas entre os subtemas daquele tema.
- `fonte` aponta para o arquivo em `raw/` que originou cada item.
- `aliases` foram adicionados apenas quando ajudam a preservar grafias alternativas, fases em linguagem natural ou nomes comuns de busca.

## Ambiguidades preservadas

- Temas chamados `Outros` foram mantidos com o nome original, porque a fonte não detalha um agrupamento canônico único.
- Em História, `Atualidades` contém um subtema sem estrelas: `Sem detalhamento específico no material`. Ele foi preservado com `estrelas: 0`.
- A contagem de estrelas não foi comprimida para escala de 1 a 5. Ela preserva a intensidade literal do material original.
- Segunda fase foi normalizada como fase, mas a indicação `Medicina` permanece inferida pela fonte do arquivo, não como campo separado nesta versão.

