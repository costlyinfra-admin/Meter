#!/usr/bin/env bash
#
# Release tasks: apply DB migrations and ensure the app-role password.
# Idempotent — safe to run on every deploy. Requires DATABASE_URL (the owner/admin
# connection) and, in production, METER_APP_DB_PASSWORD.
#
set -euo pipefail

echo "▶ Applying database migrations…"
python -m meter.migrations

if [ -n "${METER_APP_DB_PASSWORD:-}" ]; then
  echo "▶ Ensuring the application role password…"
  # psql :'pw' interpolation safely quotes any password, but it only works when
  # the SQL arrives via stdin/file — NOT via `-c` (which sends literal SQL).
  psql "$DATABASE_URL" --no-psqlrc -v ON_ERROR_STOP=1 -v "pw=$METER_APP_DB_PASSWORD" <<'SQL'
ALTER ROLE meter_app WITH LOGIN PASSWORD :'pw';
SQL
fi

echo "✓ Release tasks complete."
