#!/usr/bin/env bash
#
# One-command demo: spins up a throwaway seeded Postgres, starts the API and the
# web dev server, and prints the demo login. Everything is torn down on exit —
# including when the script is killed outright (see "Teardown" below).
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

# ── Teardown ──────────────────────────────────────────────────────────────────
# The throwaway Postgres runs detached (`pg_ctl start` gives it its own session),
# so it does not stop just because this script does. The Claude desktop app stops
# a demo by sending SIGTERM to the script's process group and, ~3 s later,
# SIGKILL — which no trap can catch. With only an EXIT trap, every stopped demo
# left a Postgres running and its temp directory behind. So teardown has three
# layers, all safe to run more than once:
#   1. an EXIT trap — Ctrl-C and ordinary exits;
#   2. a watchdog in its own process group, out of reach of signals sent to ours,
#      that tears down as soon as this script is gone, however it died;
#   3. at startup, a sweep of leftovers from earlier demos whose script is gone
#      (e.g. if a watchdog was killed as well).
# Each demo's files live in $TMPDIR/meter-demo.XXXXXX, with the script's pid in
# owner.pid.
TMPROOT="${TMPDIR:-/tmp}"
TMPROOT="${TMPROOT%/}"

# Stop the Postgres of demo directory $1, if it is still running. Immediate mode:
# the data is throwaway, and the stop has to fit inside the app's kill window.
stop_postgres() {
  local pid
  pid="$(head -n 1 "$1/data/postmaster.pid" 2>/dev/null || true)"
  [ -n "$pid" ] || return 0
  # Pids get reused: only touch the process if it is this demo's postmaster.
  case "$(ps -o command= -p "$pid" 2>/dev/null || true)" in
    *postgres*"/$(basename "$1")/data"*) ;;
    *) return 0 ;;
  esac
  pg_ctl -D "$1/data" stop -m immediate -w -t 10 >/dev/null 2>&1 \
    || kill -KILL "$pid" 2>/dev/null || true
}

teardown() {
  stop_postgres "$1"
  rm -rf "$1"
}

# Layer 3 — leftovers from earlier demos.
for old in "$TMPROOT"/meter-demo.*; do
  [ -f "$old/owner.pid" ] || continue
  kill -0 "$(cat "$old/owner.pid")" 2>/dev/null && continue  # that demo is still running
  echo "▶ Removing a leftover demo database ($old)…"
  teardown "$old"
done

TMPD="$(mktemp -d "$TMPROOT/meter-demo.XXXXXX")"
echo "$$" > "$TMPD/owner.pid"
UVPID=""

# Layer 2 — the watchdog. `set -m` starts it in its own process group; its stdio
# is /dev/null so it never holds the launcher's output open.
DEMO_PID=$$
set -m
(
  while kill -0 "$DEMO_PID" 2>/dev/null; do sleep 1; done
  teardown "$TMPD"
) </dev/null >/dev/null 2>&1 &
set +m

# Layer 1 — the EXIT trap.
cleanup() {
  [ -n "$UVPID" ] && kill "$UVPID" 2>/dev/null || true
  teardown "$TMPD"
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
# The SELECT-only role (migration 0060), which /api/mcp narrows every call to.
# Same reasoning as the line above: without it the MCP endpoint answers 503
# rather than falling back to a role that can write, and the demo would show a
# broken feature instead of the real one.
export DATABASE_READ_URL="host=$TMPD port=5544 dbname=meter user=meter_read"
# The one browser Origin the MCP endpoint answers. In the demo the app is served
# by Vite, so that is where an in-product client would be.
export METER_APP_ORIGIN="http://localhost:5173"
export APP_SECRET_KEY="demo-secret-change-me"

# Ask the seeder for the account it actually creates, so the admin list and the
# banner below can never drift from it (and any DEMO_USER_* override is honoured).
[ -x backend/.venv/bin/python ] || { echo "✖ Backend venv not found. Run: make install"; exit 1; }
DEMO_CREDS="$( cd backend && .venv/bin/python -c 'import seed; print(seed.DEMO_USER_EMAIL); print(seed.DEMO_USER_PASSWORD)' )"
DEMO_EMAIL="$(printf '%s\n' "$DEMO_CREDS" | sed -n 1p)"
DEMO_PASSWORD="$(printf '%s\n' "$DEMO_CREDS" | sed -n 2p)"

# The demo account is an admin here so the internal Admin Portal is explorable in
# the throwaway demo. In production, set METER_ADMIN_EMAILS to your own admins.
export METER_ADMIN_EMAILS="$DEMO_EMAIL"
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

# Box drawn to fit its contents, so a longer demo address stays aligned.
BOX_L1="Open http://localhost:5173"
BOX_L2="Login:  $DEMO_EMAIL  /  $DEMO_PASSWORD"
BOX_W=56
[ ${#BOX_L1} -gt $((BOX_W - 4)) ] && BOX_W=$((${#BOX_L1} + 4))
[ ${#BOX_L2} -gt $((BOX_W - 4)) ] && BOX_W=$((${#BOX_L2} + 4))
BOX_RULE="$(printf '─%.0s' $(seq 1 "$BOX_W"))"

echo ""
printf '  ┌%s┐\n' "$BOX_RULE"
printf '  │  %-*s  │\n' "$((BOX_W - 4))" "$BOX_L1"
printf '  │  %-*s  │\n' "$((BOX_W - 4))" "$BOX_L2"
printf '  └%s┘\n' "$BOX_RULE"
echo ""
echo "▶ Starting web on http://localhost:5173  (Ctrl-C to stop everything)…"
cd web && npm run dev
