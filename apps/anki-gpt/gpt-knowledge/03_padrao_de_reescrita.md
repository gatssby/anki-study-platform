# Padrao De Reescrita

## Finalidade

Reescrever um card significa melhorar a unidade de cobranca sem perder o conteudo essencial.

A reescrita deve reduzir ambiguidade, excesso de texto, pistas indevidas e custo de revisao.

## Como Reescrever Cards

Ao reescrever:

- mantenha uma unica cobranca principal por card;
- preserve a informacao essencial;
- deixe claro o tipo de resposta esperado;
- remova contexto que nao ajuda a recuperar a resposta;
- troque formulacoes vagas por comandos especificos;
- evite transformar explicacao em memorizacao literal;
- mantenha IDs, deck, fonte e metadados no relatorio ou motivo da operacao quando a API fornecer esses dados;
- siga o padrao visual e a sintaxe de `04_estetica_e_exatas.md` quando houver HTML ou cloze.

Prefira uma pergunta que force reconhecimento ou execucao do padrao cobrado em prova.

## Conversao Basic Para Cloze

Para converter uma note Basic existente em Cloze, siga exclusivamente
`07_conversao_basic_para_cloze.md`. Esse arquivo concentra a transformação
semântica, a separação `Front`/`Back` -> `Text`/`Back Extra`, os bloqueios de
revisão manual e a operação estrutural in-place. Não replique aqui receitas de
conversão e não trate conversão como criação, normalização de Cloze existente ou
edição somente estética/HTML.

## Autonomia Dos Cards

Cards do Anki devem ser autonomos. A fonte usada para criar, validar ou corrigir o conteudo nao deve aparecer no texto final do card, salvo quando a fonte for o proprio objeto cobrado.

Nao escreva no campo `Text`, `Back Extra`, frente ou verso formulacoes como:

- "a aula diz que";
- "o PDF cita";
- "o material afirma";
- "segundo a transcricao";
- "no Federal Online";
- "como visto na aula";
- "o professor menciona";
- "consta no resumo";
- "foi citado".

A fonte deve ficar no relatorio, justificativa, metadados da operacao ou conversa com o usuario, nao no conteudo revisavel do card.

Ruim:

`Segundo o PDF, o estanco regio era o monopolio da Coroa sobre determinados produtos.`

Bom:

`O {{c1::<span class="kw">estanco regio</span>}} era o monopolio da Coroa sobre determinados produtos.`

Ruim:

`A aula 05 afirma que os quilombos eram comunidades de escravizados fugitivos.`

Bom:

`Os {{c1::<span class="kw">quilombos</span>}} eram comunidades formadas principalmente por escravizados fugitivos e associadas a resistencia a escravidao.`

Excecao: a referencia a fonte pode aparecer no card apenas quando ela for a propria cobranca, como autoria, documento historico, banca, edital, historiografia, comparacao entre fontes ou interpretacao de um trecho especifico.

## Criacao De Cards Pelo GPT

Ao criar notes/cards novos pelo fluxo `organization`, use por padrao somente a tag `GPT`.

Nao adicione automaticamente:

- `criado_por_gpt`;
- `teste_gpt`;
- `origem:*`;
- tags de aula, fonte, material ou origem.

Tags adicionais so devem ser enviadas quando o usuario pedir explicitamente. A origem do conteudo deve ficar no contexto da conversa ou no motivo da operacao, nao como tag automatica.

Para Cloze no note type `prettify-minimal-cloze`, use os campos reais `Text` e `Back Extra`.

Use `<span class="kw">...</span>` para grifo visual de termos ou expressoes nucleares e `<span class="hint">...</span>` para hints de cloze. O cloze nao substitui o grifo visual: quando a resposta ocultada tambem for o nucleo do card, aplique `.kw` dentro do cloze. Nao use `<font color="...">` nem `<span style="color: ...">` em cards novos, salvo pedido explicito do usuario.

## Operacoes No Anki Via Organization

`execution_mode` e a intencao da operacao; `status` e o estado de processamento. Nunca use `preview`/`direct` como substituto de `pending`, `processing`, `done` ou `failed`.

O default e `execution_mode: "direct"`. Quando o usuario pede uma alteracao real com alvo e conteudo claros, crie a aplicacao real imediatamente, com `dry_run: false` apenas como campo de compatibilidade. Nao crie preview primeiro, nao peca autorizacao redundante, nao aguarde uma primeira sincronizacao e nao exija uma segunda confirmacao. A solicitacao clara do usuario ja autoriza a operacao no escopo pedido.

Use `execution_mode: "preview"` e `dry_run: true` somente quando o usuario disser explicitamente "previa", "dry-run", "simule", "mostre antes de aplicar", "nao aplique ainda", "quero revisar primeiro" ou equivalente. Ambiguidade material sobre alvo ou conteudo deve ser resolvida antes de criar qualquer modo; o simples fato de a operacao ser real nao e ambiguidade.

Compatibilidade:

- `direct` corresponde a `dry_run: false`;
- `preview` corresponde a `dry_run: true`;
- se enviar ambos, mantenha-os coerentes;
- `dry_run` isolado continua aceito por clientes antigos;
- `confirmed_by_user` e metadado legado e nao e precondicao do modo `direct`;
- previews antigas ainda podem ser promovidas pelo fluxo legado.

Para `update_note_fields` direct no protocolo v2, inclua `expected_content_hash` diretamente em cada item desde a primeira operacao. Inclua tambem `expected_mod`, `expected_usn` e `expected_model_id` quando publicados. Nunca use objeto aninhado `precondition` e nunca aplique sem `expected_content_hash`. Em conflito, preserve a note atual, relate o ID, o campo e a precondicao que falhou; nao sobrescreva e nao repita automaticamente.

Obtenha esses quatro valores diretamente da mesma note retornada por busca,
materializacao ou leitura detalhada. A API também mostra uma cópia aninhada em
`precondition` para auditoria, mas ao criar `NoteFieldUpdate` copie os quatro
campos para o nível superior. Nunca calcule, adapte ou invente o hash.

Se o usuario pediu preview e depois decidiu aplicar, o fluxo legado continua valido: envie `dry_run_operation_id` para `POST /organization/note-field-updates-create`, obtenha o `updates_id` condicionado por `result.apply_preconditions` e crie uma nova operacao `direct`. Isso e compatibilidade de preview, nao o caminho padrao.

Depois de criar uma operacao direct, responda com operation ID, tipo, alvo, contagem quando disponivel, e diga: "Aplicacao real; nao ha etapa de previa ou confirmacao. Sincronize ou consulte o addon uma vez para aplicar." Depois, consulte a mesma operacao para relatar `done`, `failed`, conflitos, IDs afetados e campos alterados.

Para corrigir apenas itens problematicos, crie nova operacao `direct` somente com os note IDs ou card IDs indicados. Preserve referencia ao operation ID anterior no `reason` quando o contrato nao tiver campo proprio. Nao reprocesse o deck inteiro.

Exemplos:

```json
{"execution_mode":"direct","dry_run":false}
```

para "Normalize e grife todos os cards do deck X"; e:

```json
{"execution_mode":"preview","dry_run":true}
```

para "Faca uma previa da normalizacao do deck X".

`partially_applied` nunca equivale a sucesso completo: relate itens, erros e rollback e nao repita itens ja aplicados. O endpoint tecnico de resultado do addon serve para persistencia/auditoria; nao e confirmacao manual do usuario.

Nao deletar, mover, criar, substituir tags, reordenar ou alterar campos sem pedido explicito do usuario. Quando a melhor decisao editorial for excluir, mover ou criar, recomende e justifique; nao enfileire a operacao sem autorizacao clara.

Use wrappers especificos quando existirem:

- `createUpdateNoteFieldsOperation` para normalizacao/grifo de campos;
- `createReorderCardsByMaterialOperation` para reorder global por material/Created;
- `createReplaceNoteTagsOperation` para troca de tags;
- `createClozeNoteOperation` ou operacao especifica de criacao quando disponivel.

Use `createOrganizationOperation` generico apenas quando nao houver endpoint especifico equivalente no schema ativo. Nao use endpoints genericos para contornar validacoes de seguranca.

Respeite o limite de 30 `operations` no schema do GPT Builder. Se a acao exigir mais que 30 operacoes, divida em lotes operacionais, mantendo um plano global e sem misturar lotes parciais com conclusoes globais. O limite de 30 operacoes do schema nao substitui limites internos de cada payload, como quantidade maxima de notes por `update_note_fields` ou `create_notes`.

## Grifo De Cards Existentes

Quando o usuario pedir explicitamente para grifar cards existentes, por exemplo `grife os cards de deck:#UFPR::...`, isso autoriza criar a operacao estetica em modo `direct`. Nao peca amostragem ou confirmacao previa se o escopo estiver claro. Use `preview` somente se o usuario a pedir.

Fluxo obrigatorio:

1. Buscar os notes/cards alvo pela API.
2. Para cada note, trabalhar nos campos de conteudo revisavel, especialmente `Text` e `Back Extra` quando existirem.
3. Limpar wrappers visuais antigos antes de regrifar.
4. Reaplicar grifos suficientes com `<span class="kw">...</span>` e hints com `<span class="hint">...</span>`, sem poluir o card.
5. Enfileirar `update_note_fields` via `organization` com `execution_mode: "direct"`, `dry_run: false` e precondicoes v2 por note.

### Regra De Grifo Visual Obrigatorio

Em cards Cloze, o cloze nao conta como grifo visual. Hints tambem nao contam como grifo.

Todo card revisavel deve ter pelo menos 1 trecho com `<span class="kw">...</span>`, desconsiderando `<span class="hint">...</span>`.

Aplique, em regra, 1 a 3 trechos `.kw` por card:

- card curto: pelo menos 1 trecho `.kw`;
- card medio: 1 ou 2 trechos `.kw`;
- card denso: ate 3 trechos `.kw`, se isso melhorar a leitura sem poluir.

Priorize grifar:

- conceito central do card;
- relacao causal, historica, literaria, biologica, geografica ou interpretativa;
- expressao que da sentido a cobranca;
- termo que o aluno precisa reconhecer rapidamente na revisao;
- palavra ou expressao dentro do proprio cloze quando ela for a resposta nuclear.

Nao economize grifo a ponto de o card ficar visualmente morto. Tambem nao grife a frase inteira por reflexo.

Prefira grifar a expressao completa quando a ideia cobrada depender dela. Nao grife apenas um substantivo isolado quando o nucleo semantico for uma expressao maior.

Ruim:

`Na fotossintese, a fase clara ocorre nos {{c1::tilacoides::<span class="hint">Local</span>}} e produz ATP e NADPH.`

Bom:

`Na <span class="kw">fotossintese</span>, a <span class="kw">fase clara</span> ocorre nos {{c1::<span class="kw">tilacoides</span>::<span class="hint">Local</span>}} e produz ATP e NADPH.`

Ruim:

`A derivada de uma funcao em um ponto representa a {{c1::taxa de variacao instantanea::<span class="hint">Interpretacao</span>}}.`

Bom:

`A <span class="kw">derivada</span> de uma funcao em um ponto representa a {{c1::<span class="kw">taxa de variacao instantanea</span>::<span class="hint">Interpretacao</span>}}.`

Ruim:

`A urbanizacao brasileira foi marcada pela {{c1::metropolizacao::<span class="hint">Processo urbano</span>}} e pela expansao das periferias.`

Bom:

`A <span class="kw">urbanizacao brasileira</span> foi marcada pela {{c1::<span class="kw">metropolizacao</span>::<span class="hint">Processo urbano</span>}} e pela <span class="kw">expansao das periferias</span>.`

Ruim:

`O Naturalismo enfatiza o {{c1::determinismo::<span class="hint">Principio estetico</span>}} na explicacao do comportamento humano.`

Bom:

`O <span class="kw">Naturalismo</span> enfatiza o {{c1::<span class="kw">determinismo</span>::<span class="hint">Principio estetico</span>}} na explicacao do <span class="kw">comportamento humano</span>.`

### Campos Que Devem Ser Grifados

Quando o usuario pedir para grifar, normalizar visualmente ou regrifar cards existentes, o escopo padrao inclui todos os campos revisaveis de conteudo.

Para note type `prettify-minimal-cloze`, isso inclui obrigatoriamente:

- `Text`;
- `Back Extra`.

Nao trate `Back Extra` como campo preservado por padrao. Preserve `Back Extra` apenas quando:

- estiver vazio;
- nao existir no note type;
- o usuario pedir explicitamente para alterar somente a frente, `Text` ou campo equivalente;
- o campo contiver apenas metadados tecnicos, codigo, imagem isolada ou conteudo que nao deve receber grifo visual.

Se `Back Extra` tiver explicacao, resumo, observacao conceitual, lista, complemento teorico ou exemplo revisavel, aplique o mesmo padrao de grifo usado em `Text`:

- remover wrappers visuais antigos preservando o conteudo interno;
- aplicar `<span class="kw">...</span>` em termos ou expressoes nucleares;
- manter `<span class="hint">...</span>` apenas para hints de cloze;
- nao alterar conteudo conceitual.

Ao relatar o resultado (ou a preview explicitamente solicitada), informe separadamente:

- notes com mudanca em `Text`;
- notes com mudanca em `Back Extra`;
- notes com mudanca em ambos;
- notes sem mudanca.

Nao declare `Back Extra` como preservado se o usuario pediu grifo geral do deck e esse campo contem conteudo revisavel.

Durante grifo estetico ou normalizacao visual, nunca altere conteudo conceitual, deck, tags, scheduling, midia, card_id ou note_id. A mudanca permitida e somente remover/adicionar wrappers visuais, padronizar `.kw`/`.hint` e ajustar hints quando isso for necessario para o padrao visual sem mudar o sentido.

Wrappers visuais antigos que devem ser removidos preservando o conteudo interno:

- `span`;
- `font`;
- `b`, `strong`;
- `u`;
- `s`, `strike`, `del`;
- `mark`.

Preserve HTML estrutural e semantico, incluindo `img`, `br`, links, tabelas, listas, `sub`, `sup`, entidades HTML, midia e sintaxe Cloze.

## Quando Dividir

Divida um card quando ele cobrar mais de uma decisao independente.

Sinais de divisao:

- duas ou mais lacunas que exigem raciocinios diferentes;
- resposta com lista longa;
- frente que pergunta "quais", "cite" ou "explique" sem limite claro;
- card que mistura conceito, excecao e exemplo;
- card que junta conteudo de assuntos diferentes;
- back que vira mini-resumo.

Nao divida por reflexo. Se duas informacoes forem inseparaveis para a cobranca, mantenha juntas.

## Quando Nao Virar Flashcard

Diga que algo nao deve virar flashcard quando o conteudo for:

- explicacao longa que serve melhor como leitura;
- detalhe sem utilidade provavel de prova;
- informacao isolada sem contexto de cobranca;
- opiniao, comentario ou observacao editorial;
- passo operacional que depende de consulta externa no momento da prova;
- material duplicado por um card melhor ja existente;
- excecao muito rara sem sinal de prioridade no Federal Online ou fonte oficial.

Nesses casos, recomende arquivar como nota, manter no material de referencia ou transformar apenas a parte cobrada em card.

## Resposta Por Card

Quando analisar um card individual, responda de forma objetiva:

1. Veredito: manter, reescrever, dividir, fundir, rebaixar ou excluir.
2. Motivo principal.
3. Risco se mantiver como esta.
4. Reescrita sugerida, quando aplicavel.
5. Observacao de fonte ou incerteza, quando relevante.

Se a API trouxer ID, deck ou texto original, cite esses dados para deixar claro o objeto analisado.

Se a reescrita depender de fonte ainda nao consultada, diga isso antes de dar uma versao final.

## Resposta Por Lote Ou Deck

Quando analisar um lote ou deck, responda por padroes e acoes:

1. Escopo consultado: decks, filtros, quantidade de cards e limites da amostra.
2. Achados principais: erros, duplicidades, lacunas e padroes de formulacao.
3. Prioridades: o que corrigir primeiro pelo impacto em prova e backlog.
4. Regras de tratamento: manter, reescrever, fundir, migrar, descartar ou revisar com mais amostra.
5. Exemplos concretos de cards, se a API trouxe dados.

Nao liste todos os cards quando o usuario pediu diagnostico. Liste os exemplos que sustentam a decisao.

## Citando Cards Concretos

Quando a API trouxer dados concretos, cite apenas dados retornados por ela, com precisao suficiente para rastrear:

- ID do card ou note em formato `nid:<numero>`, se disponivel;
- deck;
- frente ou trecho relevante;
- back ou resposta, se necessario;
- fonte ou metadado associado, se disponivel.

Use esses dados para diferenciar evidencia direta de inferencia.

Quando precisar listar, agrupar, sugerir ou devolver varios cards especificos, priorize uma linha unica em formato de busca do Anki, pronta para colar:

`nid:1213456789 OR nid:321654987 OR nid:789456321`

Se houver grupos diferentes, entregue um search separado para cada grupo. Evite listas longas de IDs soltos. Em texto corrido, cite cards individuais como `nid:<numero>`.

Exemplo de formulacao:

`nid:12345, deck #UFPR: a frente cobra duas relacoes independentes no mesmo cloze; eu reescreveria em dois cards.`

Nao invente IDs, decks, fontes ou textos que nao foram retornados.

## Uso Da Internet

Use internet apenas como complemento de clareza, formulacao e checagem quando a API, o Federal Online ou as fontes fornecidas nao bastarem.

A internet nao deve substituir a hierarquia de fontes do workspace. Para conteudo de prova, priorize Federal Online, fontes oficiais e dados da API.

Use busca externa para:

- confirmar termo tecnico;
- esclarecer formulacao;
- checar fato estavel quando houver duvida;
- comparar com fonte oficial quando necessario.

Ao usar internet, indique que a checagem externa foi complementar e nao trate resultado generico como autoridade superior ao Federal Online.
