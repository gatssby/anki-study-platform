# Fluxo futuro de tagging controlado

## Objetivo

O sistema de tagging deve permitir que o GPT solicite adicao ou remocao de tags em cards do Anki sem editar conteudo dos cards.

As operacoes serao limitadas a tags controladas inicialmente:

- `prio:alta`
- `prio:media`
- `prio:baixa`
- `avaliar`
- `precisa_melhorar`
- `overkill`
- `duplicado`
- `bom_card`

## Fluxo previsto

1. O GPT sugere uma operacao de tagging no chat.
2. O usuario confirma explicitamente a operacao.
3. A VPS registra uma operacao pendente.
4. O addon local do Anki consulta operacoes pendentes.
5. O addon aplica as tags no Anki local.
6. O addon envia uma confirmacao de execucao para a VPS.
7. A VPS registra status final, erros e quantidade de notas afetadas.

## Regra de seguranca

Nenhuma operacao de escrita deve ser aceita sem confirmacao explicita do usuario no chat. A API futura deve diferenciar claramente uma sugestao do GPT de uma operacao confirmada e pronta para execucao pelo addon.

