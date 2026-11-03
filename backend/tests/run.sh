#!/bin/bash
# Runs the backend tests in a one-off container. See backend/tests/README.md.
set -euo pipefail

E2E=0
[[ "${1:-}" == "--e2e" ]] && E2E=1

PG_USER="$(grep '^POSTGRES_USER=' .env | cut -d= -f2 | tr -d '\r')"
PG_PASSWORD="$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2 | tr -d '\r')"
TEST_URL="postgresql+asyncpg://${PG_USER}:${PG_PASSWORD}@postgres:5432/meshpilot_test"

if [[ "$E2E" == "1" ]]; then
    # Always a clean database, so the deliberately tampered audit chain from the
    # last run can't affect this one. This is never the real meshpilot database.
    docker exec meshpilot-postgres psql -q -U "$PG_USER" -d postgres -c "DROP DATABASE IF EXISTS meshpilot_test"
    docker exec meshpilot-postgres psql -q -U "$PG_USER" -d postgres -c "CREATE DATABASE meshpilot_test"
fi

MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -T \
    -v "$(pwd)/infra/docker-proxy:/docker-proxy:ro" \
    -e DATABASE_URL="$TEST_URL" \
    -e MESHPILOT_E2E="$E2E" \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e HOME=/tmp \
    api sh -c '
        pip install --quiet --no-warn-script-location --target /tmp/dev "pytest==8.2.2" "anyio==4.4.0" "openai==1.40.0" "httpx==0.27.0" 2>&1 | grep -v "dependency resolver" || true
        export PYTHONPATH=/tmp/dev:/app
        [ "$MESHPILOT_E2E" = "1" ] && alembic upgrade head
        python -m pytest -p no:cacheprovider -q tests
    '
