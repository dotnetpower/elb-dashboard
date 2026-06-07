"""External OpenAPI BLAST job cache + table-sync helpers.

Responsibility: External OpenAPI BLAST job cache, negative-cache, detail-enrich,
Azure Table sync, and the elb-openapi client-config resolver (the pure job ->
dashboard projection helpers live in the sibling `external_job_projection.py`).
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_external_list_jobs_cached`, `_sync_external_jobs_to_table`,
`_external_job_detail_or_row`, `_openapi_client_kwargs_from_cluster`, `_reset_external_jobs_cache`
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
        try:
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
                if ext_status and (ext_status != cur_status or ext_phase != cur_phase):
                    try:
                        repo.update(job_id, status=ext_status, phase=ext_phase)
                        updated += 1
                    except KeyError:
                        existing = None
                if existing is not None:
                    continue
            payload = converted.get("payload") or {"external": ext}
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

    public_base_url = get_public_tls_base_url()
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
        return kwargs
    except Exception as exc:
        LOGGER.info("openapi cluster context unavailable: %s", type(exc).__name__)
        # When the public endpoint is configured we still want to attempt
        # the call rather than degrade to an empty config — the public LB
        # is reachable independently of the cluster's K8s API surface.
        if public_base_url:
            return {"base_url": public_base_url}
        return {}
