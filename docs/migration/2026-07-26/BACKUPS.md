# Backups e rollback

## Local

Backup seletivo de código e metadados:

`/Users/gatsby/Workspace/Anki Study Platform Migration Backups/20260726T190824Z`

Os dois diretórios originais permanecem integralmente no lugar. Dados grandes
foram inventariados por caminho, tamanho, contagem e hashes representativos,
sem duplicação integral.

## VPS

Bundle privado:

`/home/ubuntu/migration-backups/anki-study-platform-20260726T190824Z`

Contém código dos componentes, configuração Nginx/systemd, crontabs, metadados
Docker e snapshots SQLite consistentes do Cronograma, fila e índice FTS.
Arquivos compactados foram testados e os bancos retornaram
`integrity_check=ok`. O arquivo `SHA256SUMS` permite verificar a restauração.

## Ordem de rollback

1. parar somente o componente novo afetado;
2. verificar `SHA256SUMS`;
3. restaurar o arquivo de código/configuração correspondente;
4. restaurar SQLite apenas se a comparação de integridade/contagens exigir;
5. restaurar o symlink do addon para o destino registrado;
6. validar Nginx/Compose/unidades;
7. iniciar o runtime anterior e repetir healthchecks e contagens.

Os bundles não entram no Git e não serão removidos nesta migração.
