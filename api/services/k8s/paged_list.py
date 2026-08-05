"""Bounded, paged Kubernetes LIST helper for the api sidecar.

Why this exists: the monitoring routes issued cluster-wide LIST calls
(``/api/v1/pods``, ``/apis/apps/v1/deployments``, ``/apis/batch/v1/jobs``) with
no ``limit`` and no ``continue`` paging, then materialised the whole response
with ``response.json()``. Peak memory was therefore **O(cluster object count)**
inside a 2 GiB process, and those routes are polled by every open dashboard tab
every few seconds. A sharded BLAST run leaves thousands of Completed pods/Jobs
behind (the pod list deliberately includes them), so the api sidecar was
SIGKILL'd (exit 137) — 56 times on 2026-07-31, zero on 2026-08-04 when the
cluster was stopped.

Paging makes the per-request peak **O(page size)** instead, regardless of how
big the cluster gets.

Responsibility: Own the paged LIST loop — ``limit``/``continue`` handling, the
    total-item ceiling, the concurrency gate, and the oversized-body warning.
Edit boundaries: Transport-level list mechanics only. Item projection/business
    shaping stays in the calling ``k8s_*`` helper.
Key entry points: ``list_k8s_items``, ``K8sListBusy``.
Risky contracts: The page loop MUST stay bounded (``_MAX_PAGES``) so a broken
    ``continue`` token can never spin forever. An expired continue token (HTTP
    410) MUST degrade to the partial result rather than failing the whole
    monitoring request. The concurrency gate MUST use a bounded acquire — an
    unbounded wait would stall the api worker pool instead of shedding load.
Validation: ``uv run pytest -q api/tests/test_k8s_paged_list.py``.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

LOGGER = logging.getLogger(__name__)

# Per-page object count. 500 keeps a single pods page at a few MB even with fat
# pod specs, which is the whole point: the peak must not scale with the cluster.
_DEFAULT_PAGE_LIMIT = 500
# Total ceiling per call. No dashboard table renders more than this, and the
# capacity/diagnostics callers only aggregate counts. Truncation is logged.
_DEFAULT_MAX_ITEMS = 5000
# Hard stop for the page loop so a server that keeps handing back a continue
# token (or a bug in our own paging) cannot spin forever.
_MAX_PAGES = 50
# Warn when one page body is unexpectedly large, so the next investigation does
# not have to re-derive this from tracemalloc.
_LARGE_PAGE_WARN_BYTES = 8 * 1024 * 1024

# Bound concurrent cluster-wide LISTs. Paging already caps a single call, but N
# parallel monitor requests still multiply the peak; this is defence in depth.
# Default 6, not 4: `diagnostics.snapshot` fans out pods + jobs + deployments
# (3 gated calls) in one ThreadPoolExecutor, so a smaller gate would shed a
# perfectly normal snapshot the moment a monitor refresh ran alongside it.
# 6 x one 500-object page is still only tens of MB.
_DEFAULT_MAX_CONCURRENCY = 6
_ACQUIRE_TIMEOUT_SECONDS = 10.0


class K8sListBusy(RuntimeError):
    """Raised when the K8s list concurrency gate could not be acquired.

    Monitor routes wrap their service calls in a graceful degrade, so this
    surfaces as an empty card rather than a 500 — shedding load is the intended
    behaviour when too many cluster-wide LISTs are already in flight.
    """


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, min(maximum, int(raw)))
    except ValueError:
        LOGGER.warning("invalid %s=%r; using default %d", name, raw, default)
        return default


_SEMAPHORE_LOCK = threading.Lock()
_SEMAPHORE: threading.Semaphore | None = None


def _gate() -> threading.Semaphore:
    global _SEMAPHORE
    with _SEMAPHORE_LOCK:
        if _SEMAPHORE is None:
            _SEMAPHORE = threading.Semaphore(
                _env_int(
                    "K8S_LIST_MAX_CONCURRENCY",
                    _DEFAULT_MAX_CONCURRENCY,
                    minimum=1,
                    maximum=32,
                )
            )
        return _SEMAPHORE


def _reset_gate_for_tests() -> None:
    """Drop the memoised semaphore so a test can re-read the env override."""
    global _SEMAPHORE
    with _SEMAPHORE_LOCK:
        _SEMAPHORE = None


def _page_body_size(response: Any) -> int:
    """Best-effort page size for the oversized-body warning (never raises)."""
    try:
        header = (getattr(response, "headers", {}) or {}).get("content-length")
        if header:
            return int(header)
    except (TypeError, ValueError):
        pass
    try:
        return len(response.content or b"")
    except Exception:  # pragma: no cover - defensive
        return 0


def list_k8s_items(
    session: Any,
    url: str,
    *,
    params: dict[str, str] | None = None,
    label: str,
    timeout: float = 10.0,
    page_limit: int | None = None,
    max_items: int | None = None,
) -> list[dict[str, Any]]:
    """Return up to ``max_items`` objects from a Kubernetes LIST, paging safely.

    ``limit`` + ``continue`` keep each HTTP response — and therefore the JSON we
    materialise — bounded no matter how many objects the cluster holds. The
    result is capped at ``max_items`` and a WARNING names the label + counts
    when that cap truncates, so silent data loss is visible in the logs.

    An expired ``continue`` token (HTTP 410) returns the pages collected so far
    instead of failing: a partially-populated monitoring card beats an empty one.
    """
    bounded_page = page_limit or _env_int(
        "K8S_LIST_PAGE_LIMIT", _DEFAULT_PAGE_LIMIT, minimum=50, maximum=5000
    )
    bounded_total = max_items or _env_int(
        "K8S_LIST_MAX_ITEMS", _DEFAULT_MAX_ITEMS, minimum=100, maximum=100_000
    )

    if not _gate().acquire(timeout=_ACQUIRE_TIMEOUT_SECONDS):
        LOGGER.warning("k8s list gate busy label=%s; shedding request", label)
        raise K8sListBusy(f"k8s list concurrency gate busy for {label}")

    items: list[dict[str, Any]] = []
    truncated = False
    try:
        continue_token = ""
        for page in range(_MAX_PAGES):
            query: dict[str, str] = dict(params or {})
            query["limit"] = str(bounded_page)
            if continue_token:
                query["continue"] = continue_token
            response = session.get(url, params=query, timeout=timeout)
            status = getattr(response, "status_code", 200)
            if status == 410 and items:
                # The continue token expired mid-pagination (objects churned).
                # Degrade to what we already have rather than failing the card.
                LOGGER.warning(
                    "k8s list continue token expired label=%s page=%d collected=%d",
                    label,
                    page,
                    len(items),
                )
                truncated = True
                break
            response.raise_for_status()
            size = _page_body_size(response)
            if size > _LARGE_PAGE_WARN_BYTES:
                LOGGER.warning(
                    "k8s list page unusually large label=%s page=%d bytes=%d limit=%d",
                    label,
                    page,
                    size,
                    bounded_page,
                )
            payload = response.json() or {}
            page_items = payload.get("items") or []
            for item in page_items:
                if isinstance(item, dict):
                    items.append(item)
                if len(items) >= bounded_total:
                    truncated = True
                    break
            if truncated:
                break
            metadata = payload.get("metadata") or {}
            continue_token = str(metadata.get("continue") or "")
            if not continue_token:
                break
        else:
            # Loop exhausted _MAX_PAGES with a live continue token still set.
            truncated = True
            LOGGER.warning(
                "k8s list hit the %d-page ceiling label=%s collected=%d",
                _MAX_PAGES,
                label,
                len(items),
            )
    finally:
        _gate().release()

    if truncated:
        LOGGER.warning(
            "k8s list truncated label=%s returned=%d max_items=%d "
            "(the cluster holds more objects than the dashboard renders)",
            label,
            len(items),
            bounded_total,
        )
    return items
