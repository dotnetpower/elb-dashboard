"""Liveness + readiness endpoints. No auth required - used by Container Apps health probes.

Responsibility: Liveness + readiness endpoints. No auth required - used by Container Apps health
probes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `health`, `readiness`, `celery_diag`, `celery_enqueue_noop`,
`celery_task_result`, `azure_discovery_probe`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends

from api import __version__
from api.auth import require_caller

if TYPE_CHECKING:
    from api.auth import CallerIdentity


# Re-export with a private alias so the dependency is unambiguous in the
# diagnostic route signature (the public name is still ``require_caller``).
_require_caller_lazy = require_caller

LOGGER = logging.getLogger(__name__)

# Cache the Storage probe result. /api/health/ready is unauthenticated and
# may be hit at high frequency by monitoring/CI; without a TTL cache each
# request would fan out a real Storage Table data-plane call (DDoS
# amplifier).
#
# Differential TTL on purpose:
#   * `ok`           — cached for 15 s. This is the amplifier defense.
#   * `down`/`skipped` — cached for 3 s only. A long TTL on a `down` would
#     keep returning the old failure for up to 14 s after an operator
#     opens Storage back up, making recovery look broken when it is not.
#
# Single-flight: when the cache is cold (or just expired) and N requests
# arrive concurrently, only ONE thread actually runs the probe and the
# others reuse its result. Implemented with double-checked locking around
# `_STORAGE_PROBE_CACHE_LOCK`.
#
# The cache is published as a single `(result, ts)` tuple. CPython
# guarantees the rebind of a module global is atomic under the GIL, so the
# fast-path no-lock reader never sees a torn pair (result from one probe,
# ts from a different probe).
_STORAGE_PROBE_CACHE: tuple[dict[str, Any], float] | None = None
_STORAGE_PROBE_CACHE_LOCK = threading.Lock()
_STORAGE_PROBE_CACHE_TTL_OK_SECONDS = float(
    os.environ.get("STORAGE_PROBE_CACHE_TTL_OK_SECONDS", "15.0")
)
_STORAGE_PROBE_CACHE_TTL_BAD_SECONDS = float(
    os.environ.get("STORAGE_PROBE_CACHE_TTL_BAD_SECONDS", "3.0")
)

# TableServiceClient is designed for long-lived reuse (internal connection
# pool, TLS keep-alive). Recreating it per probe pays a fresh TLS handshake
# every cache miss. Cache one instance keyed by endpoint so a change to
# AZURE_TABLE_ENDPOINT (rare — only at process restart in practice) still
# rebuilds the client.
_TABLE_SERVICE_CLIENT: tuple[str, Any] | None = None
_TABLE_SERVICE_CLIENT_LOCK = threading.Lock()


def _ttl_for(result: dict[str, Any] | None) -> float:
    if result is not None and result.get("status") == "ok":
        return _STORAGE_PROBE_CACHE_TTL_OK_SECONDS
    return _STORAGE_PROBE_CACHE_TTL_BAD_SECONDS


def _reset_storage_probe_cache() -> None:
    """Test helper: drop the cached result so the next call re-probes."""
    global _STORAGE_PROBE_CACHE, _TABLE_SERVICE_CLIENT
    LOGGER.info("storage probe cache reset")
    with _STORAGE_PROBE_CACHE_LOCK:
        _STORAGE_PROBE_CACHE = None
    with _TABLE_SERVICE_CLIENT_LOCK:
        # Close any cached client before dropping the reference so the
        # underlying HTTP session is released cleanly, not garbage-collected
        # at some later interpreter pause.
        if _TABLE_SERVICE_CLIENT is not None:
            try:
                _TABLE_SERVICE_CLIENT[1].close()
            except Exception:  # noqa: S110 - close() is best-effort
                pass
        _TABLE_SERVICE_CLIENT = None


def _get_table_service_client(endpoint: str) -> Any:
    """Return a cached TableServiceClient for the endpoint, or build one."""
    global _TABLE_SERVICE_CLIENT
    cached = _TABLE_SERVICE_CLIENT
    if cached is not None and cached[0] == endpoint:
        return cached[1]
    with _TABLE_SERVICE_CLIENT_LOCK:
        cached = _TABLE_SERVICE_CLIENT
        if cached is not None and cached[0] == endpoint:
            return cached[1]
        from azure.data.tables import TableServiceClient

        from api.services import get_credential

        # Build the NEW client BEFORE closing the old one. If construction
        # raises (bad endpoint, transient AAD error), the old client stays
        # usable and the next call retries cleanly. Closing first would
        # leave `_TABLE_SERVICE_CLIENT` pointing at a closed client.
        client = TableServiceClient(
            endpoint=endpoint,
            credential=get_credential(),
            # Probe is fail-fast on purpose: any failure is a real signal
            # readers (the SPA, cli-upgrade.sh, monitoring) want to see
            # immediately. The SDK's default retry policy would otherwise
            # stretch a single readiness call to ~9 s (3 attempts × 3 s
            # timeout each) and mask transient symptoms behind retries.
            retry_total=0,
        )
        old_cached = _TABLE_SERVICE_CLIENT
        _TABLE_SERVICE_CLIENT = (endpoint, client)
        if old_cached is not None:
            try:
                old_cached[1].close()
            except Exception:  # noqa: S110 - close() is best-effort
                pass
        return client


def _probe_storage_table() -> dict[str, Any]:
    """Run the Storage Table probe, or return a cached result within the TTL.

    Cheapest call that exercises (a) AAD token (b) network reachability to
    Storage (c) Storage Table RBAC. Catches the
    ``publicNetworkAccess=Disabled`` without Private Endpoint footgun that
    returns 403 AuthorizationFailure at the data plane while ARM/credential
    paths look healthy. ``timeout=3`` caps a single call so a wedged Storage
    cannot tarpit ``/health/ready``.

    Concurrency: a fast no-lock read serves cache hits; on a miss the
    `_STORAGE_PROBE_CACHE_LOCK` is acquired and the cache re-checked
    (double-checked locking) so only one of N concurrent first-callers
    actually runs the probe.
    """
    global _STORAGE_PROBE_CACHE
    now = time.monotonic()
    cached = _STORAGE_PROBE_CACHE  # single atomic load under GIL
    if cached is not None and now - cached[1] < _ttl_for(cached[0]):
        return cached[0]

    with _STORAGE_PROBE_CACHE_LOCK:
        # Re-check after acquiring the lock: another thread may have just
        # refreshed while we were waiting.
        cached = _STORAGE_PROBE_CACHE
        now = time.monotonic()
        if cached is not None and now - cached[1] < _ttl_for(cached[0]):
            return cached[0]

        endpoint = os.environ.get("AZURE_TABLE_ENDPOINT", "")
        if not endpoint:
            result: dict[str, Any] = {
                "status": "skipped",
                "reason": "AZURE_TABLE_ENDPOINT not set",
            }
        else:
            try:
                svc = _get_table_service_client(endpoint)
                next(svc.list_tables(results_per_page=1, timeout=3).by_page(), None)
                result = {"status": "ok"}
            except Exception as exc:
                # `error_class` is an additive field for operator diagnosis;
                # the existing `error` string keeps the same shape so any
                # downstream parsing remains backwards-compatible. Common
                # classes: HttpResponseError (RBAC / 403), ServiceRequestError
                # (DNS / TCP / TLS), ClientAuthenticationError (token),
                # TimeoutError (`timeout=3` tripped).
                result = {
                    "status": "down",
                    "error": str(exc)[:200],
                    "error_class": type(exc).__name__,
                }

        # Single atomic publish: readers never see (new_result, old_ts) or
        # (old_result, new_ts).
        prev_status = cached[0].get("status") if cached is not None else None
        _STORAGE_PROBE_CACHE = (result, time.monotonic())

    # Log state transitions OUTSIDE the lock — logging.Logger acquires its
    # own handler lock and may block on I/O (stderr, file, journald). Doing
    # that while holding `_STORAGE_PROBE_CACHE_LOCK` would create a
    # lock-ordering footgun: any future code path that logs from inside
    # another lock that wraps this function could deadlock.
    #
    # Wrap in try/except so a broken handler (disk full, journald down,
    # custom handler raising) never bubbles up to fail the readiness
    # response itself — the cache is already published at this point.
    new_status = result.get("status")
    if prev_status is not None and prev_status != new_status:
        try:
            LOGGER.info(
                "storage probe state transition: %s -> %s (%s)",
                prev_status,
                new_status,
                result.get("error", result.get("reason", "")),
            )
        except Exception:  # noqa: S110 - intentional: cannot re-log a logging failure
            pass
    return result


router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str | bool]:
    """Liveness probe — return 200 if the process can answer.

    Container Apps uses HTTP probes against this path. Keep the response
    cheap — no Azure SDK calls, no Storage reads, no token validation.

    `app_insights_configured` is a boolean indicating whether the deployment
    provided `APPLICATIONINSIGHTS_CONNECTION_STRING`. The connection string
    itself is exposed under the auth-gated `/api/settings/app-insights`.
    """
    return {
        "status": "ok",
        "version": __version__,
        "revision": os.environ.get("CONTAINER_APP_REVISION", "local"),
        "app_insights_configured": bool(
            (os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING") or "").strip()
        ),
    }


@router.get("/health/ready")
def readiness() -> Any:
    """Readiness/deep health probe — checks downstream dependencies.

    Returns 200 with component statuses when the service can accept work.
    Returns 503 when critical dependencies are down.  The response always
    includes ``retryable`` and ``retry_after_seconds`` hints so the caller
    (SPA or load balancer) knows whether to wait or fail fast.
    """
    components: dict[str, dict[str, Any]] = {}
    overall_ok = True

    # 1. Redis (Celery broker)
    try:
        from api.celery_app import celery_app

        conn = celery_app.connection()
        # Fail-fast on purpose: `max_retries=0` makes a single connect attempt
        # and raises immediately on failure instead of sleeping the kombu retry
        # interval (`interval_start`, ~2 s) before one retry. A readiness probe
        # is re-fired by the orchestrator on its own cadence, so an internal
        # retry only adds latency (and can push the probe past the caller's
        # timeout, making a briefly-degraded broker look fully down). This
        # mirrors the Storage probe's documented `retry_total=0` philosophy
        # below and removes a ~2 s tarpit per readiness call when Redis is down.
        conn.ensure_connection(max_retries=0, timeout=2)
        conn.close()
        components["redis"] = {"status": "ok"}
    except Exception as exc:
        components["redis"] = {"status": "down", "error": str(exc)[:200]}
        overall_ok = False

    # 2. Azure credential (can we get a token?)
    try:
        from api.services import get_credential

        cred = get_credential()
        # Just check the credential object exists — don't make an ARM call
        components["azure_credential"] = {"status": "ok", "type": type(cred).__name__}
    except Exception as exc:
        components["azure_credential"] = {"status": "down", "error": str(exc)[:200]}
        overall_ok = False

    # 3. Terminal sidecar
    try:
        from api.services.terminal_exec import healthz

        th = healthz()
        components["terminal_sidecar"] = {
            "status": "ok" if th.get("status") == "ok" else "degraded"
        }
    except Exception:
        components["terminal_sidecar"] = {"status": "down"}
        # Non-critical — terminal being down doesn't block BLAST submit

    # 4. Azure Storage Table data plane.
    # Delegated to a cached helper so high-frequency unauthenticated callers
    # do not amplify into Storage data-plane traffic (see
    # `_probe_storage_table` for cache TTL + per-call timeout details).
    storage_status = _probe_storage_table()
    components["azure_storage"] = storage_status
    if storage_status["status"] == "down":
        overall_ok = False

    status_code = 200 if overall_ok else 503
    from fastapi.responses import JSONResponse

    # Audit #3: the per-component map leaks internal topology (which
    # dependencies exist, credential class name, and truncated error strings
    # that can echo endpoints) to an anonymous caller. Behind the default-OFF
    # `STRICT_READINESS_DETAIL` guard (charter §12a Rule 4) the body collapses
    # to the overall status only — enough for a load balancer / CI gate that
    # cares about the 200-vs-503 signal, nothing for a recon probe. Default
    # OFF preserves the existing full-detail body (and the cli-upgrade Tier-1
    # gate that reads `components.azure_storage`).
    if os.environ.get("STRICT_READINESS_DETAIL", "").lower() == "true":
        return JSONResponse(
            content={
                "status": "ready" if overall_ok else "not_ready",
                "version": __version__,
                "retryable": not overall_ok,
                "retry_after_seconds": 30 if not overall_ok else None,
            },
            status_code=status_code,
        )

    return JSONResponse(
        content={
            "status": "ready" if overall_ok else "not_ready",
            "version": __version__,
            "components": components,
            "retryable": not overall_ok,
            "retry_after_seconds": 30 if not overall_ok else None,
        },
        status_code=status_code,
    )


@router.get("/health/celery")
def celery_diag(
    caller: CallerIdentity = Depends(_require_caller_lazy),
) -> dict[str, Any]:
    """Diagnostic-only: return per-queue length + worker inspect snapshot.

    Auth-gated (MSAL bearer required) because the payload includes
    broker URL, queue depth, and worker stats — internal infrastructure
    detail that should never be reachable anonymously. Used to diagnose
    'task enqueued but worker silent' problems; `redis_keys_db0` should
    contain only the worker's known queue names.
    """
    del caller
    out: dict[str, Any] = {"queues": {}, "workers": None, "errors": []}

    # 0. Producer-side celery config (so we can spot a config mismatch
    #    between api and worker sidecars).
    try:
        from api.celery_app import celery_app

        out["producer_conf"] = {
            "default_queue": celery_app.conf.task_default_queue,
            "task_routes": dict(celery_app.conf.task_routes or {}),
            "broker_url": celery_app.conf.broker_url,
        }
    except Exception as exc:
        out["errors"].append(f"producer_conf: {type(exc).__name__}: {exc}")

    # 1. Redis queue lengths via raw redis client (no kombu wrapper)
    try:
        from api.celery_app import CELERY_BROKER_URL
        from api.services.redis_clients import get_broker_redis_client

        r = get_broker_redis_client(socket_timeout=2)
        for q in ("default", "azure", "blast", "storage", "celery"):
            try:
                out["queues"][q] = r.llen(q)
            except Exception as exc:
                out["queues"][q] = f"err:{type(exc).__name__}"
        out["broker_url"] = CELERY_BROKER_URL
        out["redis_keys_db0"] = sorted(
            k.decode() for k in r.keys("*") if not k.startswith(b"_kombu")
        )
    except Exception as exc:
        out["errors"].append(f"redis: {type(exc).__name__}: {exc}")

    # 2. Celery worker inspect (active / reserved / scheduled / registered)
    try:
        from api.celery_app import celery_app

        insp = celery_app.control.inspect(timeout=2)
        out["workers"] = {
            "active": insp.active(),
            "reserved": insp.reserved(),
            "scheduled": insp.scheduled(),
            "registered": insp.registered(),
            "stats": insp.stats(),
            "ping": insp.ping(),
        }
    except Exception as exc:
        out["errors"].append(f"inspect: {type(exc).__name__}: {exc}")

    return out


@router.post("/health/celery/enqueue-noop")
def celery_enqueue_noop(
    message: str = "diag-ping",
    caller: CallerIdentity = Depends(_require_caller_lazy),
) -> dict[str, Any]:
    """Diagnostic-only: enqueue a no-op task. Returns task_id for status polling.

    Auth-gated — anonymous enqueue would let an unauthenticated caller
    spam the broker and burn worker capacity.
    """
    del caller
    from api.tasks.azure import diag_noop

    res = diag_noop.delay(message=message)
    return {"task_id": res.id, "queue": "azure", "message": message}


@router.get("/health/celery/result/{task_id}")
def celery_task_result(
    task_id: str,
    caller: CallerIdentity = Depends(_require_caller_lazy),
) -> dict[str, Any]:
    """Diagnostic-only: get a celery task result.

    Auth-gated because task results can carry arbitrary payload (BLAST
    metadata, ARM responses, error tracebacks containing subscription
    ids). For per-user ownership enforcement use ``/api/operations/{id}``
    or ``/api/tasks/{id}`` instead.
    """
    del caller
    from celery.result import AsyncResult

    from api.celery_app import celery_app

    r = AsyncResult(task_id, app=celery_app)
    out: dict[str, Any] = {
        "task_id": task_id,
        "status": r.status,
        "ready": r.ready(),
    }
    if r.ready():
        if r.successful():
            out["result"] = r.result
        else:
            out["error"] = str(r.result)[:500]
    return out


@router.get("/health/azure-discovery")
def azure_discovery_probe(
    caller: CallerIdentity = Depends(_require_caller_lazy),
) -> dict[str, Any]:
    """Diagnostic-only: prove the api can list subscriptions / RGs end-to-end.

    Why this exists: the SPA's discovery wizard fails silently when the
    api sidecar's credential cannot reach ARM (no MI attached, MI lacks
    Reader at subscription scope, IMDS not responding, ...). The user-
    visible symptom is an empty list with no error. This endpoint forces
    each step of the chain and reports which one fails so the operator
    has a single curl-able answer.

    Auth-gated (MSAL bearer required — unlike the other /health/* probes)
    because the response references subscription ids and would otherwise
    leak tenant topology to anyone who can reach the ingress. All sub
    ids and display names that appear in the response go through
    `sanitise()` so even an authenticated caller never sees raw GUIDs.

    Makes two real ARM calls (subscriptions.list with a hard cap of 5,
    then resource_groups.list on the first sub) so do not poll this
    from a dashboard — use it as a one-shot post-deploy sanity check.
    """
    from api.services import get_credential
    from api.services.azure_clients import resource_client, subscription_client
    from api.services.sanitise import sanitise

    _ = caller  # accepted for auth-gate side effect; not echoed back

    out: dict[str, Any] = {
        "credential": {"status": "unknown"},
        "subscriptions_list": {"status": "unknown"},
        "resource_groups_list": {"status": "unknown"},
        "hint": None,
    }

    # 1. Credential acquisition (does NOT call IMDS yet — just constructs).
    try:
        cred = get_credential()
        out["credential"] = {"status": "ok", "type": type(cred).__name__}
    except Exception as exc:
        out["credential"] = {
            "status": "error",
            "error_type": type(exc).__name__,
            "error_message": sanitise(str(exc))[:300],
        }
        out["hint"] = (
            "DefaultAzureCredential could not be constructed. Check that the "
            "azure-identity package is installed in the api image."
        )
        return out

    # 2. Subscriptions list (forces a real ARM token via IMDS / MI).
    sub_id: str | None = None
    try:
        client = subscription_client(cred)
        # Hard cap so a misconfigured tenant with thousands of subs does
        # not turn this probe into an outage.
        samples: list[dict[str, str]] = []
        first_sub_id: str | None = None
        for i, s in enumerate(client.subscriptions.list()):
            if i >= 5:
                break
            if first_sub_id is None:
                first_sub_id = s.subscription_id
            # Always sanitise: response is auth-gated but we still mask GUIDs
            # so the diagnostic blob can be safely pasted into a bug report
            # or a public log channel. Display names are dropped entirely
            # (they often encode tenant/org names or billing context).
            samples.append({"id": sanitise(s.subscription_id or "")})
        out["subscriptions_list"] = {
            "status": "ok",
            "count_capped_at_5": len(samples),
            "samples": samples,
        }
        sub_id = first_sub_id  # raw (used for the next ARM call only, never echoed)
    except Exception as exc:
        out["subscriptions_list"] = {
            "status": "error",
            "error_type": type(exc).__name__,
            "error_message": sanitise(str(exc))[:300],
        }
        out["hint"] = (
            "subscriptions.list() failed. Most likely the user-assigned MI is "
            "not attached to this Container App, or the AZURE_CLIENT_ID env "
            "does not match the MI's clientId. Verify with `az containerapp "
            "show --name ca-elb-dashboard --query identity` and "
            "`az containerapp show ... --query 'properties.template.containers[0].env'`."
        )
        return out

    if not sub_id:
        out["resource_groups_list"] = {"status": "skipped", "reason": "no subscriptions visible"}
        out["hint"] = (
            "Credential works but the MI sees zero subscriptions. Confirm "
            "subscription-scope Reader was granted (infra/modules/"
            "subscriptionRoles.bicep, or `az role assignment create --role "
            "Reader --scope /subscriptions/<id> --assignee-object-id <miOid>`)."
        )
        return out

    # 3. Resource group list — proves Reader/Contributor at sub scope.
    try:
        rc = resource_client(cred, sub_id)
        rg_count = 0
        for _rg in rc.resource_groups.list():
            rg_count += 1
            if rg_count >= 2:
                break
        out["resource_groups_list"] = {
            "status": "ok",
            "subscription_id": sanitise(sub_id),
            "count": rg_count,
            "count_capped_at_2": rg_count >= 2,
        }
        if rg_count == 0:
            out["hint"] = (
                "subscriptions.list() works but resource_groups.list() returned 0. "
                "Either the subscription truly is empty, or the MI has only a "
                "resource-scope role (Storage/ACR/KV) which is enough to surface "
                "the subscription but not to enumerate RGs. Grant Reader at "
                "subscription scope."
            )
    except Exception as exc:
        out["resource_groups_list"] = {
            "status": "error",
            "subscription_id": sanitise(sub_id),
            "error_type": type(exc).__name__,
            "error_message": sanitise(str(exc))[:300],
        }
        out["hint"] = (
            "resource_groups.list() failed. Check that the MI has Reader (or "
            "higher) at subscription scope — see docs/auth.md §Step 2."
        )

    return out
