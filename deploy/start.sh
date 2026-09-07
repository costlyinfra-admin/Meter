#!/usr/bin/env bash
#
# Container entrypoint: run release tasks (migrations + app-role password),
# then start the API (which also serves the built web app).
#
set -euo pipefail

/app/deploy/release.sh

exec uvicorn --factory meter.api:create_app --host 0.0.0.0 --port "${PORT:-8000}"
