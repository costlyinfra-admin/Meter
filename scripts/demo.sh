#!/usr/bin/env bash
#
# One-command demo: spins up a throwaway seeded Postgres, starts the API and the
# web dev server, and prints the demo login. Everything is torn down on exit.
#
# Prereqs (one time): `make install`, and Postgres 16 (`brew install postgresql@16`).
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Locate Postgres binaries (Homebrew keg-only path, or PATH).
PGBIN=""
for d in /opt/homebrew/opt/postgresql@16/bin /usr/local/opt/postgresql@16/bin; do
  [ -x "$d/initdb" ] && PGBIN="$d" && break
done
if [ -z "$PGBIN" ] && command -v initdb >/dev/null 2>&1; then
  PGBIN="$(dirname "$(command -v initdb)")"
fi
[ -n "$PGBIN" ] || { echo "✖ Postgres not found. Run: brew install postgresql@16"; exit 1; }
export PATH="$PGBIN:$PATH"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

TMPD="$(mktemp -d)"
UVPID=""
cleanup() {
  [ -n "$UVPID" ] && kill "$UVPID" 2>/dev/null || true
  pg_ctl -D "$TMPD/data" stop >/dev/null 2>&1 || true
  rm -rf "$TMPD"
}
trap cleanup EXIT INT TERM

echo "▶ Starting throwaway Postgres…"
initdb -D "$TMPD/data" >/dev/null
pg_ctl -D "$TMPD/data" -o "-k $TMPD -p 5544 -c listen_addresses=''" -l "$TMPD/log" -w start >/dev/null
createdb -h "$TMPD" -p 5544 meter

export DATABASE_URL="host=$TMPD port=5544 dbname=meter"
# The API connects as the non-privileged `meter_app` role — exactly as it does in
# production — so the demo exercises the real Row-Level Security path.
#
# WHY THIS LINE MATTERS: DATABASE_URL above is the cluster owner (initdb makes us
# a superuser), and RLS never applies to a superuser or to a table's owner. Without
# DATABASE_APP_URL, app_dsn() falls back to DATABASE_URL and every tenant-isolation
# policy is silently inert — a fresh tenant would see the seeded demo tenant's rows,
# so the demo would give a false pass on invariant 6 (per-tenant isolation).
#
# The role needs no setup here: migration 0002 creates `meter_app` and each
# migration grants it what it needs, all of which `make db-seed` applies below.
# DATABASE_URL stays the owner connection used by migrations, seeding and auth.
export DATABASE_APP_URL="host=$TMPD port=5544 dbname=meter user=meter_app"
export APP_SECRET_KEY="demo-secret-change-me"
# The demo account is an admin here so the internal Admin Portal is explorable in
# the throwaway demo. In production, set METER_ADMIN_EMAILS to your own admins.
export METER_ADMIN_EMAILS="demo@costlyinfra.com"
# Names the model that would see prompts, which prompt optimization's consent
# screen has to state. No key: discovery and the assistant fall back as usual,
# and the demo's prompt data is seeded rather than collected.
export METER_DISCOVERY_BASE_URL="${METER_DISCOVERY_BASE_URL:-https://api.groq.com/openai/v1}"
export METER_DISCOVERY_MODEL="${METER_DISCOVERY_MODEL:-openai/gpt-oss-120b}"

echo "▶ Migrating + seeding the Acme Security demo tenant…"
make db-seed

echo "▶ Starting API on http://localhost:8000 …"
( cd backend && exec .venv/bin/python -m uvicorn --factory meter.api:create_app --port 8000 --log-level warning ) &
UVPID=$!

echo ""
echo "  ┌────────────────────────────────────────────────────────┐"
echo "  │  Open http://localhost:5173                            │"
echo "  │  Login:  demo@costlyinfra.com  /  meter-demo           │"
echo "  └────────────────────────────────────────────────────────┘"
echo ""
echo "▶ Starting web on http://localhost:5173  (Ctrl-C to stop everything)…"
cd web && npm run dev
