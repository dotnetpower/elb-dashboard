"""External OpenAPI BLAST job cache + table-sync helpers.

Responsibility: External OpenAPI BLAST job cache, negative-cache, detail-enrich,
Azure Table sync, and the elb-openapi client-config resolver (the pure job ->
dashboard projection helpers live in the sibling `external_job_projection.py`).
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_external_list_jobs_cached`, `_sync_external_jobs_to_table`,
`collect_and_sync_external_jobs`, `_external_job_detail_or_row`,
`_openapi_client_kwargs_from_cluster`, `_discover_subscription_clusters`,
`_reset_external_jobs_cache`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries. The projection helpers are re-exported under their original private names so
existing consumers (`job_state`, tests) keep their import surface.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py api/tests/test_external_blast_api.py`.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

# Pure job -> dashboard projection helpers were extracted into
# `external_job_projection.py` (SRP: this module owns cache + sync, that one
# owns projection). They are re-imported here under their original private
# names so `job_state` and the external-jobs tests keep importing them from
# `api.services.blast.external_jobs` unchanged, and so the internal
# `_sync_external_jobs_to_table` can keep calling `_external_to_blast_job`.
from api.services.blast.external_job_projection import (
    _external_error_message as _external_error_message,
)
from api.services.blast.external_job_projection import (
    _external_result_files as _external_result_files,
)
from api.services.blast.external_job_projection import (
    _external_status_to_dashboard as _external_status_to_dashboard,
)
from api.services.blast.external_job_projection import (
    _external_to_blast_job as _external_to_blast_job,
)
from api.services.blast.external_job_projection import (
    _short_external_db_name as _short_external_db_name,
)
from api.services.blast.external_query_labels import apply_remembered_query_label

LOGGER = logging.getLogger(__name__)


def _exception_reason(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            code = detail.get("code")
            if code not in (None, ""):
                return str(code)
        if detail not in (None, ""):
            return str(detail)[:120]
        return f"http_{exc.status_code}"
    return type(exc).__name__


# Detail codes that signal the IP/base URL we're using is wrong (Service
# was recreated, pod rescheduled, LB IP rotated). Treat these as a signal
# to flush the IP cache so the next request goes through k8s_get_service_ip
# again instead of replaying the bad IP for the full 70 s cache TTL.
_OPENAPI_TRANSPORT_FAILURE_CODES = frozenset(
    {
        "openapi_unreachable",
        "openapi_upstream_unreachable",
    }
)


def _exception_is_transport_failure(exc: Exception) -> bool:
    if not isinstance(exc, HTTPException):
        return False
    if exc.status_code != 503:
        return False
    detail = exc.detail
    if isinstance(detail, dict):
        code = detail.get("code")
        if code in _OPENAPI_TRANSPORT_FAILURE_CODES:
            return True
    return False


_EXTERNAL_NOT_ENABLED_REASONS = frozenset(
    {
        "openapi_not_configured",
        "openapi_not_enabled",
    }
)
_EXTERNAL_DETAIL_ENRICH_LIMIT = 20
_EXTERNAL_JOBS_CACHE_TTL_SECONDS = 70.0
_EXTERNAL_JOBS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_EXTERNAL_JOBS_CACHE_LOCK = threading.Lock()
_EXTERNAL_JOBS_INFLIGHT: dict[str, threading.Event] = {}
# Negative cache: when ``list_jobs`` raises ``HTTPException`` (401 missing
# token, 5xx upstream, ``openapi_not_configured`` 503, …) we cache the
# exception for a short TTL so SPA polling (every ~14 s) doesn't keep paying
# the 700-1500 ms upstream round-trip just to learn the same failure again.
_EXTERNAL_JOBS_NEG_CACHE_TTL_SECONDS = float(
    os.environ.get("EXTERNAL_JOBS_NEG_CACHE_TTL", "30.0")
)
_EXTERNAL_JOBS_NEG_CACHE: dict[str, tuple[float, HTTPException]] = {}
_EXTERNAL_JOB_DETAIL_CACHE_TTL_SECONDS = 70.0
_EXTERNAL_JOB_DETAIL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_EXTERNAL_SYNC_CACHE_TTL_SECONDS = 70.0
_EXTERNAL_SYNC_CACHE: dict[str, tuple[float, tuple[int, int, set[str]]]] = {}
_OPENAPI_CLIENT_KWARGS_CACHE_TTL_SECONDS = 70.0
_OPENAPI_CLIENT_KWARGS_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
# Subscription-wide ElasticBLAST cluster discovery cache. The Recent searches
# history view lists jobs subscription-scoped (no cluster pinned), so to find
# jobs submitted directly through ``POST /v1/jobs`` we must enumerate the
# subscription's clusters and resolve each one's OpenAPI endpoint. That is one
# ARM ``managedClusters.list`` round trip — cache it so the ~10 s jobs-list
# poll cannot fan out into a managedClusters.list per request (App Insights
# previously caught managedClusters call storms from uncached fan-out).
_SUBSCRIPTION_CLUSTERS_CACHE_TTL_SECONDS = 60.0
_SUBSCRIPTION_CLUSTERS_CACHE: dict[str, tuple[float, list[tuple[str, str]]]] = {}


def _discover_subscription_clusters(subscription_id: str) -> list[tuple[str, str]]:
    """Return cached ``(resource_group, cluster_name)`` pairs for ELB clusters.

    Used by the subscription-scoped jobs listing to resolve every cluster's
    OpenAPI endpoint so directly-submitted ``/v1/jobs`` jobs are discovered.
    One ARM ``managedClusters.list`` round trip, cached for
    ``_SUBSCRIPTION_CLUSTERS_CACHE_TTL_SECONDS``. Never raises — discovery
    failures (no credential, ARM throttle, RBAC gap) return an empty list so
    the caller degrades to the env / runtime-cache fallback target.

    Stopped clusters are excluded on purpose. The caller resolves each
    returned cluster's OpenAPI endpoint via ``_openapi_client_kwargs_from_cluster``,
    which calls ``k8s_get_service_ip`` against the cluster's K8s API server
    (a 10 s-timeout HTTP GET). A Stopped cluster's API server is down, so that
    call always burns the full timeout and then returns ``{}`` (which the
    resolver does NOT cache), forcing the ~14 s-polled Recent searches endpoint
    to re-pay one 10 s timeout per Stopped cluster on every poll. A Stopped
    cluster also cannot serve ``/v1/jobs`` (no running pods), so it can never
    yield a live job anyway — anything it ran while Running was already synced
    into our Table and still shows as a local row. Gating on power state keeps
    the latency cost proportional to the number of *running* clusters.
    """
    if not subscription_id:
        return []
    import time as _time

    now = _time.monotonic()
    with _EXTERNAL_JOBS_CACHE_LOCK:
        cached = _SUBSCRIPTION_CLUSTERS_CACHE.get(subscription_id)
        if cached and cached[0] > now:
            return list(cached[1])
    try:
        from api.services import get_credential
        from api.services.monitoring import list_aks_clusters_in_subscription

        credential = get_credential()
        clusters = list_aks_clusters_in_subscription(credential, subscription_id)
        pairs = [
            (str(c.get("resource_group") or ""), str(c.get("name") or ""))
            for c in clusters
            if c.get("name") and _cluster_power_state_allows_openapi(c.get("power_state"))
        ]
    except Exception as exc:
        LOGGER.info(
            "subscription cluster discovery for external jobs failed: %s",
            type(exc).__name__,
        )
        pairs = []
    with _EXTERNAL_JOBS_CACHE_LOCK:
        _SUBSCRIPTION_CLUSTERS_CACHE[subscription_id] = (
            _time.monotonic() + _SUBSCRIPTION_CLUSTERS_CACHE_TTL_SECONDS,
            list(pairs),
        )
        if len(_SUBSCRIPTION_CLUSTERS_CACHE) > 32:
            oldest = min(
                _SUBSCRIPTION_CLUSTERS_CACHE.items(), key=lambda kv: kv[1][0]
            )[0]
            _SUBSCRIPTION_CLUSTERS_CACHE.pop(oldest, None)
    return pairs


def _cluster_power_state_allows_openapi(power_state: object) -> bool:
    """True when a cluster may have a reachable OpenAPI plane.

    A missing/unknown power state is treated as allowed (do not hide a
    genuinely-running cluster just because the field was absent); only an
    explicitly non-``Running`` state (``Stopped`` / ``Stopping``) is excluded.
    """
    if power_state in (None, ""):
        return True
    return str(power_state).strip().casefold() == "running"


def _external_list_jobs_cached(external_kwargs: dict[str, Any]) -> list[dict[str, Any]]:
    """Cached wrapper around ``external_blast.list_jobs(**kwargs)``."""

    import json
    import time as _time

    from api.services import external_blast

    key = json.dumps(external_kwargs, sort_keys=True, default=str)
    while True:
        now = _time.monotonic()
        with _EXTERNAL_JOBS_CACHE_LOCK:
            entry = _EXTERNAL_JOBS_CACHE.get(key)
            if entry and entry[0] > now:
                return entry[1]
            neg = _EXTERNAL_JOBS_NEG_CACHE.get(key)
            if neg and neg[0] > now:
                raise neg[1]
            inflight = _EXTERNAL_JOBS_INFLIGHT.get(key)
            if inflight is None:
                inflight = threading.Event()
                _EXTERNAL_JOBS_INFLIGHT[key] = inflight
                leader = True
            else:
                leader = False
        if not leader:
            inflight.wait(timeout=35.0)
            continue
        try:
            rows = external_blast.list_jobs(**external_kwargs).get("jobs", []) or []
            if not isinstance(rows, list):
                rows = []
            expires_at = _time.monotonic() + _EXTERNAL_JOBS_CACHE_TTL_SECONDS
            with _EXTERNAL_JOBS_CACHE_LOCK:
                _EXTERNAL_JOBS_CACHE[key] = (expires_at, rows)
                _EXTERNAL_JOBS_NEG_CACHE.pop(key, None)
                if len(_EXTERNAL_JOBS_CACHE) > 32:
                    oldest = min(_EXTERNAL_JOBS_CACHE.items(), key=lambda kv: kv[1][0])[0]
                    _EXTERNAL_JOBS_CACHE.pop(oldest, None)
            return rows
        except HTTPException as exc:
            # `openapi_unreachable` (503) usually means the Service IP /
            # base URL we cached is stale — Service was recreated, pod was
            # rescheduled, LB IP rotated. Invalidate the IP cache so the
            # next request triggers a fresh `k8s_get_service_ip` lookup
            # instead of replaying the bad IP for up to 70 s. Auth /
            # configuration errors (401, 503 `openapi_not_configured`)
            # are NOT IP-related — leave their negative cache alone.
            if _exception_is_transport_failure(exc):
                with _EXTERNAL_JOBS_CACHE_LOCK:
                    _OPENAPI_CLIENT_KWARGS_CACHE.clear()
                # Shorter negative cache so the next /api/blast/jobs poll
                # gets to retry instead of replaying the cached 503 for
                # the full 30 s window.
                neg_ttl = min(_EXTERNAL_JOBS_NEG_CACHE_TTL_SECONDS, 5.0)
            else:
                neg_ttl = _EXTERNAL_JOBS_NEG_CACHE_TTL_SECONDS
            expires_at = _time.monotonic() + neg_ttl
            with _EXTERNAL_JOBS_CACHE_LOCK:
                _EXTERNAL_JOBS_NEG_CACHE[key] = (expires_at, exc)
                if len(_EXTERNAL_JOBS_NEG_CACHE) > 32:
                    oldest = min(
                        _EXTERNAL_JOBS_NEG_CACHE.items(), key=lambda kv: kv[1][0]
                    )[0]
                    _EXTERNAL_JOBS_NEG_CACHE.pop(oldest, None)
            raise
        finally:
            with _EXTERNAL_JOBS_CACHE_LOCK:
                _EXTERNAL_JOBS_INFLIGHT.pop(key, None)
                inflight.set()


def _reset_external_jobs_cache() -> None:
    """Test hook: clear the in-memory external jobs caches."""

    with _EXTERNAL_JOBS_CACHE_LOCK:
        _EXTERNAL_JOBS_CACHE.clear()
        _EXTERNAL_JOBS_INFLIGHT.clear()
        _EXTERNAL_JOBS_NEG_CACHE.clear()
        _EXTERNAL_JOB_DETAIL_CACHE.clear()
        _EXTERNAL_SYNC_CACHE.clear()
        _OPENAPI_CLIENT_KWARGS_CACHE.clear()
        _SUBSCRIPTION_CLUSTERS_CACHE.clear()


def _recover_external_failure_error(
    job_id: str, infrastructure: dict[str, Any]
) -> str | None:
    """Best-effort recovery of a failed external job's real error message.

    The ``/v1/jobs`` LIST snapshot the sync runs on never carries an ``error``
    field — only the per-job detail endpoint (``GET /api/v1/elastic-blast/jobs/
    {id}``, reached by :func:`external_blast.get_job`) does. So a synced row
    that transitions to ``failed`` would otherwise surface the generic
    "External BLAST job failed, but the OpenAPI service reported no error
    detail." banner even though the sibling knows the precise cause (e.g. a
    memory-fit rejection). This fetches that detail ONCE at the failed
    transition and returns the sanitised message for persistence in the
    row's ``error_code`` column.

    Resolution uses the row's own ``subscription_id`` / ``resource_group`` /
    ``cluster_name`` so the call targets the cluster the job actually ran on
    (``external_blast.get_job`` resolves the per-cluster endpoint + token from
    the runtime cache). Never raises — a sibling outage / unresolved endpoint
    degrades to ``None`` (the generic banner is preserved), so error recovery
    can never turn a successful sync into a failure.
    """
    from api.services import external_blast

    try:
        detail = external_blast.get_job(
            job_id,
            subscription_id=str(infrastructure.get("subscription_id") or ""),
            resource_group=str(infrastructure.get("resource_group") or ""),
            cluster_name=str(infrastructure.get("cluster_name") or ""),
        )
    except Exception as exc:
        LOGGER.info(
            "external failed-job error recovery unavailable job_id=%s: %s",
            job_id,
            _exception_reason(exc),
        )
        return None
    if not isinstance(detail, dict):
        return None
    code, message = _external_error_message(detail.get("error"))
    return message or code or None


def _stored_submission_source(state: Any) -> str:
    """Return the dashboard-recorded submission_source on a stored job row.

    Prefers the nested ``payload.external.submission_source`` (where the drain
    stamps ``"servicebus"``) and falls back to the payload top level. Returns
    ``""`` when the row carries no marker. Used to recover the true origin of a
    queue-drained job, which the sibling cannot report (it only knows the wire
    value ``"external_api"``).
    """
    payload = getattr(state, "payload", None)
    if not isinstance(payload, dict):
        return ""
    external = payload.get("external")
    if isinstance(external, dict):
        nested = str(external.get("submission_source") or "").strip()
        if nested:
            return nested
    return str(payload.get("submission_source") or "").strip()


def _stored_queue_origin(state: Any) -> str:
    """Return the dashboard-recorded ``queue_origin`` on a stored job row.

    Mirrors :func:`_stored_submission_source`: the drain stamps
    ``queue_origin`` (``"control_plane"`` | ``"external"``) onto
    ``payload.external``; the sibling ``/v1/jobs`` snapshot cannot report it, so
    this recovers the durable value to relabel the freshly-projected row. Returns
    ``""`` when the row carries no marker (older rows, or a non-queue origin).
    """
    payload = getattr(state, "payload", None)
    if not isinstance(payload, dict):
        return ""
    external = payload.get("external")
    if isinstance(external, dict):
        nested = str(external.get("queue_origin") or "").strip()
        if nested:
            return nested
    return str(payload.get("queue_origin") or "").strip()


def _sync_external_jobs_to_table(
    external_jobs: list[dict[str, Any]],
    *,
    caller_oid: str,
    tenant_id: str = "",
) -> tuple[int, int, set[str]]:
    """Best-effort upsert of external OpenAPI jobs into Azure Table Storage."""
    if not external_jobs:
        return (0, 0, set())
    import json
    import time as _time

    sync_key = json.dumps(
        {
            "caller_oid": caller_oid,
            "tenant_id": tenant_id,
            "jobs": [
                {
                    "job_id": str(ext.get("job_id") or ""),
                    "status": str(ext.get("status") or ""),
                    "phase": str(ext.get("phase") or ""),
                    "updated_at": str(ext.get("updated_at") or ext.get("completed_at") or ""),
                }
                for ext in external_jobs
            ],
        },
        sort_keys=True,
        default=str,
    )
    now = _time.monotonic()
    with _EXTERNAL_JOBS_CACHE_LOCK:
        cached = _EXTERNAL_SYNC_CACHE.get(sync_key)
        if cached and cached[0] > now:
            c_created, c_updated, c_tombstoned = cached[1]
            return (c_created, c_updated, set(c_tombstoned))
    try:
        from api.services.state_repo import JobState, JobStateRepository

        repo = JobStateRepository()
    except Exception:
        return (0, 0, set())

    job_ids = [str(ext.get("job_id") or "") for ext in external_jobs]
    try:
        existing_map = repo.get_many([jid for jid in job_ids if jid])
    except Exception as exc:
        LOGGER.debug("sync_external_jobs batch lookup failed: %s", type(exc).__name__)
        existing_map = {}

    created = 0
    updated = 0
    tombstoned: set[str] = set()
    for ext in external_jobs:
        job_id = str(ext.get("job_id") or "")
        if not job_id:
            continue
        # Date-tiered layout: the drain stamps the exact results prefix
        # (``YYYY/MM/DD/<openapi_job_id>/``) it told the sibling to write under.
        # Persist it verbatim so resolve_results_prefix(job_id) returns the date
        # path for analytics/marker/runtime-failure blob reads. The sibling
        # status/list snapshots never echo the results URL, so this stamped
        # value is the only authoritative source the dashboard has. Empty/absent
        # leaves the row on the flat ``<job_id>/`` fallback (legacy behaviour).
        _row_results_prefix = str(ext.get("results_prefix") or "").strip() or None
        # Recover the dashboard's own submission_source (e.g. "servicebus") that
        # the sibling cannot report — over the wire a queue-drained job is
        # submitted as "external_api" (the sibling's enum has no "servicebus"
        # value), so the /v1/jobs row always reads "external_api". The stored
        # row preserves the true marker because the update path below never
        # rewrites the payload, so a queue-originated job stays labelled
        # "servicebus" in Recent searches / Jobs instead of being downgraded.
        # Mutating ``ext`` here also relabels the same dict the list route
        # projects (it is shared by reference with ``result.rows``).
        _existing_for_source = existing_map.get(job_id)
        if _existing_for_source is not None:
            _stored_src = _stored_submission_source(_existing_for_source)
            if _stored_src and _stored_src != str(ext.get("submission_source") or ""):
                ext["submission_source"] = _stored_src
            # Recover the queue origin (control_plane | external) the same way:
            # the sibling row cannot report it, the stored row preserves it.
            _stored_qo = _stored_queue_origin(_existing_for_source)
            if _stored_qo and _stored_qo != str(ext.get("queue_origin") or ""):
                ext["queue_origin"] = _stored_qo
        try:
            # Inline-FASTA API submits carry no query identity from the sibling.
            # Inject the defline label remembered at submit time BEFORE projecting
            # so it is persisted into the Table row (durable), independent of
            # whether the caller already applied it for display. Idempotent: a
            # row that already has a query identity is returned unchanged.
            ext = apply_remembered_query_label(ext)
            converted = _external_to_blast_job(ext)
            ext_status = str(converted.get("status") or "unknown")
            ext_phase = str(converted.get("phase") or ext_status)
            existing = existing_map.get(job_id)
            if existing is not None:
                cur_status = str(existing.status or "")
                cur_phase = str(existing.phase or "")
                if cur_status == "deleted":
                    tombstoned.add(job_id)
                    continue
                # Backfill scope columns that were stored empty. A row first
                # synced by a sub-scoped poll whose cluster endpoint was
                # transiently unresolvable falls back to the env target and
                # lands with no cluster_name. The AKS cluster card filters jobs
                # by cluster_name, so such a /v1/jobs row stayed visible on
                # Recent searches but hidden on the card forever. Once a later
                # poll resolves the real cluster identity, copy it onto the row
                # so both views converge on the same scope rule. Only ever fill
                # an empty column — never overwrite a value already on the row.
                infra = converted.get("infrastructure") or {}
                scope_backfill: dict[str, str] = {}
                for col in (
                    "subscription_id",
                    "resource_group",
                    "cluster_name",
                    "storage_account",
                ):
                    new_val = str(infra.get(col) or "")
                    cur_val = str(getattr(existing, col, "") or "")
                    if new_val and not cur_val:
                        scope_backfill[col] = new_val
                # Heal the identity columns (program / db / job_title /
                # query_label) when they were stored as the canonical default.
                # A row first synced from a transient /v1/jobs row that lacked
                # program/db was persisted with program/title = "blast", db = ""
                # (canonical_job_metadata reads the payload top level, not its
                # nested ``external`` key, so a ``{"external": ...}`` payload
                # yields the defaults). The scope-only backfill above never
                # touched these, and the list view reads the columns directly
                # (include_payload=False), so the API job stayed stuck showing
                # "blast" with no database even after the sibling list carried
                # the real values. Fill ONLY when the stored column is the
                # degenerate default AND the fresh projection has a real value;
                # a row that already carries good metadata is never overwritten.
                meta_backfill: dict[str, str] = {}
                fresh_program = str(converted.get("program") or "")
                fresh_db = str(converted.get("db") or "")
                fresh_title = str(converted.get("job_title") or "")
                fresh_query = str(converted.get("query_label") or "")
                cur_program = str(getattr(existing, "program", "") or "")
                cur_db = str(getattr(existing, "db", "") or "")
                cur_title = str(getattr(existing, "job_title", "") or "")
                cur_query = str(getattr(existing, "query_label", "") or "")
                if fresh_program and fresh_program != "blast" and cur_program in {"", "blast"}:
                    meta_backfill["program"] = fresh_program
                if fresh_db and not cur_db:
                    meta_backfill["db"] = fresh_db
                if (
                    fresh_title
                    and fresh_title != "blast"
                    and (cur_title in {"", "blast"} or cur_title == job_id)
                ):
                    meta_backfill["job_title"] = fresh_title
                if fresh_query and fresh_query not in {"", "query.fa"} and not cur_query:
                    meta_backfill["query_label"] = fresh_query
                # Backfill the date-tiered results prefix onto a row that was
                # first created without one (e.g. a poll-discovered row that
                # predates the stamped value). Only ever fill when empty so a
                # row already carrying its prefix is never rewritten.
                prefix_backfill: dict[str, str] = {}
                if _row_results_prefix and not (getattr(existing, "results_prefix", None) or ""):
                    prefix_backfill["results_prefix"] = _row_results_prefix
                status_changed = bool(
                    ext_status and (ext_status != cur_status or ext_phase != cur_phase)
                )
                if status_changed or scope_backfill or meta_backfill or prefix_backfill:
                    update_kwargs: dict[str, Any] = dict(scope_backfill)
                    update_kwargs.update(meta_backfill)
                    update_kwargs.update(prefix_backfill)
                    if status_changed:
                        update_kwargs["status"] = ext_status
                        update_kwargs["phase"] = ext_phase
                        # Clear any stale ``error_code`` (e.g. a transient
                        # ``worker_lost`` left by a false-positive reconcile
                        # pass) when flipping to a terminal-success state. The
                        # sibling is authoritative here -- if it now says the
                        # job completed, the dashboard must not keep showing
                        # the recovered error code on the row.
                        if ext_status.lower() in {"completed", "succeeded"} and (
                            getattr(existing, "error_code", "") or ""
                        ):
                            update_kwargs["error_code"] = ""
                        # Surface the real failure cause on the FAILED transition.
                        # The /v1/jobs LIST snapshot carries no ``error`` field,
                        # so without this the row would render the generic
                        # "no error detail" banner. Fetch the sibling detail ONCE
                        # (guarded on an empty existing error_code so a stable
                        # failed row never re-fetches) and persist its message
                        # into the indexed error_code column.
                        elif ext_status.lower() == "failed" and not (
                            getattr(existing, "error_code", "") or ""
                        ):
                            recovered = _recover_external_failure_error(
                                job_id, converted.get("infrastructure") or {}
                            )
                            if recovered:
                                update_kwargs["error_code"] = recovered
                    try:
                        repo.update(job_id, **update_kwargs)
                        updated += 1
                    except KeyError:
                        existing = None
                if existing is not None:
                    continue
            payload = converted.get("payload") or {"external": ext}
            # A row first observed already in a failed state (the dashboard
            # never saw it running) still needs the real cause recovered from
            # the sibling detail, since the LIST snapshot has no ``error``.
            create_error_code: str | None = None
            if ext_status.lower() == "failed":
                create_error_code = _recover_external_failure_error(
                    job_id, converted.get("infrastructure") or {}
                )
            state = JobState(
                job_id=job_id,
                type="blast",
                status=ext_status,
                phase=ext_phase,
                owner_oid=caller_oid,
                owner_upn="api",
                tenant_id=tenant_id,
                created_at=str(converted.get("created_at") or ""),
                updated_at=str(converted.get("updated_at") or ""),
                payload=payload,
                error_code=create_error_code,
                job_title=str(converted.get("job_title") or ""),
                program=str(converted.get("program") or ""),
                db=str(converted.get("db") or ""),
                query_label=str(converted.get("query_label") or ""),
                subscription_id=str(
                    (converted.get("infrastructure") or {}).get("subscription_id") or ""
                ),
                resource_group=str(
                    (converted.get("infrastructure") or {}).get("resource_group") or ""
                ),
                cluster_name=str((converted.get("infrastructure") or {}).get("cluster_name") or ""),
                storage_account=str(
                    (converted.get("infrastructure") or {}).get("storage_account") or ""
                ),
                results_prefix=_row_results_prefix,
            )
            repo.create(state)
            created += 1
        except Exception as exc:
            LOGGER.debug(
                "sync_external_job_to_table failed job_id=%s: %s",
                job_id,
                type(exc).__name__,
            )
    if created or updated:
        LOGGER.info("external job sync: created=%d updated=%d", created, updated)
    with _EXTERNAL_JOBS_CACHE_LOCK:
        _EXTERNAL_SYNC_CACHE[sync_key] = (
            _time.monotonic() + _EXTERNAL_SYNC_CACHE_TTL_SECONDS,
            (created, updated, set(tombstoned)),
        )
        if len(_EXTERNAL_SYNC_CACHE) > 128:
            oldest = min(_EXTERNAL_SYNC_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _EXTERNAL_SYNC_CACHE.pop(oldest, None)
    return (created, updated, tombstoned)


# Statuses for which a list-view row is worth a per-row detail enrichment call
# (the row is in flight, so its richer fields may have changed). Terminal rows
# are skipped — their detail is stable and already synced. Shared by the
# Recent-searches list route and the Message Flow discovery path so both decide
# "needs detail" identically.
_EXTERNAL_LIST_DETAIL_STATUSES = frozenset(
    {
        "pending",
        "queued",
        "running",
        "submitted",
        "submitting",
        "inprogress",
        "in_progress",
        "splitting",
        "reducing",
    }
)


def _external_list_row_needs_detail(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or row.get("phase") or "").strip().casefold()
    return status in _EXTERNAL_LIST_DETAIL_STATUSES


def _external_row_with_scope_defaults(
    row: dict[str, Any],
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Fill empty Azure/AKS scope fields on an external row from its target.

    Only ever sets a field that is absent — a value the sibling already carries
    is never overwritten. Returns the row unchanged when there is no scope to
    apply (the env / runtime-cache fallback target).
    """
    if not (subscription_id or resource_group or cluster_name):
        return row
    scoped = dict(row)
    if subscription_id:
        scoped.setdefault("subscription_id", subscription_id)
    if resource_group:
        scoped.setdefault("resource_group", resource_group)
    if cluster_name:
        scoped.setdefault("cluster_name", cluster_name)
    return scoped


def _resolve_external_list_targets(
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> list[dict[str, Any]]:
    """Resolve the elb-openapi endpoint(s) to query for ``/v1/jobs`` listing.

    Shared by the Recent searches list route and the Message Flow snapshot. The
    history view lists jobs subscription-scoped (no ``cluster_name``) so it can
    show jobs across every cluster; ``_openapi_client_kwargs_from_cluster`` needs
    the full ``(subscription, resource_group, cluster)`` triple to resolve a
    base URL + token, so a subscription-only call must first enumerate the
    subscription's clusters.

    Resolution:
      * ``cluster_name`` scope → a single target from
        ``_openapi_client_kwargs_from_cluster`` (or the legacy ``{}`` fallback).
      * subscription-only scope → discover the subscription's ElasticBLAST
        clusters and resolve one target per reachable cluster, deduped by base
        URL. Falls back to the legacy ``{}`` target only when discovery finds no
        usable cluster endpoint.
      * no subscription → the legacy ``{}`` fallback only.

    Each target carries the scope it was resolved from so the caller applies the
    correct ``_external_row_with_scope_defaults`` / detail-enrich context.
    """
    targets: list[dict[str, Any]] = []
    seen_base_urls: set[str] = set()

    def _add(kwargs: dict[str, str], sub: str, rg: str, cluster: str) -> None:
        base = str(kwargs.get("base_url") or "")
        # The legacy ``{}`` (env / runtime-cache) fallback has no base_url and
        # is added at most once.
        dedup_key = base or "__env_fallback__"
        if dedup_key in seen_base_urls:
            return
        seen_base_urls.add(dedup_key)
        targets.append(
            {
                "kwargs": dict(kwargs),
                "subscription_id": sub,
                "resource_group": rg,
                "cluster_name": cluster,
            }
        )

    if cluster_name:
        kwargs = _openapi_client_kwargs_from_cluster(
            subscription_id, resource_group, cluster_name
        )
        _add(kwargs, subscription_id, resource_group, cluster_name)
        return targets

    if subscription_id:
        for rg, cluster in _discover_subscription_clusters(subscription_id):
            if not cluster:
                continue
            kwargs = _openapi_client_kwargs_from_cluster(subscription_id, rg, cluster)
            if kwargs:
                _add(kwargs, subscription_id, rg, cluster)
        if targets:
            return targets

    # No cluster endpoint resolved: keep the legacy env / runtime-cache fallback
    # so deployments that set ELB_OPENAPI_BASE_URL (or have a populated runtime
    # cache) keep working exactly as before.
    _add({}, subscription_id, resource_group, cluster_name)
    return targets


@dataclass
class ExternalJobsSyncResult:
    """Outcome of :func:`collect_and_sync_external_jobs`.

    ``rows`` are the discovered external rows (scope-defaulted, query-labelled,
    optionally detail-enriched) the caller may merge into a response. The Table
    upsert is a side effect that has already happened; ``rows`` is provided so
    the Recent-searches route can render them without a second Table read, while
    the Message Flow path ignores it and re-reads the Table.
    """

    rows: list[dict[str, Any]] = field(default_factory=list)
    tombstoned_ids: set[str] = field(default_factory=set)
    any_target_ok: bool = False
    target_failures: list[Exception] = field(default_factory=list)
    created: int = 0
    updated: int = 0


def collect_and_sync_external_jobs(
    *,
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
    tenant_id: str = "",
    seen_job_ids: set[str] | None = None,
    detail_enrich_budget: int = 0,
    limit: int | None = None,
) -> ExternalJobsSyncResult:
    """Discover external ``/v1/jobs`` for a scope and upsert them into the Table.

    Best-effort orchestration shared by the Recent-searches list route and the
    Message Flow snapshot. Resolves the OpenAPI endpoint(s) for the given scope,
    fetches each target's ``/v1/jobs`` list (70 s cached), applies scope defaults
    and remembered query labels (plus up to ``detail_enrich_budget`` per-row
    detail-enrichment calls when a scope is present), and upserts the discovered
    rows into Azure Table Storage with a blank owner
    (``caller_oid=""`` → cluster-shared visibility, so any caller with ARM scope
    on the cluster — including the Message Flow card — sees them).

    Resilience: a transport/auth failure against one cluster is recorded in
    ``ExternalJobsSyncResult.target_failures`` and never aborts the other
    targets. ``any_target_ok`` is True if at least one target answered. The
    function never raises — the worst case is an empty result. The caller
    decides whether all-failed should surface as a degraded badge.

    ``seen_job_ids`` (if given) is mutated in place to track de-duplication
    against rows the caller already has (e.g. local Table rows merged first);
    pass ``None`` for an isolated discovery.
    """
    from api.services import external_blast

    result = ExternalJobsSyncResult()
    seen = seen_job_ids if seen_job_ids is not None else set()

    try:
        targets = _resolve_external_list_targets(
            subscription_id, resource_group, cluster_name
        )
    except Exception as exc:
        LOGGER.info(
            "external jobs target resolution failed: %s", type(exc).__name__
        )
        result.target_failures.append(exc)
        return result

    candidate_rows: list[dict[str, Any]] = []
    # Rows already shown as a local Table row this request (pre-seeded into
    # ``seen`` by the caller). They are NOT re-displayed, but they ARE still
    # synced so a row first persisted with degenerate program/db/title (from a
    # transient empty /v1/jobs row) heals toward the authoritative upstream
    # values. ``_sync_external_jobs_to_table`` only writes when the stored
    # column is the degenerate default, so a row with good metadata is a no-op.
    preexisting_ids = set(seen)
    heal_rows: dict[str, dict[str, Any]] = {}
    budget = max(0, int(detail_enrich_budget))
    for target in targets:
        t_kwargs = dict(target["kwargs"])
        # #51: bound the external /v1/jobs LIST fetch to the most-recent
        # ``limit`` jobs once the sibling supports it (older sibling ignores the
        # param and returns the full list — degrades cleanly). ``limit`` is a
        # list-only parameter, so it goes ONLY into the fetch kwargs — NOT
        # ``t_kwargs``, which is also reused for the per-row ``get_job`` detail
        # enrichment whose signature rejects ``limit`` (passing it there raised
        # TypeError and silently skipped enrichment). ``limit`` joins the list
        # cache key so a wider page does not serve a narrower cached fetch.
        list_kwargs = dict(t_kwargs)
        if isinstance(limit, int) and limit > 0:
            list_kwargs["limit"] = limit
        t_sub = target["subscription_id"]
        t_rg = target["resource_group"]
        t_cluster = target["cluster_name"]
        try:
            external_rows = _external_list_jobs_cached(list_kwargs)
        except Exception as exc:
            # One unreachable cluster (e.g. Stopped) must not hide jobs on the
            # other reachable clusters in a subscription-wide discovery.
            result.target_failures.append(exc)
            continue
        result.any_target_ok = True
        if not isinstance(external_rows, list):
            continue
        for ext_row in external_rows:
            if not isinstance(ext_row, dict):
                continue
            job_id = str(ext_row.get("job_id") or "")
            if not job_id:
                continue
            if job_id in preexisting_ids:
                # Already a local row this request — capture once for the heal
                # pass (no display dup, no detail-enrichment budget spent).
                if job_id not in heal_rows:
                    heal_rows[job_id] = apply_remembered_query_label(
                        _external_row_with_scope_defaults(
                            ext_row,
                            subscription_id=t_sub,
                            resource_group=t_rg,
                            cluster_name=t_cluster,
                        )
                    )
                continue
            if job_id in seen:
                continue
            seen.add(job_id)
            ext_row = _external_row_with_scope_defaults(
                ext_row,
                subscription_id=t_sub,
                resource_group=t_rg,
                cluster_name=t_cluster,
            )
            should_enrich_detail = bool(t_sub or t_rg or t_cluster)
            if (
                should_enrich_detail
                and budget > 0
                and _external_list_row_needs_detail(ext_row)
            ):
                ext_row = _external_job_detail_or_row(external_blast, ext_row, t_kwargs)
                budget -= 1
            # Inline-FASTA API submits carry no query identity from the sibling;
            # inject the defline label remembered at submit time so the row shows
            # the real query instead of "query.fa".
            ext_row = apply_remembered_query_label(ext_row)
            candidate_rows.append(ext_row)

    result.rows = candidate_rows
    # Display the freshly-discovered rows; sync both those AND the heal-only
    # rows (disjoint by job_id) so existing degenerate local rows converge.
    sync_input = candidate_rows + list(heal_rows.values())
    if sync_input:
        created, updated, tombstoned_ids = _sync_external_jobs_to_table(
            sync_input,
            caller_oid="",
            tenant_id=tenant_id,
        )
        result.created = created
        result.updated = updated
        result.tombstoned_ids = tombstoned_ids
    return result


def _merge_external_detail(row: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    merged = dict(row)
    for key, value in detail.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _external_job_detail_or_row(
    external_blast: Any,
    row: dict[str, Any],
    external_kwargs: dict[str, str],
) -> dict[str, Any]:
    import json
    import time as _time

    job_id = str(row.get("job_id") or "").strip()
    if not job_id:
        return row
    detail_key = json.dumps(
        {"job_id": job_id, "kwargs": external_kwargs},
        sort_keys=True,
        default=str,
    )
    now = _time.monotonic()
    with _EXTERNAL_JOBS_CACHE_LOCK:
        entry = _EXTERNAL_JOB_DETAIL_CACHE.get(detail_key)
        if entry and entry[0] > now:
            return _merge_external_detail(row, entry[1])
    try:
        detail = external_blast.get_job(job_id, **external_kwargs)
    except Exception as exc:
        LOGGER.info(
            "external blast job detail unavailable job_id=%s: %s",
            job_id,
            _exception_reason(exc),
        )
        return row
    if not isinstance(detail, dict):
        return row
    with _EXTERNAL_JOBS_CACHE_LOCK:
        _EXTERNAL_JOB_DETAIL_CACHE[detail_key] = (
            _time.monotonic() + _EXTERNAL_JOB_DETAIL_CACHE_TTL_SECONDS,
            detail,
        )
        if len(_EXTERNAL_JOB_DETAIL_CACHE) > 256:
            oldest = min(_EXTERNAL_JOB_DETAIL_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _EXTERNAL_JOB_DETAIL_CACHE.pop(oldest, None)
    return _merge_external_detail(row, detail)


def _openapi_client_kwargs_from_cluster(
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, str]:
    if not (subscription_id and resource_group and cluster_name):
        return {}
    import json
    import time as _time

    cache_key = json.dumps(
        {
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "cluster_name": cluster_name,
        },
        sort_keys=True,
    )
    now = _time.monotonic()
    with _EXTERNAL_JOBS_CACHE_LOCK:
        cached = _OPENAPI_CLIENT_KWARGS_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return dict(cached[1])

    # Public TLS endpoint, when configured, skips the K8s Service IP
    # lookup entirely. The token still needs the cluster context to be
    # read from the Deployment env, so fall through to the legacy path
    # when reading it fails — that path is unchanged from before the TLS
    # rollout, so the only behavioural difference here is the `base_url`
    # scheme/host. Env unset = 100% legacy behaviour.
    from api.services.openapi.runtime import get_public_tls_base_url

    public_base_url = get_public_tls_base_url(
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    try:
        from api.services import get_credential
        from api.services.k8s.monitoring import (
            k8s_get_deployment_env_value,
            k8s_get_service_ip,
        )

        credential = get_credential()
        base_url: str
        if public_base_url:
            # Skip the IP lookup; the public endpoint is the authoritative
            # base. We still need the cluster to be reachable to read the
            # token below, but a transient k8s_get_service_ip flake should
            # not block the public endpoint from being used.
            base_url = public_base_url
        else:
            ip = k8s_get_service_ip(
                credential,
                subscription_id,
                resource_group,
                cluster_name,
                "elb-openapi",
            )
            if not ip:
                return {}
            base_url = f"http://{ip}"
            try:
                from api.services.openapi.runtime import save_openapi_base_url

                save_openapi_base_url(
                    base_url,
                    metadata={
                        "subscription_id": subscription_id,
                        "resource_group": resource_group,
                        "cluster_name": cluster_name,
                        "service_name": "elb-openapi",
                    },
                )
            except Exception as exc:
                LOGGER.debug("openapi runtime cache write skipped: %s", type(exc).__name__)
        api_token = os.environ.get("ELB_OPENAPI_API_TOKEN", "").strip()
        if not api_token:
            try:
                api_token = (
                    k8s_get_deployment_env_value(
                        credential,
                        subscription_id,
                        resource_group,
                        cluster_name,
                        "elb-openapi",
                        "ELB_OPENAPI_API_TOKEN",
                        container_name="openapi",
                    )
                    or ""
                ).strip()
            except Exception as exc:
                # When using the public endpoint we don't have to fail the
                # call just because the K8s API was momentarily unhappy —
                # the caller already passes any cached token via env. Log
                # and continue.
                LOGGER.debug(
                    "openapi token lookup via K8s skipped: %s",
                    type(exc).__name__,
                )
                api_token = ""
        kwargs = {"base_url": base_url}
        if api_token:
            kwargs["api_token"] = api_token
        with _EXTERNAL_JOBS_CACHE_LOCK:
            _OPENAPI_CLIENT_KWARGS_CACHE[cache_key] = (
                _time.monotonic() + _OPENAPI_CLIENT_KWARGS_CACHE_TTL_SECONDS,
                dict(kwargs),
            )
            if len(_OPENAPI_CLIENT_KWARGS_CACHE) > 64:
                oldest = min(
                    _OPENAPI_CLIENT_KWARGS_CACHE.items(), key=lambda kv: kv[1][0]
                )[0]
                _OPENAPI_CLIENT_KWARGS_CACHE.pop(oldest, None)
        return kwargs
    except Exception as exc:
        LOGGER.info("openapi cluster context unavailable: %s", type(exc).__name__)
        # When the public endpoint is configured we still want to attempt
        # the call rather than degrade to an empty config — the public LB
        # is reachable independently of the cluster's K8s API surface.
        if public_base_url:
            return {"base_url": public_base_url}
        return {}
