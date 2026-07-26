# Transcrições FO

O índice `fo_transcripts_fts.sqlite` usa FTS5 unicode61 com remoção de diacríticos. Produção validada: 1.017 documentos.

`rebuild_fo_search_index.py --verify` é somente leitura. `--incremental --dry-run` compara fontes. Rebuild real usa arquivo temporário e substitui somente após integrity/row-count. Paths são relativos e confinados à raiz; snippets são limitados e não expõem paths absolutos.

