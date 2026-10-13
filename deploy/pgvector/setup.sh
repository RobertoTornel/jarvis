#!/usr/bin/env bash
# Idempotent setup for the claire-pg pgvector container.
#
# Guarantees:
#   * Safe to re-run against the live container.
#   * NEVER drops a database, NEVER drops a table, NEVER deletes a volume.
#
# What it does:
#   1. Loads ~/.config/claire/pg.env (CLAIRE_PG_PASSWORD and friends).
#   2. Waits until the container is ready (pg_isready).
#   3. Creates the claire_test database if it does not already exist.
#   4. Enables the `vector` extension in both `claire` and `claire_test`.
#   5. Prints a final summary.
set -euo pipefail

CONTAINER="${CLAIRE_PG_CONTAINER:-claire-pg}"
ENV_FILE="${CLAIRE_PG_ENV_FILE:-$HOME/.config/claire/pg.env}"
PG_USER="${CLAIRE_PG_USER:-claire}"
MAIN_DB="${CLAIRE_PG_DB:-claire}"
TEST_DB="${CLAIRE_PG_TEST_DB:-claire_test}"

die() { echo "ERROR: $*" >&2; exit 1; }

# --- 1. Load environment -----------------------------------------------------
[ -f "$ENV_FILE" ] || die "env file not found: $ENV_FILE"
# shellcheck disable=SC1090
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a
# Use the value from pg.env when present; otherwise keep the defaults above.
PG_USER="${CLAIRE_PG_USER:-$PG_USER}"
MAIN_DB="${CLAIRE_PG_DB:-$MAIN_DB}"
TEST_DB="${CLAIRE_PG_TEST_DB:-$TEST_DB}"
: "${CLAIRE_PG_PASSWORD:?CLAIRE_PG_PASSWORD is not set in $ENV_FILE}"

command -v docker >/dev/null 2>&1 || die "docker not found in PATH"

# --- 2. Container + readiness ------------------------------------------------
[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ] \
  || die "container '$CONTAINER' is not running (start it with: docker compose -f deploy/pgvector/docker-compose.yml up -d)"

echo "==> Waiting for PostgreSQL in '$CONTAINER' to accept connections ..."
ready=0
for _ in $(seq 1 60); do
  if docker exec "$CONTAINER" pg_isready -U "$PG_USER" -d "$MAIN_DB" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[ "$ready" -eq 1 ] || die "PostgreSQL did not become ready within 60s"

# Run SQL inside the container as the postgres superuser.
# Password comes from CLAIRE_PG_PASSWORD (from pg.env) via PGPASSWORD, never hardcoded.
sql() {
  local db="$1"
  docker exec -e PGPASSWORD="$CLAIRE_PG_PASSWORD" "$CONTAINER" \
    psql -U "$PG_USER" -d "$db" -X -q -v ON_ERROR_STOP=1 -tAc "$2"
}

# --- 3. Ensure claire_test exists (never drops anything) ---------------------
echo "==> Ensuring database '$TEST_DB' exists ..."
db_count="$(sql "$MAIN_DB" "SELECT count(*) FROM pg_database WHERE datname = '${TEST_DB}';")"
if [ "$db_count" = "1" ]; then
  echo "    '$TEST_DB' already exists."
else
  echo "    '$TEST_DB' missing -> creating."
  sql "$MAIN_DB" "CREATE DATABASE \"${TEST_DB}\";"
fi

# --- 4. Enable vector extension in both databases (IF NOT EXISTS => idempotent)
for db in "$MAIN_DB" "$TEST_DB"; do
  echo "==> Ensuring 'vector' extension in database '$db' ..."
  sql "$db" "CREATE EXTENSION IF NOT EXISTS vector;"
done

# --- 5. Summary --------------------------------------------------------------
echo
echo "==================== setup.sh summary ===================="
echo "Container : $CONTAINER (running)"
echo "Image     : $(docker inspect -f '{{.Config.Image}}' "$CONTAINER")"
echo "Server    : $(sql "$MAIN_DB" "SELECT version();" | cut -d',' -f1)"
echo "pgvector  : $(sql "$MAIN_DB" "SELECT extversion FROM pg_extension WHERE extname = 'vector';")"
echo "Databases :"
while IFS= read -r line; do
  [ -n "$line" ] || continue
  v="$(sql "$line" "SELECT extversion FROM pg_extension WHERE extname = 'vector';")"
  if [ -n "$v" ]; then
    echo "  - $line  (vector extension: $v)"
  else
    echo "  - $line  (vector extension: NOT INSTALLED)"
  fi
done < <(sql "$MAIN_DB" "SELECT datname FROM pg_database WHERE datallowconn AND datname IN ('${MAIN_DB}','${TEST_DB}') ORDER BY datname;")
echo "==========================================================="
echo "Nothing was dropped or deleted; re-running this script is always safe."
