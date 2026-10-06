#!/bin/sh
# Runs once, on first initialisation of the PostgreSQL data volume.
# POSTGRES_USER owns the schema and runs migrations; analyzer_app is the least-privilege
# application role (DML only, granted per table by migrations); analyzer_ro is read-only.
set -eu

psql -v ON_ERROR_STOP=1 -v app_pw="$APP_DB_PASSWORD" \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'EOSQL'
CREATE ROLE analyzer_app LOGIN PASSWORD :'app_pw';
CREATE ROLE analyzer_ro NOLOGIN;
GRANT USAGE ON SCHEMA public TO analyzer_app, analyzer_ro;
EOSQL
