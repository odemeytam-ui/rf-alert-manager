# One image, two services: SERVICE=ingest or SERVICE=alerts picks the app.

# ---- build stage: resolve dependencies with uv into a virtualenv ----
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# ---- runtime stage: no uv, no build tools, non-root ----
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
COPY --chown=app:app config ./config
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SERVICE=ingest \
    PORT=8000
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=2)"
CMD ["sh", "-c", "exec uvicorn --factory rfam.${SERVICE}.app:build --host 0.0.0.0 --port ${PORT} --no-access-log"]
