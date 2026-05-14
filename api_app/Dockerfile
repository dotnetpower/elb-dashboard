# syntax=docker/dockerfile:1.7
# Multi-stage build for the api sidecar.
# Stage 1: install deps into a clean prefix so the runtime image stays small.
# Stage 2: distroless-style runtime on python:3.11-slim.

FROM python:3.11-slim AS deps
WORKDIR /app

# Build deps for cryptography (pyjwt[crypto]) on slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY api_app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r /tmp/requirements.txt


FROM python:3.11-slim AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Copy installed Python deps from the build stage.
COPY --from=deps /install /usr/local

# Copy application source.
# api_app/ is the new FastAPI surface; api/services/ is reused as-is via
# the sys.path bootstrap in api_app/services/__init__.py.
COPY api_app /app/api_app
COPY api/services /app/api/services
COPY api/_http_utils.py /app/api/_http_utils.py

# Run as non-root.
RUN groupadd -r app && useradd -r -g app -u 10001 app
USER 10001:10001

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).status==200 else 1)"

# Two workers because the api sidecar terminates WebSockets and
# does streaming upload/download proxying — one worker is too few even
# at low concurrency.
CMD ["uvicorn", "api_app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "2", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
