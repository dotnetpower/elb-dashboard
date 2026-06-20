"""Logging configuration for the api sidecar.

Responsibility: Own the process-wide logging setup (root JSON formatter +
silencing the noisy third-party loggers) so `api.main` stays a thin router-
wiring module.
Edit boundaries: Pure logging configuration — no FastAPI, no routes, no Azure
SDK. Called once at import time from `api.main`.
Key entry points: `configure_logging`.
Risky contracts: Must run before any module emits its first log record, so
`api.main` calls it at import time (before `LOGGER` is created). The noisy-
logger list is intentionally silenced regardless of `LOG_LEVEL`; override the
whole group with `AZURE_LOG_LEVEL=DEBUG`.
Validation: `uv run pytest -q api/tests/test_smoke.py`.
"""

from __future__ import annotations

import logging
import os

# Verbose third-party loggers silenced regardless of LOG_LEVEL — at DEBUG these
# dump full HTTP request/response headers on every Azure SDK call and were the
# single biggest CPU + log-volume drain during local dev. Override the whole
# group with AZURE_LOG_LEVEL=DEBUG when you genuinely need wire-level traces.
_NOISY_LOGGERS = (
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "azure.identity._internal.decorators",
    "azure.identity._credentials.default",
    "urllib3.connectionpool",
    "httpx",
    "watchfiles",
)


def configure_logging() -> None:
    """Configure the root logger (JSON line format) and quiet noisy libraries."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    )
    azure_log_level = os.environ.get("AZURE_LOG_LEVEL", "WARNING").upper()
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(azure_log_level)
