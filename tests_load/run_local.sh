#!/usr/bin/env bash
# Start the core and BioBridge (4 workers each) on a PostgreSQL server, a fake
# BioTime, run the concurrency checks, and stop everything again.
#
#   PGHOST=/var/run/postgresql PGPORT=5432 PGUSER=postgres tests_load/run_local.sh [accounts]
#
# Creates (and drops first) the databases ob_load_core and ob_load_bb.
set -euo pipefail
cd "$(dirname "$0")/.."
ACCOUNTS="${1:-30}"
WORKERS="${WORKERS:-4}"
PGHOST="${PGHOST:-localhost}"; PGPORT="${PGPORT:-5432}"; PGUSER="${PGUSER:-postgres}"
OUT="$(mktemp -d)"
dsn() { echo "postgresql://${PGUSER}${PGPASSWORD:+:$PGPASSWORD}@/${1}?host=${PGHOST}&port=${PGPORT}"; }
for db in ob_load_core ob_load_bb; do
  psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -q -c "drop database if exists $db" -c "create database $db"
done
pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; echo "Logs: $OUT"; }
trap cleanup EXIT

TX_DELAY=2 python tests_load/fake_biotime.py > "$OUT/biotime.log" 2>&1 & pids+=($!)
(cd core && exec env DATABASE_URL="$(dsn ob_load_core | sed 's#^postgresql://#postgresql+psycopg://#')" \
  STAFF_EMAIL=staff@load.test STAFF_PASSWORD=Staff-Pass-12 \
  BIOMETRIC_CLIENT_SECRET=load-secret-123456 BIOMETRIC_BASE_URL=http://127.0.0.1:8201 \
  BIOMETRIC_INTERNAL_URL=http://127.0.0.1:8201 PUBLIC_BASE_URL=http://127.0.0.1:8200 \
  SMTP_HOST= VERIFY_EMAIL_DELIVERABILITY=false REGISTRATION_RATE_PER_HOUR=100000 \
  python -m uvicorn obcore.main:app --port 8200 --workers "$WORKERS" > "$OUT/core.log" 2>&1) & pids+=($!)
BB_ENV=(DATABASE_URL="$(dsn ob_load_bb | sed 's#^postgresql://#postgresql+psycopg://#')" PLATFORM_MODE=true
  PLATFORM_CORE_URL=http://127.0.0.1:8200 PLATFORM_CORE_INTERNAL_URL=http://127.0.0.1:8200
  PLATFORM_CLIENT_SECRET=load-secret-123456 ALLOW_PRIVATE_NETWORK_TARGETS=true VERIFY_EMAIL_DELIVERABILITY=false)
(cd services/biobridge && env "${BB_ENV[@]}" python tools/db_upgrade.py)
(cd services/biobridge && exec env "${BB_ENV[@]}" \
  python -m uvicorn app.main:app --port 8201 --workers "$WORKERS" > "$OUT/biobridge.log" 2>&1) & pids+=($!)
for url in http://127.0.0.1:8200/health http://127.0.0.1:8201/health; do
  for _ in $(seq 60); do curl -fs "$url" >/dev/null && break; sleep 1; done
done

python tests_load/concurrency_check.py --accounts "$ACCOUNTS" --core-log "$OUT/core.log" \
  --core-db "$(dsn ob_load_core)" --bb-db "$(dsn ob_load_bb)"
