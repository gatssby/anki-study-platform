# Shared FO runtime

O pipeline continua usando:

- estado do Cronograma em `/opt/cronograma-fo/state/federal_online`;
- API/fila em `/home/ubuntu/fo-transcricoes-system`;
- transcrições em `/home/ubuntu/fo-transcricoes`;
- materiais/índices consumidos pela API em `/home/ubuntu/anki-gpt-sync`.

Os jobs são implantados junto do componente que já os executa. Serviços de
transcrição não são relocados nesta migração.
