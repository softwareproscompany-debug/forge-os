# ForgeOS worker image.
#
# Build context MUST be the repo root (forge-os/), because the workspace
# packages live outside apps/worker:
#   docker build -f apps/worker/Dockerfile -t forgeos-worker .
#
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Workspace packages first (better layer caching).
COPY packages/ /srv/forge-os/packages/
COPY apps/worker/ /srv/forge-os/apps/worker/
COPY apps/api/app/ /srv/forge-os/apps/worker/app/

WORKDIR /srv/forge-os/apps/worker
# Keep the repo's directory layout so -e ../../packages/... resolves correctly.
RUN pip install --no-cache-dir -r requirements.txt

# Drop privileges: the worker never binds ports or writes to disk.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /srv/forge-os/packages /srv/forge-os/apps/worker
USER appuser

# arq CLI resolves `worker.settings` from the working directory.
CMD ["arq", "worker.settings.WorkerSettings"]
