#!/usr/bin/env bash
# =====================================================================
# init_db.sh — wait for Postgres, then apply init.sql.
# ---------------------------------------------------------------------
# Honours the standard libpq env vars; falls back to dev defaults that
# match docker-compose. Safe to run repeatedly (init.sql is idempotent).
#
# Usage:
#   PGHOST=localhost PGPORT=5432 ./scripts/init_db.sh
# =====================================================================
set -euo pipefail

PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-${POSTGRES_USER:-qconnect}}"
PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-qconnect}}"
PGDATABASE="${PGDATABASE:-${POSTGRES_DB:-qconnect}}"
export PGPASSWORD

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INIT_SQL="${SCRIPT_DIR}/../cloud/databases/postgres/init.sql"

if [[ ! -f "${INIT_SQL}" ]]; then
  echo "ERROR: init.sql not found at ${INIT_SQL}" >&2
  exit 1
fi

echo "Waiting for Postgres at ${PGHOST}:${PGPORT} (db=${PGDATABASE}, user=${PGUSER}) ..."
ATTEMPTS=0
MAX_ATTEMPTS="${MAX_ATTEMPTS:-30}"
until pg_isready -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDATABASE}" >/dev/null 2>&1; do
  ATTEMPTS=$((ATTEMPTS + 1))
  if [[ "${ATTEMPTS}" -ge "${MAX_ATTEMPTS}" ]]; then
    echo "ERROR: Postgres not ready after ${MAX_ATTEMPTS} attempts" >&2
    exit 1
  fi
  sleep 2
done

echo "Postgres is ready. Applying init.sql ..."
psql -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDATABASE}" \
     -v ON_ERROR_STOP=1 -f "${INIT_SQL}"

echo "Schema applied successfully."
