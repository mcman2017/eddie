# syntax=docker/dockerfile:1.7
# Eddie web/API image: read-only, keyless, mock backend. Ember calls never run in a container.

# ---- web UI -----------------------------------------------------------------------------
FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- python runtime ----------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.10.0 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY eddie/ ./eddie/
RUN uv sync --frozen --no-dev
COPY --from=web /web/dist ./web/dist

ENV PATH=/opt/venv/bin:$PATH \
    HOME=/tmp \
    EDDIE_BACKEND=mock \
    EDDIE_DATA_DIR=/data \
    EDDIE_INDEX_PATH=/data/index/eddie_index.sqlite \
    EDDIE_PUBLIC_DIR=/public \
    EDDIE_WEB_DIST=/app/web/dist
USER 1000:1000
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2).status == 200 else 1)"]
CMD ["uvicorn", "eddie.api.app:app", "--host", "0.0.0.0", "--port", "8765", "--no-server-header"]
