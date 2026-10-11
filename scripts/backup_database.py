"""Create a PostgreSQL recovery archive and verify its private S3 upload."""

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from uuid import uuid4
from urllib.parse import parse_qsl, unquote, urlsplit


class BackupError(RuntimeError):
    """Only fixed, credential-free diagnostics may use this exception."""


def postgres_environment(database_url):
    """PGDATABASE accepts a database name, not a connection URI."""
    try:
        url = urlsplit(database_url.strip())
        if url.scheme not in {'postgres', 'postgresql'} or not url.hostname or not url.path.lstrip('/') or url.fragment:
            raise ValueError()
        values = {
            'PGHOST': unquote(url.hostname),
            'PGPORT': str(url.port or 5432),
            'PGDATABASE': unquote(url.path[1:]),
        }
        if url.username is not None:
            values['PGUSER'] = unquote(url.username)
        if url.password is not None:
            values['PGPASSWORD'] = unquote(url.password)
        query_keys = {
            'sslmode': 'PGSSLMODE', 'sslrootcert': 'PGSSLROOTCERT',
            'sslcert': 'PGSSLCERT', 'sslkey': 'PGSSLKEY',
            'connect_timeout': 'PGCONNECT_TIMEOUT', 'application_name': 'PGAPPNAME',
            'options': 'PGOPTIONS', 'channel_binding': 'PGCHANNELBINDING',
        }
        for key, value in parse_qsl(url.query, keep_blank_values=True):
            if key not in query_keys:
                raise BackupError('PG_URL_OPTION: DATABASE_URL tem um parametro nao suportado; use a URL PostgreSQL fornecida pelo Render.')
            values[query_keys[key]] = value
        if any('\x00' in value for value in values.values()):
            raise ValueError()
    except ValueError:
        raise BackupError('PG_URL: DATABASE_URL deve ser uma URL postgres:// ou postgresql:// valida, com servidor e nome da base.') from None
    env = os.environ.copy()
    # Avoid inherited connection/service settings redirecting the export.
    for key in ('PGHOST', 'PGHOSTADDR', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGSERVICE', 'PGSERVICEFILE', 'DATABASE_URL'):
        env.pop(key, None)
    env.update(values)
    env['LC_ALL'] = 'C'
    env.setdefault('PGSSLMODE', 'require')
    env.setdefault('PGCONNECT_TIMEOUT', '30')
    return env


def dump_failure(stderr):
    text = (stderr or b'').decode('utf-8', errors='replace').lower()
    reasons = [
        ('no such file or directory', 'PG_LOCAL: tentativa de ligacao local ou ficheiro necessario ausente. Confirme o servidor em DATABASE_URL.'),
        ('server does not support ssl', 'PG_SSL: o servidor nao suporta o SSL exigido. Confirme a ligacao e a configuracao SSL da base.'),
        ('password authentication failed', 'PG_AUTH: autenticacao rejeitada. Atualize DATABASE_URL com a ligacao correta.'),
        ('no password supplied', 'PG_PASSWORD: a ligacao nao forneceu a palavra-passe necessaria.'),
        ('could not translate host name', 'PG_DNS: hostname nao resolvido. Confirme DATABASE_URL e a regiao/rede do cron job.'),
        ('name or service not known', 'PG_DNS: hostname nao resolvido. Confirme DATABASE_URL e a regiao/rede do cron job.'),
        ('connection timed out', 'PG_TIMEOUT: ligacao expirou. Verifique rede, regiao e regras de acesso.'),
        ('timeout expired', 'PG_TIMEOUT: ligacao expirou. Verifique rede, regiao e regras de acesso.'),
        ('connection refused', 'PG_CONNECTION: ligacao recusada. Confirme servidor, porta e estado da base.'),
        ('server version mismatch', 'PG_VERSION: pg_dump e mais antigo que o servidor. Atualize o cliente PostgreSQL.'),
        ('permission denied', 'PG_PERMISSION: permissoes insuficientes. Verifique leitura de tabelas, sequencias e large objects e escrita no diretorio temporario.'),
        ('row-level security', 'PG_RLS: as regras de seguranca impedem a exportacao completa.'),
        ('no pg_hba.conf entry', 'PG_ACCESS: servidor rejeitou o acesso. Verifique utilizador, rede e SSL.'),
        ('no space left on device', 'PG_DISK: espaco temporario insuficiente para o dump.'),
        ('certificate verify failed', 'PG_CERTIFICATE: verificacao do certificado SSL falhou.'),
        ('does not exist', 'PG_NOT_FOUND: a base ou um objeto necessario nao existe. Confirme DATABASE_URL.'),
    ]
    for marker, message in reasons:
        if marker in text:
            return message
    return 'PG_DUMP: exportacao falhou por causa ainda nao classificada; stderr omitido para proteger dados e credenciais.'


def progress(stage):
    print(f'Backup etapa: {stage}', flush=True)


def s3_failure(error):
    """Emit only allowlisted AWS codes, never the upload error's raw message."""
    messages = {
        'AccessDenied': 'acesso negado. Verifique s3:PutObject no prefixo do bucket, politica do bucket e encriptacao exigida.',
        'AllAccessDisabled': 'acesso ao bucket desativado. Verifique o bucket e a conta AWS.',
        'NoSuchBucket': 'bucket inexistente. Confirme BACKUP_S3_BUCKET.',
        'InvalidAccessKeyId': 'chave AWS invalida. Confirme AWS_ACCESS_KEY_ID.',
        'SignatureDoesNotMatch': 'assinatura rejeitada. Confirme o par AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY e a regiao.',
        'ExpiredToken': 'credenciais temporarias expiradas. Renove as credenciais e AWS_SESSION_TOKEN.',
        'InvalidToken': 'token AWS invalido. Confirme AWS_SESSION_TOKEN para credenciais temporarias.',
        'AuthorizationHeaderMalformed': 'regiao ou autorizacao incorreta. Confirme AWS_DEFAULT_REGION.',
        'PermanentRedirect': 'endpoint incorreto. Confirme a regiao do bucket em AWS_DEFAULT_REGION.',
        'IllegalLocationConstraintException': 'regiao incorreta. Confirme AWS_DEFAULT_REGION.',
        'InvalidRequest': 'pedido rejeitado pelo S3. Verifique as exigencias de encriptacao e a configuracao do bucket.',
        'RequestTimeTooSkewed': 'relogio do sistema fora de sincronizacao.',
        'SlowDown': 'limite temporario de pedidos S3 atingido. Tente mais tarde.',
    }
    response = getattr(error, 'response', None)
    code = response.get('Error', {}).get('Code') if isinstance(response, dict) else None
    if code not in messages:
        # boto3 wraps ClientError in S3UploadFailedError and embeds its code.
        match = re.search(r'An error occurred \(([A-Za-z0-9]+)\)', str(error))
        code = match.group(1) if match else None
    if code in messages:
        return f'S3_{code}: {messages[code]}'
    return None


def digest_stream(stream):
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def run_backup(s3, database_url, bucket, prefix="smartic-pro/postgres"):
    if not database_url or not bucket or not prefix.strip("/"):
        raise BackupError("CONFIG: DATABASE_URL, BACKUP_S3_BUCKET e prefixo sao obrigatorios.")
    dump = shutil.which("pg_dump")
    restore = shutil.which("pg_restore")
    if not dump or not restore:
        raise BackupError("DEPENDENCIES: instale pg_dump e pg_restore antes de executar o backup.")

    started = datetime.now(timezone.utc)
    key = (f"{prefix.strip('/')}/{started:%Y/%m/%d}/"
           f"{started:%H%M%S}-{uuid4().hex}.dump")
    # Pass decoded connection parameters through the child environment, never argv/logs.
    env = postgres_environment(database_url)
    with tempfile.TemporaryDirectory(prefix="smartic-db-backup-") as directory:
        archive = Path(directory) / "database.dump"
        progress('exportar PostgreSQL')
        result = subprocess.run(
            [dump, "--format=custom", "--no-password", "--lock-wait-timeout=60s",
             "--file", str(archive)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=7200,
        )
        if result.returncode:
            # PostgreSQL errors may contain connection details or row contents.
            raise BackupError(dump_failure(getattr(result, 'stderr', b'')))
        progress('validar arquivo PostgreSQL')
        result = subprocess.run(
            [restore, "--list", str(archive)], stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=120,
        )
        if result.returncode:
            raise BackupError("PG_RESTORE: arquivo rejeitado; nenhum backup foi enviado.")
        with archive.open("rb") as stream:
            digest, size = digest_stream(stream)
        if not size:
            raise BackupError("EMPTY: arquivo vazio; nenhum backup foi enviado.")

        progress('enviar arquivo para S3')
        s3.upload_file(str(archive), bucket, key, ExtraArgs={
            "ServerSideEncryption": "AES256",
            "ChecksumAlgorithm": "SHA256",
            "Metadata": {"sha256": digest},
        })
        # Read back the actual bytes; matching metadata alone cannot prove integrity.
        progress('verificar integridade no S3')
        response = s3.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            remote_digest, remote_size = digest_stream(body)
        finally:
            body.close()
        if (remote_digest, remote_size) != (digest, size):
            raise BackupError("INTEGRITY: falha de integridade no S3; backup nao confirmado.")

        manifest = {
            "archive_key": key, "sha256": digest, "bytes": size,
            "started_at": started.isoformat(),
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "format": "pg_dump-custom", "restore_tested": False,
        }
        progress('publicar manifesto no S3')
        s3.put_object(
            Bucket=bucket, Key=key + ".json",
            Body=json.dumps(manifest).encode("utf-8"),
            ContentType="application/json", ServerSideEncryption="AES256",
        )
    return manifest


def main():
    try:
        progress('inicializar cliente S3')
        import boto3

        manifest = run_backup(
            boto3.client("s3"), os.environ.get("DATABASE_URL"),
            os.environ.get("BACKUP_S3_BUCKET"),
            os.environ.get("BACKUP_S3_PREFIX", "smartic-pro/postgres"),
        )
    except KeyboardInterrupt:
        print('Backup interrompido (SIGINT); esta execucao nao confirmou a conclusao. Verifique o manifesto no S3 antes de considerar o backup valido.', file=sys.stderr)
        return 130
    except BackupError as error:
        print(f'Backup falhou: {error}', file=sys.stderr)
        return 1
    except Exception as error:
        # Do not expose URLs, credentials or database content in unattended logs.
        diagnostic = s3_failure(error)
        if diagnostic:
            print(f'Backup falhou: {diagnostic}', file=sys.stderr)
        else:
            print(f"Backup falhou ({type(error).__name__}); verificar configuracao/acesso e dependencias.", file=sys.stderr)
        return 1
    print(f"Backup enviado e integridade verificada: {manifest['archive_key']} ({manifest['bytes']} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
