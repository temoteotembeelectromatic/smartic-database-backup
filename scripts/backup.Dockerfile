# pg_dump can read older servers, but cannot read a newer major version.
ARG POSTGRES_MAJOR=18
FROM postgres:${POSTGRES_MAJOR}-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && python3 -m venv /opt/backup-venv \
    && /opt/backup-venv/bin/pip install --no-cache-dir 'boto3>=1.36,<2' \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/backup
COPY backup_database.py ./backup_database.py
ENV PYTHONUNBUFFERED=1
USER postgres
# Replace the postgres image entrypoint: this container only runs the backup.
ENTRYPOINT ["/opt/backup-venv/bin/python", "/opt/backup/backup_database.py"]
CMD []
