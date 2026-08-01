# Manutencao Do GPT Knowledge

Este arquivo orienta manutencao dos arquivos de Knowledge do Custom GPT Anki GPT. Ele nao substitui as regras operacionais dos arquivos numerados.

## Lista Final De Arquivos

Arquivos operacionais centrais:

- `01_principios_gerais.md`
- `02_criterios_de_analise.md`
- `03_padrao_de_reescrita.md`
- `04_estetica_e_exatas.md`
- `05_fluxo_decks_grandes.md`
- `06_fluxo_fo_materiais_transcricoes.md`
- `07_conversao_basic_para_cloze.md`

Arquivo para colar no GPT Builder:

- `INSTRUCTIONS_CURTAS_GPT_BUILDER.md`

Arquivo de manutencao:

- `MANUTENCAO_GPT_KNOWLEDGE.md`

Arquivos de recorrencia:

- `Recorrência /historia.md`
- `Recorrência /matematica.md`
- `Recorrência /quimica.md`
- `Recorrência /geografia.md`
- `Recorrência /biologia.md`
- `Recorrência /portugues.md`
- `Recorrência /biologia_segunda_fase.md`
- `Recorrência /quimica_segunda_fase.md`
- `Recorrência /fisica.md`

## Funcao De Cada Arquivo

`01_principios_gerais.md`: objetivo do GPT, prioridades editoriais, hierarquia de fontes, papel da API e regra de preservacao.

`02_criterios_de_analise.md`: criterios para julgar card bom/ruim, eixos de analise, leitura de lotes/decks e separacao entre evidencia, padrao e hipotese.

`03_padrao_de_reescrita.md`: como reescrever, criar, grifar, dividir, fundir ou recomendar exclusao; tambem concentra `execution_mode`, compatibilidade `dry_run`, precondicoes e limites de schema.

`04_estetica_e_exatas.md`: padrao visual, `.kw`, `.hint`, minimalismo, cloze e diretrizes especificas para exatas.

`05_fluxo_decks_grandes.md`: leitura global de decks grandes, materializacao em lotes, consolidacao, auditoria, normalizacao/grifo em massa e reorder por materiais/Created/Added.

`06_fluxo_fo_materiais_transcricoes.md`: busca correta de materiais/PDFs Federal Online, escolha do PDF principal, extracao de texto, transcricoes, prioridade PDF > transcricao, bloqueios e exemplos de História I Aulas 01 a 08.

`07_conversao_basic_para_cloze.md`: fonte de verdade da transformação semântica e estrutural Basic -> Cloze, campos reais, preservação de IDs/metadados, validações e revisão manual.

`INSTRUCTIONS_CURTAS_GPT_BUILDER.md`: bloco curto para colar nas Instructions do GPT Builder. Deve apontar para os arquivos do Knowledge e nao repetir todos os detalhes.

Arquivos em `Recorrência /`: referencia de prioridade por materia. Use quando a decisao editorial depender de probabilidade de cobranca ou foco de prova. Nao use esses arquivos como regra operacional de API, execution_mode ou schema.

## Quando Atualizar

Atualize `01_principios_gerais.md` quando mudar objetivo, hierarquia de fontes, criterio de autoridade ou politica geral de API.

Atualize `02_criterios_de_analise.md` quando mudar o que conta como card bom/ruim, padrao de julgamento, evidencia minima ou reaproveitamento entre decks.

Atualize `03_padrao_de_reescrita.md` quando mudar operacao de escrita, execution_mode, compatibilidade dry_run, criacao de cards, tags padrao, preservacao conceitual ou wrappers de `organization`.

Atualize `04_estetica_e_exatas.md` quando mudar padrao visual, uso de `.kw`, `.hint`, HTML permitido ou heuristicas para cards de exatas.

Atualize `05_fluxo_decks_grandes.md` quando mudar materializacao, consolidacao, reorder global, Created/Added ou limites praticos de lote.

Atualize `06_fluxo_fo_materiais_transcricoes.md` quando mudar endpoints FO, schema de extracao de PDF, estrutura de transcricoes, regra de prioridade PDF/transcricao ou exemplos de chamadas.

Atualize `07_conversao_basic_para_cloze.md` quando mudar a semântica da conversão, o mapeamento `Front`/`Back` para `Text`/`Back Extra`, os bloqueios de segurança ou a operação in-place.

Atualize arquivos de `Recorrência /` quando houver nova analise estatistica por materia ou ajuste de prioridade de prova.

Atualize `INSTRUCTIONS_CURTAS_GPT_BUILDER.md` apenas quando mudar a forma curta colada no GPT Builder ou a lista de arquivos operacionais.

## Como Evitar Duplicidade Com Instructions

As Instructions do GPT Builder devem ser curtas. Elas devem:

- definir papel do GPT;
- mandar usar Actions/API antes de inferir;
- declarar que arquivos Knowledge tem prioridade operacional;
- listar arquivos principais;
- lembrar default direct, preview somente explicita, FO PDFs/transcricoes e hierarquia de fontes.

Nao coloque nas Instructions longas:

- exemplos extensos de chamadas FO;
- regras completas de `.kw`/`.hint`;
- fluxo detalhado de deck grande;
- checklist completo de modos e precondicoes;
- criterios longos de card bom/ruim;
- recorrencia por materia.

Esses detalhes pertencem aos arquivos `.md` do Knowledge. Se uma regra aparece nas Instructions e em arquivo numerado, mantenha nas Instructions apenas a versao curta e deixe o detalhe no arquivo numerado.

## Checklist Depois De Alterar Knowledge

Depois de qualquer mudanca nos arquivos de Knowledge:

1. Verificar se todos os arquivos referenciados existem no Knowledge.
2. Procurar contradicoes entre `01`, `03`, `05` e `06`, especialmente em API, fonte FO, execution_mode e operacao real.
3. Confirmar que FO ainda exige busca por frente/material geral antes de transcricoes.
4. Confirmar que PDF/texto FO vence transcricao em conflito.
5. Confirmar que nenhuma regra autoriza deletar, mover, criar, grifar, substituir tags ou reordenar sem pedido explicito.
6. Confirmar que `direct` e o default e que `preview` depende de pedido explicito.
7. Confirmar que nenhuma regra exige preview, segunda confirmacao ou duas sincronizacoes para uma operacao direct.
8. Confirmar que decks grandes exigem `/cards/query-ids`, materializacao completa e consolidacao global.
9. Confirmar que reorder por Created/Added usa ordem global, nao lotes.
10. Confirmar que `.kw` e `.hint` continuam minimalistas e sem alterar conteudo conceitual.
11. Confirmar que o limite de 30 `operations` do schema esta citado.
12. Confirmar que endpoints especificos sao preferidos a endpoints genericos perigosos.
13. Confirmar que conversão Basic -> Cloze aponta para `07`, rejeita `Front<br>{{c1::Back inteiro}}` e não conflita com normalização/criação/HTML.

## Checklist Depois De Alterar Schema OpenAPI

Quando o schema for alterado:

1. Verificar operationIds reais contra as regras do Knowledge.
2. Testar buscas de cards: `/cards/query-ids` e `/cards/materialize`.
3. Testar materiais FO: `getFoMaterials`.
4. Testar extracao de PDF: `getFoMaterial(... extract_text=true ...)` se o schema oferecer isso, ou `getFoText` quando a extracao estiver em endpoint separado.
5. Testar transcricoes: `getFoTranscripts` e `getFoTranscript`.
6. Testar wrapper de update fields com `execution_mode: direct` e precondicoes v2, alem do override preview.
7. Testar wrapper de reorder global com `execution_mode: direct` e o override preview.
8. Confirmar que payloads de escrita rejeitam operacoes parciais ou inseguras.
9. Confirmar limite maximo de `operations` aceito pelo GPT Builder.
10. Atualizar `06_fluxo_fo_materiais_transcricoes.md` se nomes de parametros FO mudarem.

## Prompt De Teste Recomendado

Use um prompt de teste grande:

`faça o fluxo completo no deck X com base em História I Aulas 01 a 08`

O comportamento esperado:

1. buscar PDF FO por frente/material geral;
2. encontrar `História/História I - Resumo.pdf` quando `getFoMaterials(q="História I")` retornar esse material;
3. extrair texto do PDF antes de criar operacoes;
4. buscar transcricoes como complemento;
5. aplicar PDF/texto FO > transcricao em conflito;
6. materializar deck completo se for grande;
7. criar `execution_mode: direct` por padrao, ou preview somente se o prompt a pedir;
8. relatar fontes, operation IDs e que uma consulta/sincronizacao do addon basta.
