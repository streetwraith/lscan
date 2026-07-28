# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first, so a code change does not re-resolve the whole environment.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# Hash static filenames into staticfiles/ (ManifestStaticFilesStorage, non-DEBUG). Settings
# read these from the environment, so the build needs throwaway values - collectstatic
# touches neither Postgres nor Redis, so unreachable URLs here are fine.
RUN DEBUG=0 \
    SECRET_KEY=build-only-not-a-real-secret \
    DATABASE_URL=postgres://build:build@127.0.0.1:1/none \
    REDIS_URL=redis://127.0.0.1:1/0 \
    uv run manage.py collectstatic --noinput


FROM python:3.13-slim-bookworm

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --system --uid 10001 --create-home lscan
COPY --from=build --chown=lscan:lscan /app /app
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

USER lscan
EXPOSE 8000

# python, not curl - curl is not in the slim runtime image. This sends `Host: 127.0.0.1:8000`,
# so ALLOWED_HOSTS must include 127.0.0.1 (see LSCAN-DEPLOY.md "the ALLOWED_HOSTS problem").
# If the deployment instead probes with the real Host header, drop this and let Coolify do it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz').read()"]

# lscan owns no tables, so there is no migrate step - see PROJECT.md.
ENTRYPOINT ["docker-entrypoint.sh"]
