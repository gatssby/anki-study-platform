# Conversão Estrutural Basic Para Cloze

## Autoridade e escopo

Este arquivo é a fonte de verdade para a operação específica de conversão de
uma note Basic existente para Cloze. As regras visuais de
`04_estetica_e_exatas.md` continuam válidas, mas não substituem a transformação
semântica definida aqui.

Não confunda quatro fluxos:

- **conversão estrutural Basic -> Cloze**: muda o note type e reescreve
  `Front` + `Back` como uma afirmação declarativa em `Text` + `Back Extra`;
- **normalização de Cloze existente**: preserva a proposição e a sintaxe cloze,
  corrigindo apenas o que o usuário pediu;
- **criação de card novo**: não possui uma note Basic de origem nem IDs a
  preservar;
- **edição estética/HTML**: não muda conteúdo conceitual nem note type.

Estas regras atingem apenas Basic -> Cloze. Se a origem já for Cloze, não use a
operação de conversão e não reescreva automaticamente a note.

## Campos e operação reais

No projeto, a origem suportada é `prettify-minimal-basic`, com campos `Front` e
`Back`. O destino é `prettify-minimal-cloze`, com campos `Text` e `Back Extra`.
Quando apenas descrever uma equivalência para outro note type, `Extra` pode ser
o nome equivalente a `Back Extra`, mas nunca invente um nome de campo: consulte
`get_note_type_fields`.

Para aplicar uma conversão existente, use somente
`convertBasicToClozeOperation` / `convert_basic_to_cloze`. Não use
`createClozeNoteOperation` para fabricar uma cópia, não use
`update_note_fields` para fingir que o note type mudou e não apague a note
Basic. A operação in-place preserva `note_id`, tags, deck e os card IDs já
existentes; `c2`, `c3` etc. podem criar cards adicionais necessários.

Na conversão de uma note Basic com um único card, o card preexistente é
reutilizado como `c1` (`ord = 0`) e conserva o próprio `card_id` e scheduling
quando a mudança de note type é tecnicamente compatível. Se o `Text` também
contiver `c2`, `c3` etc., o Anki cria cards adicionais com novos IDs e
scheduling inicial de card novo; não existe ID anterior a preservar nesses
cards. “Preservar cards existentes” nunca significa fabricar ou reutilizar IDs
para cards que ainda não existiam.

Sempre copie da leitura canônica, sem calcular ou inventar:

- `expected_content_hash` (obrigatório);
- `expected_mod`, `expected_usn` e `expected_model_id`, quando publicados;
- `source_front` e `source_back` exatamente como lidos.

Preserve campos não relacionados, mídia, scheduling e histórico. Se o note type
de origem ou destino possuir campos extras não mapeáveis, não aplique a
conversão automática: mantenha a note e peça revisão manual.

## Regra principal

Uma conversão correta transforma a pergunta e a resposta em uma ou mais frases
declarativas naturais, autossuficientes e próprias para "complete a frase".
A frente Basic não pode continuar como pergunta.

Errado:

`O nível trófico é fixo? {{c1::Não.}}`

Correto:

`O nível trófico de um organismo {{c1::não é fixo}}.`

É proibida a receita mecânica:

`Front original<br>{{c1::Back original inteiro}}`

Não existe neste projeto uma regra que autorize envolver automaticamente todo o
`Back` com `{{c1::...}}`.

## Escolha da informação ocultada

Identifique o que a pergunta original exigia que o estudante recuperasse. O
cloze deve esconder essa informação, não palavras arbitrárias usadas apenas
como contexto.

Não crie clozes sobre termos presentes no `Front` original, a menos que esses
termos também sejam alvos independentes de aprendizagem. Não transforme todo
substantivo técnico em `c2` ou `c3`.

Evite esconder isoladamente `não`, `sim`, `é`, `pode`, artigos, preposições ou
conectivos. Oculte uma unidade semanticamente completa:

- errado: `O nível trófico {{c1::não}} é fixo.`
- correto: `O nível trófico {{c1::não é fixo}}.`

## Informação mínima e campo complementar

O `Text` deve conter apenas a proposição necessária para responder ao card
original. Mova para `Back Extra` explicações, justificativas, exemplos,
exceções, consequências e observações complementares que ajudam a compreender,
mas não precisam ser recuperadas como resposta principal.

Se o `Back` contiver somente a resposta direta, `Back Extra` pode ficar vazio.
Não duplique integralmente em `Back Extra` a frase já apresentada em `Text`.
Não esconda em um único cloze parágrafos, enumerações extensas ou uma resposta
com múltiplas proposições.

## Quantidade de cards

Por padrão, use uma única proposição atômica e `c1`. Duas ocorrências de `c1`
são ocultadas juntas e continuam sendo um card. `c1` e `c2` geram cards
separados.

Use `c2`, `c3` etc. somente quando houver informações independentes que
mereçam recuperação separada. Quando a origem mistura várias proposições, a
melhor saída pode ser mais de uma conversão/card; isso exige autorização para
criação adicional e não autoriza apagar conteúdo ou IDs.

## Fidelidade

A conversão pode mudar a ordem, remover a interrogação, inserir conectivos
mínimos, ajustar concordância/pontuação/capitalização e eliminar redundâncias
necessárias apenas no formato pergunta/resposta.

A conversão não pode adicionar fatos, ampliar o conteúdo cobrado, trocar a
informação central, transformar exemplo em regra geral, criar exceções ou
justificativas ausentes, nem alterar números, nomes, relações causais ou
qualificadores. Reformule apenas o que estiver sustentado pela note de origem ou
por fonte superior já consultada e autorizada para correção conceitual.

## Conteúdo estrutural

Preserve, no `Text` ou `Back Extra`, conforme sua função original:

- imagens e áudio;
- MathJax e fórmulas;
- referências e HTML estrutural relevante;
- tags, deck, `note_id`, card IDs e scheduling;
- campos não relacionados à conversão.

Não mova mídia entre notes, não recrie a note e não altere histórico. HTML
visual pode seguir `04_estetica_e_exatas.md`, mas uma conversão não é licença
para uma normalização estética geral.

## Quando bloquear e pedir revisão manual

Se não for possível produzir uma frase natural, inequívoca, mínima e fiel sem
esconder uma resposta extensa, não improvise. Também bloqueie quando:

- a origem já contém Cloze;
- o note type não é o Basic suportado;
- campos extras seriam descartados;
- conteúdo estrutural não pode ser preservado;
- a pergunta depende de contexto ausente;
- há conflito de precondição;
- a resposta admite mais de uma interpretação e a origem não resolve qual;
- a conversão exigiria adicionar fatos ou generalizar o conteúdo.

Nesses casos, preserve a note inalterada, não crie operação e reporte
`revisão manual` com a razão específica.

## Padrões de conversão

### Pergunta binária

Entrada:

- `Front`: `As pirâmides de energia podem ser invertidas?`
- `Back`: `Não, porque há perda de energia a cada nível trófico.`

Saída:

- `Text`: `As pirâmides de energia {{c1::não podem ser invertidas}}.`
- `Back Extra`: `Há perda de energia a cada transferência entre níveis tróficos.`

### Identificação

Entrada:

- `Front`: `Qual organela realiza a respiração celular aeróbia?`
- `Back`: `Mitocôndria.`

Saída:

- `Text`: `A respiração celular aeróbia ocorre principalmente nas {{c1::mitocôndrias}}.`
- `Back Extra`: vazio.

### Definição

Entrada:

- `Front`: `O que é uma população em ecologia?`
- `Back`: `Conjunto de indivíduos da mesma espécie que vivem em uma mesma área.`

Saída:

- `Text`: `Uma população é o {{c1::conjunto de indivíduos da mesma espécie que vivem em uma mesma área}}.`
- `Back Extra`: vazio.

Não esconda `população`: a origem perguntava sua definição.

### Relação causal

Entrada:

- `Front`: `Por que a energia diminui ao longo da cadeia alimentar?`
- `Back`: `Porque parte da energia é dissipada como calor em cada nível trófico.`

Saída:

- `Text`: `A energia diminui ao longo da cadeia alimentar porque parte dela é {{c1::dissipada como calor em cada nível trófico}}.`
- `Back Extra`: vazio.

### Resposta com explicação complementar

Entrada:

- `Front`: `O nível trófico de um organismo é sempre o mesmo?`
- `Back`: `Não. Ele pode variar dependendo da cadeia alimentar considerada.`

Saída:

- `Text`: `O nível trófico de um organismo {{c1::pode variar conforme a cadeia alimentar considerada}}.`
- `Back Extra`: `Em uma teia alimentar, o mesmo organismo pode ocupar diferentes níveis tróficos.`

## Aceitação obrigatória

Entrada:

- `Front`: `Em uma teia alimentar, o nível trófico de um organismo é fixo?`
- `Back`: `Não. O mesmo organismo pode ocupar diferentes níveis tróficos.`

Saída obrigatória:

- `Text`: `Em uma teia alimentar, o nível trófico de um organismo {{c1::não é fixo}}.`
- `Back Extra`: `O mesmo organismo pode ocupar diferentes níveis tróficos conforme a cadeia alimentar considerada.`

Rejeite:

`Em uma teia alimentar, o nível trófico de um organismo é fixo?<br>{{c1::Não. O mesmo organismo pode ocupar diferentes níveis tróficos.}}`

Evite, salvo justificativa específica:

`Em uma {{c1::teia alimentar}}, o nível trófico de um organismo {{c2::não}} é fixo.`

`Em uma teia alimentar, {{c1::o nível trófico de um organismo não é fixo e o mesmo organismo pode ocupar diferentes níveis tróficos}}.`
