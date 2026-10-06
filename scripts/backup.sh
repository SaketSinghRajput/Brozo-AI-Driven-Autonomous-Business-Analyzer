#!/usr/bin/env sh
# shellcheck disable=SC2016  # single-quoted $VARS expand inside the container, on purpose
# Backup and restore drill (TASK-INFRA-008, design section 82). PostgreSQL and the dataset store
# are irreplaceable; model weights, caches and vector indexes are rebuildable and not included.
#
#   scripts/backup.sh create [out_dir]   default: $BACKUP_ROOT (~/analyzer-backups)/<UTC time>
#   scripts/backup.sh restore <dir> <target_db> [datastore_dir]
#
# create  - read-only against the running stack: pg_dump (custom format, one consistent
#           snapshot), tar of the datastore volume, per-table row counts, SHA256SUMS.
# restore - verifies SHA256SUMS, creates <target_db> (must not exist: never overwrites a live
#           database), pg_restore --exit-on-error, compares row counts with the backup, and
#           extracts the datastore into [datastore_dir] (default <dir>/restored) + lists files.
#           Switching the app to the restored database is a deliberate manual step (MLOPS.md).
# Env: COMPOSE_PROJECT (default analyzer), ENV_FILE (default <repo>/.env),
#      COMPOSE_PROD=1 for a stack started with compose.prod.yml.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE=${ENV_FILE:-$ROOT/.env}
BASE="$ROOT/infra/compose/compose.yml"
PRODF="$ROOT/infra/compose/compose.prod.yml"
PROJECT=${COMPOSE_PROJECT:-analyzer}

dc() {
  if [ "${COMPOSE_PROD:-0}" = 1 ]; then
    docker compose -f "$BASE" -f "$PRODF" --env-file "$ENV_FILE" -p "$PROJECT" "$@"
  else
    docker compose -f "$BASE" --env-file "$ENV_FILE" -p "$PROJECT" "$@"
  fi
}

# psql as the schema owner over the container's local socket: no password leaves the container.
psql_in() { # $1 = database ("" = POSTGRES_DB); SQL on stdin
  dc exec -T -e TARGET_DB="$1" postgres sh -c \
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${TARGET_DB:-$POSTGRES_DB}" -AtF "	" -f -'
}

row_counts() { # exact counts of every public table (superuser: RLS does not hide rows)
  psql_in "$1" <<'SQL'
SELECT c.relname,
       (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM public.%I',
        c.relname), false, true, '')))[1]::text
FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
WHERE ns.nspname = 'public' AND c.relkind IN ('r', 'p')
ORDER BY 1;
SQL
}

create() {
  out=${1:-${BACKUP_ROOT:-$HOME/analyzer-backups}/$(date -u +%Y%m%dT%H%M%SZ)}
  mkdir -p "$out"
  echo "backup: project=$PROJECT -> $out"
  dc exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' >"$out/postgres.dump"
  row_counts "" >"$out/row_counts.tsv"  # right after the dump; concurrent writes make it approximate
  # Inside `sh -c`: Git Bash would rewrite a bare "/data" argument into a Windows path.
  dc exec -T api sh -c 'tar -C /data -czf - datastore' >"$out/datastore.tar.gz"
  (cd "$out" && sha256sum postgres.dump datastore.tar.gz row_counts.tsv >SHA256SUMS)
  echo "backup ok: $(wc -l <"$out/row_counts.tsv" | tr -d ' ') tables," \
    "dump $(wc -c <"$out/postgres.dump" | tr -d ' ') bytes," \
    "datastore $(tar -tzf - <"$out/datastore.tar.gz" | grep -vc '/$') files"
}

restore() {
  dir=${1:?backup dir required}
  target=${2:?target database name required}
  restored=${3:-$dir/restored}
  case $target in *[!a-z0-9_]*) echo "target_db must match [a-z0-9_]+" >&2; exit 2 ;; esac
  (cd "$dir" && sha256sum -c --quiet SHA256SUMS) && echo "checksums ok"

  echo "CREATE DATABASE $target;" | psql_in ""  # fails if it exists: never overwrites
  dc exec -T -e TARGET_DB="$target" postgres sh -c \
    'pg_restore --exit-on-error -U "$POSTGRES_USER" -d "$TARGET_DB"' <"$dir/postgres.dump"
  row_counts "$target" >"$dir/restored_row_counts.tsv"
  if diff "$dir/row_counts.tsv" "$dir/restored_row_counts.tsv" >/dev/null; then
    echo "row counts match: $(wc -l <"$dir/row_counts.tsv" | tr -d ' ') tables"
  else
    echo "ROW COUNT MISMATCH (source vs restored):" >&2
    diff "$dir/row_counts.tsv" "$dir/restored_row_counts.tsv" >&2 || true
    exit 1
  fi

  mkdir -p "$restored"
  (cd "$restored" && tar -xzf -) <"$dir/datastore.tar.gz"
  echo "datastore restored to $restored: $(find "$restored" -type f | wc -l | tr -d ' ') files"
}

cmd=${1:-}
[ $# -gt 0 ] && shift
case "$cmd" in
  create) create "$@" ;;
  restore) restore "$@" ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
