# Backup PostgreSQL do Smartic Pro

Estado: funcao e cron job configurados no repositorio, ainda nao instalados nem executados em producao.
O projeto usa DATABASE_URL, configurada como segredo no cron job Render.
Confirmar no Render a base atualmente usada, versao PostgreSQL e tamanho antes de ativar.

## Destino e execucao

Criar um bucket S3 dedicado, privado, com Block Public Access completo, encriptacao
SSE-S3 e versionamento. Escolher a regiao antes da criacao. Proposta: eu-north-1,
onde o CloudShell foi utilizado. Nao reutilizar um bucket publico de documentos.

Executar scripts/backup_database.py num ambiente com acesso a base Render, idealmente
um job na mesma regiao/rede do Render, independente deste computador. Instalar boto3
e clientes pg_dump/pg_restore da mesma versao principal do servidor. O runtime Python
do projeto nao garante que esses executaveis existam: confirmar antes de configurar o job.

Variaveis secretas/configuracao do job:

- DATABASE_URL: ligacao da base de origem, fornecida pelo gestor de segredos do Render.
- BACKUP_S3_BUCKET: nome do bucket dedicado, obrigatorio (sem fallback para o bucket da app).
- BACKUP_S3_PREFIX: smartic-pro/postgres (predefinido).
- AWS_DEFAULT_REGION: regiao do bucket.
- Credenciais AWS dedicadas ao backup, fornecidas pelo ambiente, nunca no repositorio.

O utilizador da base deve poder ler todas as tabelas, sequencias e large objects;
restricoes de acesso/RLS podem impedir um backup completo. O comando falha nesse caso.
Reservar disco temporario suficiente para um dump integral. Ha um limite de 2 horas
para a exportacao e 60 segundos para espera de locks; ajustar apenas apos medir a base.

Comando manual: `python scripts/backup_database.py`.

### Cron job Render

O `render.yaml` define `smartic-database-backup`, diariamente as 02:00 UTC
(`0 2 * * *`). Em Lisboa corresponde as 02:00 no inverno e as 03:00 no verao.
O job usa `scripts/backup.Dockerfile`, que inclui Python, boto3 e os clientes
PostgreSQL 18; nao depende das ferramentas instaladas no servico web.
pg_dump 18 suporta servidores de versoes anteriores, mas nao servidores posteriores
a 18. Confirmar a versao de origem; ajustar POSTGRES_MAJOR no Dockerfile se necessario.

Para ativar:

1. Criar ou sincronizar um Blueprint ligado ao repositorio smartic-database-backup,
   branch main. Se o job ja existir, migrar a ligacao sem duplicar o agendamento.
2. No cron job, definir BACKUP_S3_BUCKET, AWS_DEFAULT_REGION,
   AWS_ACCESS_KEY_ID e AWS_SECRET_ACCESS_KEY. Definir DATABASE_URL com a ligacao
   interna da base de origem (sharepoint-db, se for a base pretendida).
   Colocar o job na mesma regiao da base para usar a ligacao interna.
3. Executar uma vez com Trigger Run. Confirmar sucesso nos logs e os dois objetos
   `.dump` e `.dump.json` no bucket. Uma falha devolve codigo de saida 1.
4. Fazer o teste de recuperacao abaixo e ativar notificacoes de falha no Render.

Para testar a imagem num computador com Docker, a partir da raiz do projeto:

```sh
docker build -f scripts/backup.Dockerfile -t smartic-db-backup scripts
docker run --rm --env DATABASE_URL --env BACKUP_S3_BUCKET --env AWS_DEFAULT_REGION --env AWS_ACCESS_KEY_ID --env AWS_SECRET_ACCESS_KEY smartic-db-backup
```

O comando executa um backup real usando as variaveis exportadas no terminal.
Com credenciais temporarias, passar tambem AWS_SESSION_TOKEN ao contentor/job.

Agendamento configurado, ainda nao ativado: diariamente as 02:00 UTC. Retencao
recomendada no S3: 30 dias. Um backup diario pode perder ate 24 horas de alteracoes (mais se houver falhas).
Configurar notificacoes de falha e alerta externo se nao surgir um manifesto valido
em 26 horas. Confirmar se esse intervalo de perda de dados e aceitavel.

Configurar lifecycle no prefixo para expirar versoes atuais apos 30 dias, versoes
nao atuais apos 30 dias, e abortar multipart incompleto apos 1 dia. Com versionamento,
os bytes de uma versao expirada podem permanecer mais 30 dias. Rever custos de
armazenamento, job e transferencia; a verificacao rele o arquivo inteiro do S3.

## Permissoes AWS do job

A chave criada anteriormente apenas para listar buckets nao serve para backups.
Usar uma identidade dedicada com esta politica, substituindo NOME_DO_BUCKET:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload"],
    "Resource": "arn:aws:s3:::NOME_DO_BUCKET/smartic-pro/postgres/*"
  }]
}
```

O processo nao apaga backups nem modifica a base de origem. Cada execucao cria um
nome unico. Publica o manifesto .dump.json apenas depois de pg_dump terminar,
pg_restore conseguir listar o arquivo, e uma leitura do S3 confirmar SHA-256 e tamanho.
Um arquivo sem manifesto nao deve ser considerado backup confirmado.
A verificacao do arquivo nao substitui um teste real de restauro.

## Teste de recuperacao obrigatorio antes de considerar a protecao ativa

1. Obter o arquivo .dump e o manifesto .dump.json correspondentes com uma identidade
   de recuperacao. Comparar o SHA-256 do arquivo com o manifesto.
2. Criar uma base PostgreSQL NOVA e vazia, separada da producao, com versao compativel
   e as extensoes necessarias. Fornecer a ligacao dessa base atraves de PGDATABASE
   no ambiente do terminal; nunca reutilizar DATABASE_URL de producao para o restauro.
3. Executar `pg_restore --no-password --exit-on-error --single-transaction --no-owner --no-acl --dbname="$PGDATABASE" backup.dump`
   num terminal Linux com essas variaveis preparadas. Nao usar --clean contra producao.
4. Verificar tabelas, contagens e dados essenciais (clientes, obras, pagamentos),
   incluindo dados recentes. Testar a aplicacao isolada e registar duracao e resultado.
5. Recriar/verificar roles e permissoes da aplicacao: pg_dump nao inclui roles globais
   e este restauro nao aplica ownership/ACLs antigos. Guardar essa configuracao separadamente.

Este backup inclui a base PostgreSQL, nao ficheiros em S3/SharePoint, segredos do
Render nem codigo. Esses elementos precisam da sua propria recuperacao. Preservar
o commit da aplicacao correspondente e repetir o teste de restauro periodicamente.

Referencias:
- https://render.com/docs/cronjobs
- https://render.com/docs/blueprint-spec
- https://www.postgresql.org/docs/current/app-pgdump.html
- https://www.postgresql.org/docs/current/app-pgrestore.html
- https://docs.aws.amazon.com/boto3/latest/guide/s3-uploading-files.html
