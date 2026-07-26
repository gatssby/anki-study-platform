# Fluxo FO: Materiais, PDFs E Transcricoes

Use este arquivo sempre que o pedido depender de aulas, frentes ou materiais do Federal Online. Antes de auditar, criar, reescrever, normalizar, grifar ou reordenar cards com base em FO, consulte os materiais/PDFs e, quando util, as transcricoes.

## Ordem De Autoridade

1. PDF/texto extraido de material Federal Online.
2. Transcricao FO da aula correspondente.
3. Decks Anki e cards existentes.
4. Inferencia ou memoria do modelo.

Se PDF/texto FO e transcricao entrarem em conflito, o PDF/texto FO vence. Use transcricao para complementar exemplos, linguagem de aula, enfases e sequencia didatica, nao para contrariar o PDF sem outra fonte superior.

## Uso Das Fontes Nos Cards

Materiais, PDFs e transcricoes FO orientam auditoria, validacao, lacunas, prioridade, reescrita e relatorio. Eles nao devem aparecer como muleta textual dentro do card.

Ao transformar conteudo FO em card, remova referencias como "a aula", "o material", "o PDF", "a transcricao", "segundo o professor", "no Federal Online", "foi citado" ou "consta no resumo". O resultado deve ser uma afirmacao, pergunta ou cloze autonomo, compreensivel sem a fonte aberta.

Ruim:

`Segundo o PDF de História I, a derrama era a cobranca forcada de impostos atrasados.`

Bom:

`A {{c1::<span class="kw">derrama</span>}} era a cobranca forcada de impostos atrasados quando a arrecadacao do ouro nao atingia a meta exigida pela Coroa.`

Ruim:

`A aula 05 cita que os quilombos eram formas de resistencia a escravidao.`

Bom:

`Os {{c1::<span class="kw">quilombos</span>}} eram formas de resistencia coletiva a escravidao, formados por comunidades de escravizados fugitivos.`

A fonte deve ser citada no relatorio ao usuario, nao no campo `Text`, `Back Extra`, frente ou verso. Excecao: cite a fonte no card apenas quando a propria cobranca for autoria, documento historico, banca, edital, historiografia, comparacao entre fontes ou interpretacao de trecho especifico.

## Materia, Frente E Aula

Nao confunda:

- `materia="História"`: disciplina ampla.
- `frente="História I"`: frente/curso especifico dentro da materia.
- `aula_number=6` ou `q="História I Aula 06"`: aula especifica, mais apropriada para transcricoes.

Para buscar PDFs/materiais, comece pela frente/material geral. Para buscar transcricoes, pode usar frente + aula.

Exemplo correto para História I Aulas 01 a 08:

1. Buscar materiais por `História I`.
2. Buscar variante `História I Resumo`.
3. Selecionar o PDF geral da frente, por exemplo `História/História I - Resumo.pdf`, se retornado.
4. Extrair texto das paginas relevantes.
5. Depois buscar transcricoes de `História I Aula 01` ate `História I Aula 08` como complemento.

Exemplo incorreto:

- comecar por `getFoMaterials(q="História I Aula 06")`;
- declarar que nao existe PDF porque a busca por aula especifica nao retornou material;
- usar transcricao antes de tentar o PDF geral da frente.

## Busca De Materiais FO

Fluxo minimo:

1. Chame `getFoMaterials(q="<frente>")`.
2. Se o resultado for amplo ou vazio, chame variantes plausiveis do material geral:
   - `getFoMaterials(q="<frente> Resumo")`;
   - `getFoMaterials(q="<frente> - Resumo")`;
   - filtros por `area`, `portal_subject` ou `output_subject`, se o schema ativo expuser e a disciplina for clara.
3. Leia os metadados retornados: `relative_path`, titulo, materia/area, frente, status, paginas/tamanho quando disponiveis.
4. Escolha o PDF principal antes de consultar transcricoes, quando houver candidato claro.

Nunca diga que o PDF FO nao existe antes de testar busca por frente/material geral e variantes plausiveis. Se `getFoMaterials(q="História I")` encontra `História/História I - Resumo.pdf`, esse PDF existe para o fluxo e deve ser acessado antes de criar operacoes.

## Como Escolher O PDF Principal

Prefira, nesta ordem:

1. PDF cujo titulo/relative_path bate com a frente exata, como `História I`.
2. PDF de resumo/apostila geral da frente, como `História I - Resumo.pdf`.
3. PDF oficial mais abrangente da mesma materia/frente quando nao houver resumo especifico.
4. Outro material FO apenas se o principal nao existir ou estiver inacessivel.

Se houver dois candidatos equivalentes e a escolha afetar conteudo, relate a ambiguidade e use ambos ou peça confirmacao. Se um candidato e claramente o resumo geral da frente, use-o sem bloquear por excesso de cautela.

## Extracao De Texto Do PDF

Depois de escolher o PDF, extraia texto antes de criar ou preparar operacoes. Nao baseie criacao/rewrite/reorder apenas no nome do PDF.

Chamada esperada quando o schema expuser extracao dentro de `getFoMaterial`:

`getFoMaterial(relative_path="História/História I - Resumo.pdf", extract_text=true, start_page=8, page_limit=2)`

Compatibilidade com o schema OpenAPI local atual, quando a extracao estiver separada:

`getFoText(relative_path="História/História I - Resumo.pdf", page=8, limit=2, max_chars=20000)`

Use paginas pequenas e progressivas:

- primeiro, extraia sumario/primeiras paginas se precisar mapear aulas;
- depois, extraia as paginas provaveis de cada aula/tema;
- aumente `page_limit` apenas quando o trecho vier cortado;
- registre paginas usadas no relatorio.

Se a extracao de texto falhar, tente endpoint de PDF (`getFoPdf`) apenas se o fluxo exigir inspecao visual ou recuperacao alternativa. Se o PDF esperado existe, mas nenhum texto foi acessado, pare antes de criar operacoes.

## Busca De Transcricoes FO

Use transcricoes depois de localizar/consultar o PDF principal, ou em paralelo quando o usuario pediu explicitamente aula especifica e a busca de PDF ja esta encaminhada.

Fluxo:

1. Use `getFoTranscripts` para metadados e descoberta.
2. Filtre por `materia`, `frente`, `aula_number`, `tipo`, `status="done"` e `exists=true` quando possivel.
3. Use `q` para busca textual quando o identificador exato nao estiver claro.
4. Depois chame `getFoTranscript` para o texto da transcricao escolhida.

Exemplos:

`getFoTranscripts(q="História I Aula 06")`

`getFoTranscripts(materia="História", frente="História I", aula_number=6, status="done", exists=true)`

Chamada esperada quando a Action aceitar ID:

`getFoTranscript(id=498, max_chars=20000)`

Compatibilidade com o schema OpenAPI local atual, quando `getFoTranscript` exigir caminho:

`getFoTranscript(relative_path="<relative_path retornado por getFoTranscripts>", max_chars=20000)`

Nao use `materia="História I"` para filtrar transcricoes se o schema separa materia e frente. Use `materia="História"` e `frente="História I"`.

## Criterios De Bloqueio

Pare antes de criar preview ou operacao real quando:

- `getFoMaterials` retornou PDF esperado, mas o PDF/texto ainda nao foi acessado;
- ha conflito material/transcricao e o PDF nao foi usado para resolver;
- o usuario pediu base em aulas FO especificas, mas nenhuma fonte FO foi consultada;
- a busca foi feita apenas por aula especifica e nao por frente/material geral;
- a extracao de texto falhou e nao ha fonte FO alternativa suficiente;
- qualquer lote de cards falhou em fluxo de deck grande.

Nesses casos, informe exatamente o que falta: material, relative_path, paginas, transcricao ou lote de cards.

## Exemplo: História I Aulas 01 A 08

Para um pedido como "faça o fluxo completo no deck X com base em História I Aulas 01 a 08":

1. Buscar cards/deck por API conforme `05_fluxo_decks_grandes.md` se o deck for grande.
2. Chamar `getFoMaterials(q="História I")`.
3. Se necessario, chamar `getFoMaterials(q="História I Resumo")`.
4. Se retornado, selecionar `História/História I - Resumo.pdf`.
5. Extrair texto do PDF por paginas relevantes antes de qualquer operacao:
   - `getFoMaterial(relative_path="História/História I - Resumo.pdf", extract_text=true, start_page=8, page_limit=2)`;
   - ou, no schema com extrator separado, `getFoText(relative_path="História/História I - Resumo.pdf", page=8, limit=2, max_chars=20000)`.
6. Buscar transcricoes das aulas 01 a 08:
   - `getFoTranscripts(q="História I Aula 01")`;
   - `getFoTranscripts(q="História I Aula 02")`;
   - repetir ate `getFoTranscripts(q="História I Aula 08")`.
7. Para cada transcricao escolhida, chamar `getFoTranscript`.
8. Cruzar cards existentes com PDF primeiro, transcricao depois.
9. Preparar reescritas, normalizacoes, criacoes ou reorder apenas apos fontes consultadas.
10. Criar as operacoes propostas com `execution_mode: "direct"` e `dry_run: false`, salvo pedido explicito de preview.
11. Informar operation IDs e que uma unica consulta/sincronizacao do addon aplica o conjunto; nao pedir confirmacao redundante.

## Como Relatar Fontes Usadas

Sempre informe:

- consultas de materiais feitas, incluindo queries principais;
- `relative_path` do PDF usado;
- paginas extraidas ou intervalo aproximado;
- transcricoes usadas, com ID ou `relative_path` quando disponivel;
- quando uma transcricao foi usada apenas como complemento;
- conflitos encontrados e qual fonte venceu;
- lacunas ou limitacoes de acesso.

Formula curta:

`Fontes FO usadas: PDF História/História I - Resumo.pdf, paginas 8-9; transcricao História I Aula 06, id 498, como complemento. Em conflito, apliquei PDF > transcricao.`
