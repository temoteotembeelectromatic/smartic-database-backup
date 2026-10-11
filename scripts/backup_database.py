"""Create a PostgreSQL recovery archive and verify its private S3 upload."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from uuid import uuid4


def digest_stream(stream):
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def run_backup(s3, database_url, bucket, prefix="smartic-pro/postgres"):
    if not database_url or not bucket or not prefix.strip("/"):
        raise ValueError("DATABASE_URL, BACKUP_S3_BUCKET e prefixo sao obrigatorios.")
    dump = shutil.which("pg_dump")
    restore = shutil.which("pg_restore")
    if not dump or not restore:
        raise RuntimeError("Instale pg_dump e pg_restore antes de executar o backup.")

    started = datetime.now(timezone.utc)
    key = (f"{prefix.strip('/')}/{started:%Y/%m/%d}/"
           f"{started:%H%M%S}-{uuid4().hex}.dump")
    # Pass the connection string through the child environment, never argv/logs.
    env = os.environ.copy()
    env["PGDATABASE"] = database_url
    env.setdefault("PGSSLMODE", "require")
    env.setdefault("PGCONNECT_TIMEOUT", "30")
    with tempfile.TemporaryDirectory(prefix="smartic-db-backup-") as directory:
        archive = Path(directory) / "database.dump"
        result = subprocess.run(
            [dump, "--format=custom", "--no-password", "--lock-wait-timeout=60s",
             "--file", str(archive)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=7200,
        )
        if result.returncode:
            # PostgreSQL errors may contain connection details or row contents.
            raise RuntimeError("pg_dump falhou; nenhum backup foi enviado. Verifique acesso, versao e espaco.")
        result = subprocess.run(
            [restore, "--list", str(archive)], stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=120,
        )
        if result.returncode:
            raise RuntimeError("Arquivo rejeitado por pg_restore; nenhum backup foi enviado.")
        with archive.open("rb") as stream:
            digest, size = digest_stream(stream)
        if not size:
            raise RuntimeError("Arquivo vazio; nenhum backup foi enviado.")

        s3.upload_file(str(archive), bucket, key, ExtraArgs={
            "ServerSideEncryption": "AES256",
            "ChecksumAlgorithm": "SHA256",
            "Metadata": {"sha256": digest},
        })
        # Read back the actual bytes; matching metadata alone cannot prove integrity.
        response = s3.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            remote_digest, remote_size = digest_stream(body)
        finally:
            body.close()
        if (remote_digest, remote_size) != (digest, size):
            raise RuntimeError("Falha de integridade no S3; backup nao confirmado.")

        manifest = {
            "archive_key": key, "sha256": digest, "bytes": size,
            "started_at": started.isoformat(),
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "format": "pg_dump-custom", "restore_tested": False,
        }
        s3.put_object(
            Bucket=bucket, Key=key + ".json",
            Body=json.dumps(manifest).encode("utf-8"),
            ContentType="application/json", ServerSideEncryption="AES256",
        )
    return manifest


def main():
    try:
        import boto3

        manifest = run_backup(
            boto3.client("s3"), os.environ.get("DATABASE_URL"),
            os.environ.get("BACKUP_S3_BUCKET"),
            os.environ.get("BACKUP_S3_PREFIX", "smartic-pro/postgres"),
        )
    except Exception as error:
        # Do not expose URLs, credentials or database content in unattended logs.
        print(f"Backup falhou ({type(error).__name__}); verificar configuracao/acesso e dependencias.", file=sys.stderr)
        return 1
    print(f"Backup enviado e integridade verificada: {manifest['archive_key']} ({manifest['bytes']} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
