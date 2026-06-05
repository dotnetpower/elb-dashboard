"""Per-node Linux page-cache sampling via the kubelet Summary API.

Responsibility: Read each node's reclaimable file (page) cache from the kubelet
``/stats/summary`` proxy so the dashboard can show "warm BLAST DB cache" as a
distinct bar segment from working-set memory. The metrics.k8s.io API only
reports working set (which deliberately excludes reclaimable file cache), so a
warmed-but-idle node looks "1% used" there even though tens of GiB of DB files
are resident in page cache.
Edit boundaries: Sampling helper only. Capacity/working-set parsing stays in
``api.services.k8s.metrics``; this module never opens its own session — the
caller passes the already-authenticated session + server so we reuse the same
kubeconfig credential and avoid a second RBAC negotiation.
Key entry points: ``fetch_node_cache_ki``.
Risky contracts: MUST NOT raise for any caller-supplied input or transport
error. The kubelet proxy needs the ``nodes/proxy`` RBAC verb; when it is denied
(403) or times out, the affected node is silently omitted so the node-resources
panel degrades to its working-set-only rendering instead of failing.
Validation: ``uv run pytest -q api/tests/test_k8s_node_cache.py``.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

LOGGER = logging.getLogger(__name__)

# Bound the fan-out so a large blastpool cannot open dozens of simultaneous
# proxy connections through the single session. 8 keeps a 10-node refresh to a
# couple of round-trip batches.
_MAX_WORKERS = 8

# Per-node proxy timeout. Short on purpose: this enrichment is best-effort and
# must not stretch the polled /aks/top-nodes latency. A slow node is dropped
# rather than blocking the whole snapshot.
_PROXY_TIMEOUT_SECONDS = 5.0


def _node_cache_ki(session: Any, server: str, node_name: str, *, timeout: float) -> int | None:
    """Return one node's reclaimable page cache in KiB, or ``None`` on failure.

    Cache is derived as ``usageBytes - workingSetBytes`` from the kubelet
    node-level memory stats: ``usageBytes`` is the root cgroup charge (which
    includes file-backed page cache) and ``workingSetBytes`` excludes the
    reclaimable inactive-file pages. The difference is the page cache the user
    perceives as "warm DB", which the metrics.k8s.io working-set number hides.
    """
    url = f"{server}/api/v1/nodes/{node_name}/proxy/stats/summary"
    try:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
        memory = (response.json().get("node") or {}).get("memory") or {}
        usage = memory.get("usageBytes")
        working_set = memory.get("workingSetBytes")
        if not isinstance(usage, (int, float)) or not isinstance(working_set, (int, float)):
            return None
        cache_bytes = int(usage) - int(working_set)
        if cache_bytes < 0:
            cache_bytes = 0
        return cache_bytes // 1024
    except Exception as exc:
        LOGGER.debug(
            "node cache sample skipped for %s: %s", node_name, type(exc).__name__
        )
        return None


def fetch_node_cache_ki(
    session: Any,
    server: str,
    node_names: list[str],
    *,
    timeout: float = _PROXY_TIMEOUT_SECONDS,
    max_workers: int = _MAX_WORKERS,
) -> dict[str, int]:
    """Sample reclaimable page cache (KiB) for ``node_names`` in parallel.

    Best-effort and side-effect-free apart from the HTTP GETs. Returns a map of
    ``node_name -> cache_ki`` containing only the nodes that answered
    successfully; a node missing from the result simply has no cache overlay in
    the UI. Never raises — a total failure (e.g. the proxy verb is denied
    cluster-wide) yields an empty dict and the panel renders exactly as it did
    before this enrichment existed.
    """
    names = [n for n in node_names if n]
    if not names:
        return {}
    workers = max(1, min(max_workers, len(names)))
    out: dict[str, int] = {}
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = pool.map(
                lambda name: (name, _node_cache_ki(session, server, name, timeout=timeout)),
                names,
            )
            for name, cache_ki in results:
                if cache_ki is not None:
                    out[name] = cache_ki
    except Exception as exc:
        LOGGER.debug("node cache fan-out skipped: %s", type(exc).__name__)
        return out
    return out
