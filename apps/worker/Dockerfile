# ForgeOS worker image.
#
# Build context MUST be the repo root (forge-os/), because the workspace
# packages live outside apps/worker:
#   docker build -f apps/worker/Dockerfile -t forgeos-worker .
#
# NOTE for infra: docker-compose.yml currently sets the worker build context
# to ./apps/worker, under which `COPY packages/` below cannot resolve.
# Either change the compose context to the repo root (like this Dockerfile
# and apps/api/Dockerfile both assume) or vendor the packages into the
# context before building.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Workspace packages first (better layer caching).
COPY packages/ /srv/packages/
COPY apps/worker/ /srv/worker/
COPY apps/api/app/ /srv/worker/app/

WORKDIR /srv/worker
# requirements.txt references the local packages via -e ../../packages/...,
# which resolves to /srv/packages/... relative to this file.
RUN pip install --no-cache-dir -r requirements.txt

# Drop privileges: the worker never binds ports or writes to disk.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /srv/packages /srv/worker
USER appuser

# arq CLI resolves `worker.settings` from the working directory.
CMD ["arq", "worker.settings.WorkerSettings"]
