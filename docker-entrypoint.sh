#!/bin/sh
# ForgeOS API container entrypoint (infra-owned).
# Runs Alembic migrations for the forge-db package, then execs the CMD
# (uvicorn). Safe to re-run: `alembic upgrade head` is idempotent.
#
# Layout contract (from the api workstream): the forge-db package dir contains
# alembic/alembic.ini and is invoked from the package dir as:
#     alembic -c alembic/alembic.ini upgrade head
set -eu

echo "[entrypoint] running database migrations..."

MIGRATED=0
for d in /srv/forge-os/packages/forge-db /app/packages/forge-db /app/forge-db /packages/forge-db /opt/forge-db .; do
  if [ -f "$d/alembic/alembic.ini" ]; then
    echo "[entrypoint] forge-db found at $d"
    (cd "$d" && alembic -c alembic/alembic.ini upgrade head)
    MIGRATED=1
    break
  fi
done

if [ "$MIGRATED" = "0" ]; then
  echo "[entrypoint] WARNING: no forge-db alembic.ini found; skipping migrations." >&2
  echo "[entrypoint] Searched: /srv/forge-os/packages/forge-db /app/packages/forge-db /app/forge-db /packages/forge-db /opt/forge-db ." >&2
fi

echo "[entrypoint] starting: $*"
exec "$@"
