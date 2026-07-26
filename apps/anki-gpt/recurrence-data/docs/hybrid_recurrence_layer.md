# Camada hibrida de recorrencia

## Objetivo

Esta pasta separa dois usos complementares dos dados de recorrencia:

- `raw/`: materiais originais ou quase originais usados como knowledge do GPT.
- `structured/`: representacoes normalizadas que poderao ser servidas por API no futuro.

O objetivo e manter a flexibilidade dos arquivos de knowledge, mas preparar uma camada estruturada para consultas mais precisas, comparaveis e auditaveis.

## Knowledge vs API estruturada

Knowledge continua sendo util para leitura livre pelo GPT: tabelas, resumos, listas de assuntos recorrentes e observacoes humanas podem ficar em formato textual.

A API estruturada deve expor dados em formato previsivel, com campos como disciplina, fase, tema, peso de recorrencia, subtemas, estrelas e aliases. Isso permite filtrar, ordenar e cruzar dados sem depender de interpretacao livre a cada chamada.

## Por que isso ajuda na priorizacao de cards

Com recorrencia estruturada, o sistema podera comparar um card do Anki com temas frequentes de prova e estimar prioridade relativa. Isso ajuda a identificar cards importantes, cards bons mas pouco prioritarios, cards overkill e lacunas em temas recorrentes ainda sem cobertura adequada.

