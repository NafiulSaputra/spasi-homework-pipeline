#!/bin/bash
# Startup script worker VM SPASI (dirender oleh Terraform templatefile).
# Jalan otomatis setiap VM boot. CI me-reset VM setelah push image baru.
set -euo pipefail
exec > /var/log/spasi-startup.log 2>&1

# 1. Docker (Debian GCE image sudah membawa gcloud CLI)
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y docker.io
  systemctl enable --now docker
fi
gcloud auth configure-docker ${region}-docker.pkg.dev --quiet

# 2. Environment file (izin 600) — secret TIDAK ditaruh di command line
ENV_FILE=/etc/spasi.env
umask 077
: > "$ENV_FILE"
${secret_env_lines}
cat >> "$ENV_FILE" <<EOV
DB_HOST=sqlproxy
DB_PORT=5432
DB_NAME=${db_name}
DB_USER=${db_user}
REDIS_HOST=${redis_host}
REDIS_PORT=${redis_port}
GCS_BUCKET_NAME=${bucket_name}
SPREADSHEET_ID=${spreadsheet_id}
SHEETS_SYNC_ENABLED=${spreadsheet_id == "" ? "0" : "1"}
EOV

# 3. Container: Cloud SQL Auth Proxy + Worker + Scheduler
docker network inspect spasi >/dev/null 2>&1 || docker network create spasi
docker pull ${worker_image}
docker rm -f sqlproxy spasi-worker spasi-scheduler >/dev/null 2>&1 || true

docker run -d --name sqlproxy --network spasi --restart=always \
  gcr.io/cloud-sql-connectors/cloud-sql-proxy:2.11.4 \
  --address 0.0.0.0 --port 5432 ${sql_connection}

docker run -d --name spasi-worker --network spasi --restart=always \
  --env-file "$ENV_FILE" ${worker_image}

docker run -d --name spasi-scheduler --network spasi --restart=always \
  --env-file "$ENV_FILE" ${worker_image} python scheduler.py

echo "SPASI worker VM siap."
