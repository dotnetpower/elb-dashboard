"""Per-(cluster, namespace) Redis lock around the `elastic-blast submit` call.

Responsibility: Provide the small Redis lock primitives used by the BLAST
``submit`` Celery task to serialize concurrent submits that share an AKS
namespace.
Edit boundaries: Keep this module self-contained — only stdlib + ``redis`` so
the broader BLAST task module can import it cheaply. No FastAPI, Celery, or
Azure SDK imports here.
Key entry points: ``BLAST_SUBMIT_LOCK_KEY_PREFIX``,
``BLAST_SUBMIT_LOCK_TTL_SECONDS``, ``submit_lock_key``,
``acquire_submit_lock``, ``release_submit_lock``.
Risky contracts: Lock token is opaque; release uses Lua CAS so a stale
release never deletes a lock held by another caller.
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

LOGGER = logging.getLogger(__name__)

# Per-(cluster, namespace) lock — `elastic-blast submit` writes Kubernetes
# objects (ServiceAccount/Secret/PVC/Job) into one namespace and shares a
# working directory on the terminal sidecar, so concurrent submits targeting
# the same namespace can race. Two submits aimed at different clusters or
# namespaces are independent and run in parallel.
BLAST_SUBMIT_LOCK_KEY_PREFIX = "elb:blast:elastic-blast-submit"
BLAST_SUBMIT_LOCK_TTL_SECONDS = 900


def submit_lock_key(cluster_name: str, namespace: str) -> str:
    cluster = (cluster_name or "_unknown").strip() or "_unknown"
    ns = (namespace or "default").strip() or "default"
    return f"{BLAST_SUBMIT_LOCK_KEY_PREFIX}:{cluster}:{ns}"


def acquire_submit_lock(job_id: str, *, lock_key: str) -> tuple[Any, str] | None:
    import redis

    broker_url = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
    client = redis.Redis.from_url(broker_url)
    token = f"{job_id}:{time.time_ns()}"
    acquired = client.set(
        lock_key,
        token,
        nx=True,
        ex=BLAST_SUBMIT_LOCK_TTL_SECONDS,
    )
    return (client, token) if acquired else None


def release_submit_lock(client: Any, token: str, *, lock_key: str) -> None:
    try:
        client.eval(
            """
            if redis.call('get', KEYS[1]) == ARGV[1] then
                return redis.call('del', KEYS[1])
            end
            return 0
            """,
            1,
            lock_key,
            token,
        )
    except Exception as exc:
        LOGGER.info("blast submit lock release skipped: %s", type(exc).__name__)
