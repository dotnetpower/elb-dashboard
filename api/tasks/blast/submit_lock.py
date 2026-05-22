"""Per-(cluster, namespace) Redis lock around the `elastic-blast submit` call.

Responsibility: Provide the small Redis lock primitives used by the BLAST
``submit`` Celery task to serialize concurrent submits that share an AKS
namespace.
Edit boundaries: Keep this module self-contained — only stdlib + the shared
``api.services.redis_clients`` pool helper. No FastAPI, Celery, or Azure SDK
imports here. Do not call ``redis.Redis.from_url`` directly; that would
allocate a fresh connection pool per submit and exhaust FDs under load.
Key entry points: ``BLAST_SUBMIT_LOCK_KEY_PREFIX``,
``BLAST_SUBMIT_LOCK_TTL_SECONDS``, ``submit_lock_key``,
``acquire_submit_lock``, ``release_submit_lock``.
Risky contracts: Lock token is opaque; release uses Lua CAS so a stale
release never deletes a lock held by another caller. The Redis client
returned in the lock tuple is a process-shared singleton — callers MUST
NOT call ``.close()`` on it.
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py
api/tests/test_redis_clients.py``.
"""

from __future__ import annotations

import logging
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
    from api.services.redis_clients import get_broker_redis_client

    client = get_broker_redis_client()
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
