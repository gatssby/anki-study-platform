# Criterios De Analise

## Card Bom

Um card bom cobra uma unidade pequena, relevante e recuperavel.

Ele deve:

- ter pergunta clara;
- exigir uma resposta objetiva;
- testar conhecimento com utilidade provavel para UFPR, ENEM ou a trilha indicada;
- evitar pistas que entreguem a resposta;
- ser revisavel em poucos segundos;
- ser compreensivel isoladamente, sem depender da aula, PDF, transcricao ou material aberto;
- manter conteudo essencial sem excesso de contexto;
- estar alinhado ao Federal Online ou a uma fonte superior na hierarquia.

## Card Ruim

Um card ruim aumenta custo de revisao sem ganho proporcional.

Sinais comuns:

- pergunta vaga ou multipla;
- resposta longa demais;
- cloze que esconde uma frase inteira sem orientar a recuperacao;
- cloze hint que entrega a resposta;
- duplicidade com outro card melhor;
- cobranca de detalhe pouco provavel sem justificativa;
- formulacao que permite respostas diferentes;
- card correto, mas inutil para prova;
- card que depende de contexto ausente;
- card que transforma a fonte em muleta textual, como "segundo o PDF", "a aula afirma", "o material cita" ou "a transcricao menciona";
- card mantido apenas por familiaridade com o deck;
- erro conceitual, cronologico, terminologico ou de escopo.

## Midia Quebrada

Nao classifique um card como ruim, `precisa_melhorar` ou equivalente por suspeita fraca de midia quebrada.

Trate midia como problema real apenas quando houver evidencia estrutural clara, como `broken_media` confirmado pela API, arquivo ausente apos publicacao/indexacao atualizada, `src` vazio/invalido, link impossivel de resolver ou campo visual indispensavel que de fato nao carrega.

Se houver duvida, nao penalize o card por esse motivo. Ausencia, suspeita ou atraso de midia nao deve dominar o julgamento do card; avalie primeiro conteudo, cobranca, recuperabilidade, custo e integracao.

## Cinco Eixos

Analise cards, lotes e decks pelos cinco eixos abaixo.

### 1. Conteudo

Verifique se o fato, conceito, relacao, procedimento ou excecao esta correto e completo no nivel necessario.

Priorize o Federal Online quando houver divergencia com decks antigos ou materiais secundarios.

### 2. Cobranca

Julgue se o item representa algo que a prova provavelmente cobra.

Pergunte se o card ajuda a reconhecer uma questao, eliminar alternativa, executar procedimento, interpretar enunciado ou evitar erro recorrente.

### 3. Recuperabilidade

Avalie se o usuario consegue recuperar a resposta a partir do front ou do cloze sem depender de adivinhacao.

Um card pode estar correto e ainda falhar se a pergunta nao delimitar o tipo de resposta esperado.

Tambem falha em recuperabilidade quando depende de referencia externa para fazer sentido. A fonte pode aparecer no relatorio da analise, mas o front, verso, `Text` e `Back Extra` devem funcionar como material autonomo de revisao.

### 4. Custo

Estime o custo de revisao: tempo, esforco, ambiguidade e chance de esquecimento artificial.

Cards longos, sobrepostos ou muito abstratos aumentam backlog mesmo quando sao verdadeiros.

### 5. Integracao

Verifique se o card se encaixa no deck, no lote, na fonte e nos cards vizinhos.

Procure lacunas, duplicidades, versoes concorrentes, reaproveitamento mal feito e perda de contexto entre decks.

## Granularidade De Recall

Ao analisar um card Cloze, nao avalie apenas se cada informacao merece ser mantida. Avalie tambem se cada informacao merece gerar um recall independente.

Uma note pode conter duas ou mais informacoes relevantes e ainda assim estar atomizada demais se cada cloze numerado gerar um card separado sem ganho proporcional para a prova.

Diferencie duas decisoes:

1. **O conteudo merece existir?** Julgue pela recorrencia, importancia conceitual e utilidade para UFPR, ENEM ou a trilha indicada.
2. **O conteudo merece recall proprio?** Julgue se recuperar esse fragmento isoladamente traz ganho real suficiente para justificar mais uma revisao recorrente no Anki.

Use clozes diferentes (`c1`, `c2`, `c3`...) quando os fragmentos:

- sao cobrancas independentes e plausiveis em prova;
- podem ser esquecidos separadamente de forma relevante;
- exigem raciocinios ou associacoes diferentes;
- continuam fazendo sentido quando recuperados isoladamente;
- tem importancia ou recorrencia suficiente para justificar cards adicionais.

Prefira o mesmo numero de cloze quando os fragmentos formam uma unica associacao semantica e o objetivo real e recuperar o conjunto.

Sinais de recalls independentes desnecessarios:

- dois clozes diferentes sao apenas partes complementares da mesma relacao;
- acertar um praticamente implica lembrar o outro;
- a prova tende a cobrar a associacao completa, nao cada elo isolado;
- separar os clozes multiplica revisoes de um detalhe de recorrencia media ou baixa;
- o segundo card adiciona custo de backlog sem aumentar de forma relevante a capacidade de resolver questoes.

Exemplo:

`Os peroxissomos {{c1::degradam peroxido de hidrogenio (H2O2)}} pela acao da {{c2::catalase}}.`

Se o conhecimento relevante for a associacao unica `peroxissomo -> catalase -> degradacao de H2O2`, prefira:

`Os peroxissomos {{c1::degradam peroxido de hidrogenio (H2O2)}} pela acao da {{c1::catalase}}.`

Isso preserva todo o conteudo, mas gera um unico recall da associacao completa.

Nao transforme esta regra em fusao automatica. Conteudos de alta recorrencia, pares que a prova pode inverter, listas cujos elementos precisam ser recuperados individualmente, etapas independentes, formulas com componentes de funcao distinta ou conceitos que frequentemente aparecem isolados podem justificar recalls separados.

Ao revisar decks, procure explicitamente notes em que seja possivel reduzir o numero de cards gerados sem remover conhecimento relevante. Trate isso como uma forma de controle de custo e de granularidade, ao lado de overkill, duplicidade e atomizacao excessiva.

## Julgando Decks E Lotes

Ao avaliar um lote ou deck, nao conclua apenas pela media de qualidade. Identifique padroes.

Observe:

- temas cobertos e lacunas;
- proporcao de cards essenciais, secundarios e descartaveis;
- duplicidades internas;
- divergencias com o Federal Online;
- cards longos ou atomizados demais;
- notes com recalls independentes demais para a relevancia do conteudo;
- padroes de cloze ruim;
- excesso de cards com baixa utilidade de prova;
- impacto provavel no backlog.

Quando a amostra for pequena, diga que o diagnostico e parcial.

Se o usuario pedir uma decisao sobre o deck inteiro, informe o escopo consultado antes do veredito: filtros, quantidade de cards, decks envolvidos e limite da amostra.

## Reaproveitamento Entre Decks

Ao comparar decks, trate reaproveitamento como decisao editorial, nao como copia automatica.

Um card de outro deck deve ser reaproveitado quando:

- cobra conteudo essencial ainda nao coberto;
- esta mais claro que a versao atual;
- reduz duplicidade ao substituir versoes piores;
- esta alinhado ao Federal Online ou a fonte oficial;
- tem custo de revisao aceitavel.

Nao reaproveite apenas porque o card existe em um deck antigo, tem maturidade no Anki, esta em um deck com nome confiavel ou parece completo.

Quando houver cards semelhantes, prefira a melhor unidade de cobranca. Se nenhum card for bom, recomende reescrita em vez de simples migracao.

## Evidencia, Padrao E Hipotese

Separe os niveis de certeza:

- Card visto: conclusao baseada em card concreto retornado pela API, com ID, deck ou texto quando disponivel.
- Padrao inferido: recorrencia observada em varios cards do lote, mas ainda limitada pela amostra consultada.
- Hipotese de revisao: suspeita plausivel que exige mais cards, fonte ou metadados antes de virar decisao.

Use linguagem proporcional:

- "Neste card..." para evidencia direta.
- "Na amostra consultada..." para padrao inferido.
- "Pode haver..." ou "vale verificar..." para hipotese.

Nao use hipotese como base para exclusao, migracao em massa ou conclusao sobre o deck inteiro.

## Nao Generalizar Cedo

Nao julgue um deck inteiro por um ou dois cards.

Antes de recomendar exclusao ampla, migracao em massa ou mudanca de politica, busque amostra suficiente pela API ou diga que a conclusao e provisoria.

Se os dados forem parciais, proponha a proxima consulta necessaria em vez de agir com excesso de confianca.
