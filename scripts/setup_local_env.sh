#!/bin/bash
# Prepare a machine to run the test suite and a local dev server.
# Idempotent: safe to run repeatedly. Never touches Supabase.
set -euo pipefail

cd "$(dirname "$0")/.."

SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

as_postgres() {
  if [ "$(id -u)" -eq 0 ]; then su postgres -c "$(printf '%q ' "$@")"; else sudo -u postgres "$@"; fi
}

if ! command -v pg_ctlcluster >/dev/null 2>&1; then
  echo "Installing PostgreSQL..."
  $SUDO apt-get update -qq
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq postgresql >/dev/null
fi

PG_VERSION="$(ls /usr/lib/postgresql | sort -n | tail -1)"
if ! pg_lsclusters -h | awk -v v="$PG_VERSION" '$1==v && $2=="main"' | grep -q online; then
  $SUDO pg_ctlcluster "$PG_VERSION" main start
fi

for _ in $(seq 1 20); do
  as_postgres psql -qtAc "SELECT 1" >/dev/null 2>&1 && break
  sleep 0.5
done

as_postgres psql -q -c "ALTER USER postgres PASSWORD 'postgres';"
for db in gardenpal_test gardenpal_dev; do
  if [ -z "$(as_postgres psql -qtAc "SELECT 1 FROM pg_database WHERE datname='$db'")" ]; then
    as_postgres psql -q -c "CREATE DATABASE $db;"
  fi
done

# --ignore-installed blinker: Ubuntu ships a system blinker pip can't uninstall.
python3 -m pip install -q --ignore-installed blinker -r requirements-dev.txt

echo "Local environment ready: Postgres $PG_VERSION, databases gardenpal_test and gardenpal_dev."
