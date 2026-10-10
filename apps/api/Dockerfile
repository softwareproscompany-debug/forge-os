FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/forge-os

# Workspace packages first (better layer caching). forge-llm / forge-channels
# are owned by other workstreams; install whichever are present so the image
# still builds before they land.
COPY packages/ ./packages/
COPY apps/api/requirements.txt ./apps/api/requirements.txt
RUN pip install --no-cache-dir -r apps/api/requirements.txt \
    && for p in forge-db forge-llm forge-channels; do \
         if [ -f "./packages/$p/pyproject.toml" ]; then \
           pip install --no-cache-dir "./packages/$p"; \
         else \
           echo "skipping missing package: $p"; \
         fi; \
       done

COPY apps/api/ ./apps/api/

# Drop privileges: the API binds 8000 (unprivileged port) and never writes
# to disk at runtime (PYTHONDONTWRITEBYTECODE=1, no file outputs in app/).
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /srv/forge-os
USER appuser

WORKDIR /srv/forge-os/apps/api
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import http.client; c=http.client.HTTPConnection('localhost',8000,timeout=4); c.request('GET','/healthz'); raise SystemExit(0 if c.getresponse().status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
