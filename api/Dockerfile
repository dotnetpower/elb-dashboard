# syntax=docker/dockerfile:1.7
# Multi-stage build for the api sidecar (also used by the worker + beat
# sidecars; they override the entrypoint).
#
# Stage 1: use the official `uv` image to materialise the locked dependency
# set into a self-contained virtual environment at /app/.venv. Building from
# pyproject.toml + uv.lock guarantees the image is byte-identical to local
# `uv sync` runs.
# Stage 2: copy that venv onto a slim python runtime — no pip / uv / build
# toolchain ships in the final image.

FROM ghcr.io/astral-sh/uv:0.9-python3.12-bookworm-slim AS deps
WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

# Install runtime build deps for cryptography (pyjwt[crypto]) on slim base.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Bring in the lock metadata first so this layer caches across source-only changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.12-slim AS runtime
WORKDIR /app
# Build-time stamps so the running api can answer with its own release
# identity. Consumed by `api/__init__.py::_from_env`, which is the
# highest-priority source for `__version__` (see that module's
# `Risky contracts` for the full discovery order).
ARG APP_VERSION="0.0.0+unknown"
ARG APP_GIT_COMMIT=""
ARG APP_BUILD_TIME=""
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:${PATH}" \
    APP_VERSION=$APP_VERSION \
    APP_GIT_COMMIT=$APP_GIT_COMMIT \
    APP_BUILD_TIME=$APP_BUILD_TIME

# Copy the prebuilt venv from the deps stage.
COPY --from=deps /app/.venv /app/.venv

# Copy application source.
COPY api /app/api

# Run as non-root.
RUN groupadd -r app && useradd -r -g app -u 10001 app
USER 10001:10001

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).status==200 else 1)"

# Two workers because the api sidecar terminates WebSockets and
# does streaming upload/download proxying — one worker is too few even
# at low concurrency.
CMD ["uvicorn", "api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "2", \
     "--proxy-headers", \
     "--no-server-header", \
     "--forwarded-allow-ips", "*"]


# ---------------------------------------------------------------------------
# Optional dev stage — adds the Azure CLI so the local docker compose stack
# can use the developer's host `az login` (mounted via ~/.azure) instead of
# the production Managed Identity. This stage is opt-in via
# `build.target: dev` in scripts/dev/docker-compose.full.yml. Production
# images still use the `runtime` stage and stay slim.
# ---------------------------------------------------------------------------
FROM runtime AS dev
USER 0
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg lsb-release apt-transport-https \
    && curl -sL https://aka.ms/InstallAzureCLIDeb | bash \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
# Stay as root — compose runs api/worker/beat with user: 0:0 for bind-mount
# friendliness, and AZURE_CONFIG_DIR is set in compose to point at the
# bind-mounted host ~/.azure so DefaultAzureCredential's AzureCliCredential
# link works.

