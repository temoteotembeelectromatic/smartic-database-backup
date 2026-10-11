# Smartic database backup

Backup PostgreSQL para S3, independente do repositorio da aplicacao.
O cron job esta definido para executar de 30 em 30 minutos (minutos 00 e 30).

## Ativar no Render

1. Criar um Blueprint ligado a este repositorio e a branch `main`.
2. Selecionar a mesma regiao e workspace da base PostgreSQL de origem.
3. Definir `DATABASE_URL` com a Internal Database URL da base a proteger.
   Esta variavel e um segredo; nao a guardar no Git. O Blueprint nao cria uma base.
4. Definir `BACKUP_S3_BUCKET`, `AWS_DEFAULT_REGION`, `AWS_ACCESS_KEY_ID`
   e `AWS_SECRET_ACCESS_KEY`, usando uma identidade dedicada ao backup.
5. Executar **Trigger Run** e confirmar os objetos `.dump` e `.dump.json` no S3.
6. Testar o restauro numa base separada e configurar notificacoes de falha.

Se o cron job ja existir no Render ligado ao repositorio `smartic`, alterar a
ligacao para este repositorio em vez de manter dois agendamentos ativos.

Na configuracao manual do Render, usar:

- Language: Docker
- Root Directory: vazio
- Dockerfile Path: `./scripts/backup.Dockerfile`
- Docker Build Context Directory: `.` (raiz do repositorio)
- Docker Command: vazio
- Schedule: `*/30 * * * *`

O `COPY` e as regras de exclusao do Docker usam caminhos relativos a raiz.
Nao definir o contexto como `./scripts`.

O contentor inclui pg_dump/pg_restore 18. Confirmar compatibilidade com a versao
da base antes da primeira execucao. Ainda nao foi validado um backup em producao.

## Testes locais

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
```

Os testes simulam PostgreSQL e S3; nao acedem a dados reais.
Consultar [configuracao, permissoes S3 e restauro](docs/database-backup.md).
