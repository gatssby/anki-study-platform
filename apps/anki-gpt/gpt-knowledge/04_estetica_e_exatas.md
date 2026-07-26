# Estetica E Exatas

## Padrao Visual Obrigatorio

O estilo deve ser minimalista, limpo e consistente.

Regras obrigatorias:

- nao usar bold;
- nao usar underline;
- usar italico apenas para estrangeirismo ou nome cientifico;
- manter texto enxuto;
- evitar decoracao visual;
- evitar marcacoes que aumentem ruido de revisao;
- preservar HTML existente quando reescrever cards que ja usam HTML;
- preservar sintaxe cloze existente quando reescrever cards cloze.

O objetivo visual e facilitar revisao rapida, nao destacar tudo.

Interprete bold e underline como qualquer equivalente visual ou HTML, incluindo `<b>`, `<strong>`, `<u>` e estilo CSS correspondente.

Em cards existentes que ja estejam grifados ou formatados visualmente, resetar antes de regrifar significa remover wrappers puramente visuais e preservar o conteudo interno. Remova `span`, `font`, `b`, `strong`, `u`, `s`, `strike`, `del` e `mark`. Preserve `i` e `em` por padrao, pois podem representar estrangeirismo ou nome cientifico.

Preserve HTML estrutural e semantico: `img`, `br`, `a`, tabelas, listas, `sub`, `sup`, entidades HTML, referencias de midia e sintaxe Cloze.

## Cloze Hint

O cloze hint, quando usado, deve ser escrito com a classe `.hint`:

`<span class="hint">...</span>`

Ele deve orientar o tipo de resposta, mas nunca entregar a resposta ou quase-resposta.

Ruim:

`{{c1::mitocondria::organela que faz respiracao celular}}`

Melhor:

`{{c1::mitocondria::<span class="hint">organela</span>}}`

Nao use hint quando ele nao acrescentar orientacao real ou quando a propria frase ja delimitar claramente a resposta.

Nao use `<font color="...">`, `<span style="color: ...">` ou estilo inline de cor em cards novos, salvo pedido explicito do usuario.

## Palavras-Chave

Use `<span class="kw">...</span>` apenas para palavras-chave estruturantes.

Regras:

- normalmente 1 a 3 marcacoes por card;
- nao marque trechos longos;
- nao marque tudo dentro do cloze por reflexo;
- marque palavras-chave no texto principal ou no `Back Extra` apenas quando isso melhorar a recuperacao;
- nao use `.kw` para enfeitar;
- se a versao sem marcacao for igualmente clara, prefira pouca ou nenhuma marcacao.

Esta regra vale para criacao e reescrita em geral. Quando o usuario pedir explicitamente grifo, regrifo ou normalizacao visual de cards existentes, siga a regra especifica de grifo de `03_padrao_de_reescrita.md`, que exige ao menos um trecho `.kw` em cada card revisavel do escopo.

Exemplo:

`O transporte <span class="kw">ativo</span> gasta ATP porque {{c1::vai <span class="kw">contra</span> o gradiente de concentracao}}.`

Exemplo com hint:

`{{c1::A <span class="kw">autolise</span>::<span class="hint">Processo</span>}} ocorre atraves {{c2::da <span class="kw">ruptura</span> das membranas lisossomicas}}.`

Exemplo sem marcacao adicional:

`{{c1::Interfase}} e {{c2::o periodo em que a celula nao esta em divisao}}.`

## Sem Pistas Indevidas

Nao deixe o cloze hint, a gramatica ou a extensao da lacuna entregarem a resposta.

Evite hints que:

- contenham sinonimo direto da resposta;
- deem a definicao quase completa;
- indiquem numero, genero ou estrutura que torne a resposta obvia sem conhecimento;
- repitam palavra-chave do back;
- transformem recuperacao em adivinhacao por pista.

## Exemplos No Back

Nao escreva "Exemplo:" quando o back ja for claramente um exemplo.

Use rotulo apenas quando houver risco real de confusao entre definicao, regra, contraexemplo e aplicacao.

## Exatas

Em exatas, evite cards de regra abstrata quando um exemplo operacional memoriza melhor.

Prefira cards que facam o usuario reconhecer e executar padroes:

- identificar tipo de problema;
- escolher formula ou procedimento;
- aplicar uma transformacao;
- reconhecer armadilha comum;
- executar uma conta curta;
- interpretar grafico, tabela ou unidade.

Um card de exatas bom deve aproximar a revisao do gesto de resolver questao.

## Regra Abstrata Vs Exemplo Operacional

Ruim:

`A distributiva permite multiplicar um termo por todos os termos dentro dos parenteses.`

Melhor:

`Ao expandir 3(x + 2), o resultado e {{c1::3x + 6::<span class="hint">expressao</span>}}.`

Use regra abstrata apenas quando ela for diretamente cobrada como conceito, nomenclatura ou justificativa.

## Manter Sintaxe

Ao reescrever, nao quebrar HTML, cloze ou campos especiais do Anki.

Preserve:

- `{{c1::...}}`;
- hints dentro de cloze;
- tags HTML ja necessarias;
- entidades e quebras de linha relevantes;
- formato de campos quando a API devolver front, back, extra ou metadados separados.

Se for necessario alterar a estrutura do card, explique a mudanca e entregue a versao final pronta para colar no campo correto.

## Minimalismo

Nao enfeite cards.

Use a menor quantidade de formatacao capaz de:

- preservar significado;
- orientar a recuperacao;
- melhorar legibilidade;
- manter consistencia com os cards existentes.

Quando houver duvida entre uma versao visualmente mais marcada e uma versao simples igualmente clara, escolha a simples.
