"""Client for the sibling ElasticBLAST OpenAPI execution plane.

Responsibility: Client for the sibling ElasticBLAST OpenAPI execution plane
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `DownloadedFile`, `StreamedFile`, `_base_url`, `submit_job`, `get_job`,
`list_jobs`, `ready`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from api.services.sanitise import sanitise

_BASE_URL_ENV = "ELB_OPENAPI_BASE_URL"
_INTERNAL_AUTH_ENV = "ELB_OPENAPI_INTERNAL_TOKEN"
_API_AUTH_ENV = "ELB_OPENAPI_API_TOKEN"
# Submit is the slowest sibling call: it creates a ConfigMap + applies a
# K8s Job + waits for the AKS API server to ack. Under cold cache /
# AKS API server pressure 30 s is not enough — the request times out
# locally but the sibling has already created the job on the cluster,
# leaving an orphan job the dashboard does not know about. 90 s covers
# the observed worst-case while keeping the user-perceived wait bounded.
_DEFAULT_TIMEOUT_SECONDS = 90.0
_LIST_TIMEOUT_SECONDS = float(os.environ.get("OPENAPI_LIST_TIMEOUT_SECONDS", "5.0"))
# Readiness probe timeout. Sibling enforces a 2.5s hard budget internally; we
# wait 2.5s longer so the sibling's structured 503 always arrives before our
# client-side timeout converts it to an opaque ``openapi_unreachable``.
_READY_TIMEOUT_SECONDS = float(os.environ.get("OPENAPI_READY_TIMEOUT_SECONDS", "5.0"))
# Sliding-window cache for the readiness probe. The sibling probe is cheap
# (~200 ms warm) but the SPA + the internal submit gate can call it back-to-back
# within a single user click. A tiny TTL absorbs the burst without hiding a
# real state change (AKS just started / stopped). Set TTL=0 to disable.
_READY_CACHE_TTL_SECONDS = float(os.environ.get("OPENAPI_READY_CACHE_TTL_SECONDS", "5.0"))
_READY_CACHE_LOCK = threading.Lock()
_READY_CACHE: dict[
    tuple[str, str], tuple[float, dict[str, Any] | HTTPException]
] = {}
# Per-key in-flight serialisation. When the cache is cold and N workers /
# concurrent requests miss simultaneously, only the first one fires the
# upstream HTTP probe; the rest wait on the same Event and then read the
# freshly populated cache. Prevents the dashboard from melting its own
# 30-req/min sibling budget under cold-cache load.
_READY_INFLIGHT_LOCK = threading.Lock()
_READY_INFLIGHT: dict[tuple[str, str], threading.Event] = {}
# How long a waiter blocks on an in-flight upstream call before giving up
# and firing its own request. Tuned slightly above the read timeout so the
# leader always wins in the happy path; a fall-through to a parallel call
# is acceptable degradation if the leader stalls.
_READY_INFLIGHT_WAIT_SECONDS = float(
    os.environ.get("OPENAPI_READY_INFLIGHT_WAIT_SECONDS", "6.0")
)
# Maximum number of "wait on a sibling leader, then re-check the cache"
# rounds a non-leader caller will perform before giving up and firing its
# own upstream probe. Capped (critique #20.12) so that a pathological
# leader-swap loop (leader A times out, B becomes leader, B times out, \u2026)
# cannot pin a single caller for an unbounded duration: total worst-case
# wait = ``_READY_INFLIGHT_MAX_WAIT_ROUNDS * _READY_INFLIGHT_WAIT_SECONDS``.
# Keep this small \u2014 the cache is the optimisation target, not waiting
# itself.
_READY_INFLIGHT_MAX_WAIT_ROUNDS = int(
    os.environ.get("OPENAPI_READY_INFLIGHT_MAX_WAIT_ROUNDS", "2")
)
# Token-resync coalescing (see ``_resync_token_after_401``). A redeploy
# invalidates the cached token for every in-flight call at once; the lock
# serialises the recovery and the short TTL lets the queued callers reuse the
# just-recovered token instead of each issuing its own K8s read.
_RESYNC_LOCK = threading.Lock()
_RESYNC_RESULT: tuple[float, str] = (0.0, "")
_RESYNC_COALESCE_TTL_SECONDS = float(
    os.environ.get("OPENAPI_TOKEN_RESYNC_COALESCE_TTL_SECONDS", "3.0")
)
_STREAM_TIMEOUT = httpx.Timeout(30.0, read=300.0)
# Submit retries on transient transport failures (connection refused /
# reset / read timeout). Only applied when the payload carries an
# idempotency_key so the sibling can dedupe a re-send; without it we
# would risk creating duplicate jobs on the cluster. Override at the env
# layer (``OPENAPI_SUBMIT_MAX_RETRIES=0``) to make a flaky test
# deterministic without waiting for the backoff sleeps.
_SUBMIT_MAX_TRANSPORT_RETRIES = int(os.environ.get("OPENAPI_SUBMIT_MAX_RETRIES", "2"))
_SUBMIT_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.5)
# Truncation policy for sibling-derived error messages we surface in 503
# ``openapi_unreachable`` responses. Kept at 300 chars verbatim from the
# pre-refactor sites; named so the policy is visible in one place.
_TRANSPORT_DETAIL_MAX_CHARS = 300
# Sanitise-detail truncation policy (kept verbatim from the pre-refactor
# inline literals so observable behaviour does not change).
_SANITISE_DETAIL_STRING_MAX_CHARS = 1000
_SANITISE_DETAIL_KEY_MAX_CHARS = 100
_SANITISE_DETAIL_LIST_LIMIT = 20
# Safe-filename cap. FAT32/NTFS allow up to 255 but the SPA + Storage
# manifest reserve the rest for path/extension prefixes.
_SAFE_FILENAME_MAX_LENGTH = 128
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadedFile:
    content: bytes
    media_type: str
    filename: str


@dataclass(frozen=True)
class StreamedFile:
    chunks: Iterator[bytes]
    media_type: str
    filename: str


def _base_url(
    value: str | None = None,
    *,
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
) -> str:
    value = (value or os.environ.get(_BASE_URL_ENV, "")).strip().rstrip("/")
    if not value and subscription_id and resource_group and cluster_name:
        # Per-cluster outbound scoping (#26): when the caller knows which AKS
        # cluster the request targets, prefer that cluster's public HTTPS
        # endpoint so a multi-cluster revision does not send the call to the
        # globally most-recently-written runtime endpoint. A miss (no domain
        # configured for this cluster yet) falls through to the legacy global
        # runtime key below — backward compatible.
        from api.services.openapi.runtime import get_public_tls_base_url

        value = (
            get_public_tls_base_url(
                subscription_id=subscription_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
            )
            .strip()
            .rstrip("/")
        )
    if not value:
        from api.services.openapi.runtime import get_openapi_base_url

        value = get_openapi_base_url()
    if not value:
        raise HTTPException(
            503,
            detail={
                "code": "openapi_not_configured",
                "message": f"{_BASE_URL_ENV} is not set and no OpenAPI runtime endpoint is cached",
            },
        )
    return value


def _headers(
    *,
    api_token: str | None = None,
    internal_token: str | None = None,
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    api_token = (api_token or os.environ.get(_API_AUTH_ENV, "")).strip()
    if not api_token:
        # Per-cluster outbound scoping (#26): pass the cluster context so the
        # per-cluster token key is read first, with the legacy global key as a
        # fallback. Empty context preserves the global-key behaviour. The token
        # value is never logged (charter §12).
        from api.services.openapi.runtime import get_openapi_api_token

        api_token = get_openapi_api_token(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
    if api_token:
        headers["X-ELB-API-Token"] = api_token
    token = (internal_token or os.environ.get(_INTERNAL_AUTH_ENV, "")).strip()
    if token:
        headers["X-ELB-Internal-Token"] = token
    return headers


def _resync_token_after_401() -> str:
    """Re-read the live elb-openapi token from the cluster after a 401.

    Returns the recovered token (which the resync helper also writes back into
    the runtime cache / process env) or ``""`` when no live token could be read.
    Never raises — a resync failure simply surfaces the original 401.

    Coalesced (self-critique: bound the concurrency fan-out). A control-plane
    redeploy invalidates the cached token for EVERY in-flight sibling call at
    once, so without coalescing a burst of N concurrent 401s would each fire an
    independent ``read_cluster_openapi_token`` K8s API call (a thundering herd
    against the API server, all reading the same value). A process-wide lock
    serialises the resync, and a short result cache lets the callers that were
    waiting on the lock reuse the just-recovered token instead of re-reading it.
    The cache TTL is deliberately tiny so a genuinely new token rotation is
    still picked up on the next 401 after the window.
    """
    import time as _time

    now = _time.monotonic()
    with _RESYNC_LOCK:
        cached_ts, cached_token = _RESYNC_RESULT
        if cached_token and (now - cached_ts) < _RESYNC_COALESCE_TTL_SECONDS:
            # A concurrent caller already resynced moments ago — reuse it
            # instead of issuing another K8s read for the same value.
            return cached_token
        try:
            from api.services.openapi.token import resync_openapi_api_token_from_cluster

            token = resync_openapi_api_token_from_cluster() or ""
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("openapi 401 token resync raised %s", type(exc).__name__)
            token = ""
        # Only cache a successful recovery; an empty result must not suppress the
        # next caller's retry (it may succeed once the pod/cluster settles).
        if token:
            _set_resync_result(_time.monotonic(), token)
        return token


def _set_resync_result(ts: float, token: str) -> None:
    """Store the last successful resync (caller holds ``_RESYNC_LOCK``)."""
    global _RESYNC_RESULT
    _RESYNC_RESULT = (ts, token)


def reset_token_resync_cache() -> None:
    """Drop the coalesced resync result (test hook)."""
    global _RESYNC_RESULT
    with _RESYNC_LOCK:
        _RESYNC_RESULT = (0.0, "")



def _request_with_token_resync(
    *,
    base_url: str,
    timeout: float,
    api_token: str | None,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    send: Callable[[httpx.Client], httpx.Response],
    label: str,
) -> httpx.Response:
    """Send a sibling request, self-healing a stale-token 401 exactly once.

    A 401 from the sibling almost always means the dashboard's *ephemeral*
    runtime token cache was wiped by a control-plane redeploy while the
    elb-openapi pod kept its minted token (the same failure mode the ``/v1/
    ready`` probe already self-heals). Without this, the BLAST job-detail
    recovery (:func:`get_job`), the jobs-list sync (:func:`list_jobs`), and a
    Service-Bus-driven :func:`submit_job` would all surface a spurious auth
    failure — and for the recovery path that hides the real failure reason
    behind the generic "no error detail" banner.

    Builds an httpx client with the resolved (or overridden) ``X-ELB-API-Token``,
    runs ``send(client)``, and on a 401 re-reads the live token from the
    deployment, syncs it into the runtime cache, and retries ONCE with the
    recovered token. Never resyncs more than once, so a pod that genuinely
    rejects a freshly read token cannot loop. Transport errors raised by
    ``send`` propagate to the caller unchanged (the caller owns retry/backoff).
    """

    def _client(token_override: str | None) -> httpx.Client:
        return httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers=_headers(
                api_token=token_override,
                subscription_id=subscription_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
            ),
        )

    with _client(api_token) as client:
        resp = send(client)
    if resp.status_code != 401:
        return resp
    healed = _resync_token_after_401()
    if not healed:
        # No live token recovered — surface the original 401 to the caller.
        return resp
    LOGGER.warning(
        "openapi %s returned 401 — token resynced from cluster; retrying once",
        label,
        extra={"event": "openapi_token_resync_retry"},
    )
    with _client(healed) as client:
        return send(client)



def _safe_filename(value: str) -> str:
    name = value.strip().strip('"') or "blast_result.xml"
    name = name.split("/", 1)[-1].split("\\", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:_SAFE_FILENAME_MAX_LENGTH]
    return name or "blast_result.xml"


def _path_segment(value: str) -> str:
    return quote(value, safe="")


def _sanitise_detail(value: Any) -> Any:
    if isinstance(value, str):
        return sanitise(value[:_SANITISE_DETAIL_STRING_MAX_CHARS])
    if isinstance(value, dict):
        return {
            str(k)[:_SANITISE_DETAIL_KEY_MAX_CHARS]: _sanitise_detail(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitise_detail(v) for v in value[:_SANITISE_DETAIL_LIST_LIMIT]]
    return value


def _compact_log_detail(value: Any, *, limit: int = 2000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = str(value)
    return sanitise(text[:limit])


def _upstream_request_url(exc: httpx.HTTPStatusError) -> str:
    try:
        return sanitise(str(exc.request.url))
    except Exception:
        return ""


def _raise_upstream_error(exc: httpx.HTTPStatusError) -> None:
    try:
        exc.response.read()
    except httpx.HTTPError:
        pass
    try:
        detail: Any = _sanitise_detail(exc.response.json())
    except Exception:
        detail = {"code": "openapi_error", "message": sanitise(exc.response.text[:500])}
    if isinstance(detail, dict):
        detail = dict(detail)
        detail.setdefault("code", f"openapi_http_{exc.response.status_code}")
        detail.setdefault("upstream_status", exc.response.status_code)
        detail.setdefault("upstream_url", _upstream_request_url(exc))
    LOGGER.warning(
        "OpenAPI upstream request failed method=%s url=%s status=%s reason=%s "
        "content_type=%s request_id=%s detail=%s",
        exc.request.method,
        _upstream_request_url(exc),
        exc.response.status_code,
        exc.response.reason_phrase,
        exc.response.headers.get("content-type", ""),
        exc.response.headers.get("x-request-id")
        or exc.response.headers.get("x-ms-request-id")
        or exc.response.headers.get("x-correlation-id")
        or "",
        _compact_log_detail(detail),
    )
    raise HTTPException(exc.response.status_code, detail=detail) from exc


def submit_job(
    payload: dict[str, Any],
    *,
    base_url: str | None = None,
    api_token: str | None = None,
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
    submit_path: str = "/api/v1/elastic-blast/submit",
) -> dict[str, Any]:
    """POST the canonical submit body to the sibling OpenAPI service.

    When the full ``subscription_id`` / ``resource_group`` / ``cluster_name``
    context is supplied (and no explicit ``base_url`` / ``api_token`` override),
    the base URL and API token are resolved from the **per-cluster** runtime
    cache keys (#26) so a multi-cluster revision targets the requested cluster
    rather than the globally most-recently-written endpoint/token. Empty
    context preserves the legacy global-key behaviour.

    When the forwarded payload carries an ``idempotency_key`` the sibling can
    safely dedupe a re-send, so this client retries up to
    ``_SUBMIT_MAX_TRANSPORT_RETRIES`` times on transient transport failures
    (httpx.ConnectError / httpx.ReadTimeout / etc.). Without one the call is
    surfaced to the caller on the first failure to avoid creating duplicate
    jobs on the cluster.

    The sibling dedupes ONLY on ``idempotency_key`` — ``external_correlation_id``
    is correlation/tracing metadata it deliberately does NOT treat as a dedupe
    key (its ``test_external_correlation_id_is_not_idempotency_key``). So when
    the caller did not supply an explicit ``idempotency_key`` we derive one
    here from the unique-per-submit ``external_correlation_id``. Without this a
    retried submit whose first attempt actually created the job (response lost
    to a ReadTimeout under burst, when the single-worker sibling serialises
    submits) would create a DUPLICATE cluster job on every retry. A
    caller-supplied ``idempotency_key`` always wins.
    """
    if isinstance(payload, dict) and not payload.get("idempotency_key"):
        derived_key = str(payload.get("external_correlation_id") or "").strip()
        if derived_key:
            # Copy so we never mutate the caller's payload dict.
            payload = {**payload, "idempotency_key": derived_key}

    # Date-tiered results layout (dashboard STORAGE_DATE_LAYOUT_ENABLED): ask the
    # sibling to write this external job's results under
    # ``results/<YYYY/MM/DD>/<job_id>/`` instead of the flat ``results/<job_id>/``
    # so SB / OpenAPI jobs match the native date tiering. This is the single
    # choke point every external submit surface (SB drain, the XML
    # ``/api/v1/elastic-blast/submit`` direct path, and the canonical external
    # submit) flows through. The sibling appends its OWN job id; an older sibling
    # that does not know the field ignores it (``extra=allow``), so sending it is
    # safe whenever the layout is on. A caller that already set ``results_prefix``
    # (including an explicit ``""`` to force the flat layout) wins, so only an
    # ABSENT key triggers injection. Never fail a submit over this optional hint.
    if isinstance(payload, dict) and "results_prefix" not in payload:
        try:
            from api.services.storage.job_prefix import (
                date_layout_enabled,
                dated_results_subdir,
            )

            if date_layout_enabled():
                payload = {**payload, "results_prefix": dated_results_subdir()}
        except Exception:
            # An import/compute failure here means date tiering meant to be on
            # silently degrades to flat — surface it (rare: job_prefix is a core
            # module) instead of hiding it at debug level.
            LOGGER.warning("results_prefix injection skipped", exc_info=True)

    has_idempotency_key = bool(isinstance(payload, dict) and payload.get("idempotency_key"))
    import time as _time

    attempts = 1 + (_SUBMIT_MAX_TRANSPORT_RETRIES if has_idempotency_key else 0)
    last_transport_exc: HTTPException | None = None
    resolved_base = _base_url(
        base_url,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    for attempt_index in range(attempts):
        try:
            # A stale-token 401 self-heals once inside the helper (idempotent:
            # the retry reuses the same idempotency_key, so the sibling dedupes
            # a job the first attempt may have created).
            resp = _request_with_token_resync(
                base_url=resolved_base,
                timeout=_DEFAULT_TIMEOUT_SECONDS,
                api_token=api_token,
                subscription_id=subscription_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
                send=lambda client: client.post(
                    submit_path, json=payload
                ),
                label="submit_job",
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 4xx/5xx with a response body: surface immediately. The
            # sibling already decided — retrying won't change it (and a
            # 5xx may itself indicate a created-but-failed job).
            _raise_upstream_error(exc)
        except httpx.HTTPError as exc:
            # Transport-level failure (connection refused / reset /
            # read timeout). Retry only when idempotent.
            transport_exc = HTTPException(
                503,
                detail={
                    "code": "openapi_unreachable",
                    "message": sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS]),
                    "attempt": attempt_index + 1,
                    "max_attempts": attempts,
                },
            )
            last_transport_exc = transport_exc
            if attempt_index + 1 < attempts:
                backoff = _SUBMIT_RETRY_BACKOFF_SECONDS[
                    min(attempt_index, len(_SUBMIT_RETRY_BACKOFF_SECONDS) - 1)
                ]
                LOGGER.info(
                    "openapi submit transport failure attempt=%s/%s sleep=%ss reason=%s",
                    attempt_index + 1,
                    attempts,
                    backoff,
                    type(exc).__name__,
                )
                _time.sleep(backoff)
                continue
            raise transport_exc from exc
        return cast(dict[str, Any], resp.json())
    # Defensive: the loop above always either returns or raises. If we
    # somehow exit without doing either, surface the last transport error.
    if last_transport_exc is not None:
        raise last_transport_exc
    raise HTTPException(
        503,
        detail={"code": "openapi_unreachable", "message": "submit failed without explicit error"},
    )


def submit_job_v1(
    payload: dict[str, Any],
    *,
    base_url: str | None = None,
    api_token: str | None = None,
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
) -> dict[str, Any]:
    """Submit via the sibling ``POST /v1/jobs`` (free-form ``blast_options``).

    Unlike :func:`submit_job` (which posts to ``/api/v1/elastic-blast/submit``,
    where the sibling forces ``-outfmt 5`` XML), this posts the
    ``JobSubmitRequest`` shape directly so a caller can request a multi-token
    tabular layout (e.g. ``-outfmt 7 std staxids sstrand qseq sseq``). Both
    endpoints land in the same sibling job store, so status/get/file tracking is
    identical. Shares the transport-retry + stale-token-401 self-heal contract
    by delegating to :func:`submit_job` with the ``/v1/jobs`` path.
    """
    return submit_job(
        payload,
        base_url=base_url,
        api_token=api_token,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        submit_path="/v1/jobs",
    )


def get_job(
    job_id: str,
    *,
    base_url: str | None = None,
    api_token: str | None = None,
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
) -> dict[str, Any]:
    resolved_base = _base_url(
        base_url,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    try:
        resp = _request_with_token_resync(
            base_url=resolved_base,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            api_token=api_token,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            send=lambda client: client.get(
                f"/api/v1/elastic-blast/jobs/{_path_segment(job_id)}"
            ),
            label="get_job",
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_upstream_error(exc)
    except httpx.HTTPError as exc:
        raise HTTPException(
            503,
            detail={
                "code": "openapi_unreachable",
                "message": sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS]),
            },
        ) from exc
    return cast(dict[str, Any], resp.json())



def delete_job(
    job_id: str,
    *,
    base_url: str | None = None,
    api_token: str | None = None,
) -> dict[str, Any]:
    """Cancel + delete a job via the sibling OpenAPI ``DELETE /v1/jobs/{id}``.

    The sibling service owns the AKS cluster the job actually runs on and
    holds the in-cluster kubeconfig, so it sets the cancel event and tears
    down the K8s resources itself. The dashboard never needs the cluster
    coordinates for this path — it only has to reach the sibling's API,
    which falls back to the cached runtime endpoint when ``base_url`` is
    not supplied (same resolution the jobs-list sync uses).
    """
    with httpx.Client(
        base_url=_base_url(base_url),
        timeout=_DEFAULT_TIMEOUT_SECONDS,
        headers=_headers(api_token=api_token),
    ) as client:
        try:
            resp = client.delete(f"/v1/jobs/{_path_segment(job_id)}")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_upstream_error(exc)
        except httpx.HTTPError as exc:
            raise HTTPException(
                503,
                detail={
                    "code": "openapi_unreachable",
                    "message": sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS]),
                },
            ) from exc
        return cast(dict[str, Any], resp.json())


def list_jobs(
    *,
    base_url: str | None = None,
    api_token: str | None = None,
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
    limit: int | None = None,
) -> dict[str, Any]:
    """List all jobs tracked by the external ElasticBLAST OpenAPI service.

    The legacy `/v1/jobs` endpoint is the only listing surface exposed by the
    sibling service today; the newer `/api/v1/elastic-blast/...` contract has
    submit/get/file but no list. The shape is `{"jobs": [...], "count": N}`.

    Supplying the cluster context resolves the per-cluster base URL + token
    (#26); empty context keeps the legacy global-key resolution.

    ``limit`` (#51) bounds the fetch to the most-recent N jobs once the sibling
    supports `/v1/jobs?limit=` (commit ``2da82ca2``). An older sibling simply
    ignores the unknown query param and returns the full list, so passing it is
    always safe (degrades cleanly). The ``next_cursor`` the sibling returns
    alongside is intentionally not consumed here: discovered external rows are
    upserted into the local Table by ``collect_and_sync_external_jobs`` and then
    served by the bounded local time-ordered index (#50), so the local index is
    the effective combined cursor — a bounded discovery fetch is all that is
    needed from the external side.
    """

    resolved_base = _base_url(
        base_url,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    params: dict[str, Any] = {}
    if isinstance(limit, int) and limit > 0:
        params["limit"] = limit
    try:
        resp = _request_with_token_resync(
            base_url=resolved_base,
            timeout=_LIST_TIMEOUT_SECONDS,
            api_token=api_token,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            send=lambda client: client.get("/v1/jobs", params=params),
            label="list_jobs",
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_upstream_error(exc)
    except httpx.HTTPError as exc:
        raise HTTPException(
            503,
            detail={
                "code": "openapi_unreachable",
                "message": sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS]),
            },
        ) from exc
    return cast(dict[str, Any], resp.json())


def reset_ready_cache() -> None:
    """Drop the in-process /v1/ready cache.

    Tests use this to keep TTL behaviour deterministic without juggling
    ``time.monotonic``. Routes do not need to call it — the cache TTL is
    intentionally short (default 5 s).
    """
    with _READY_CACHE_LOCK:
        _READY_CACHE.clear()
    with _READY_INFLIGHT_LOCK:
        for event in _READY_INFLIGHT.values():
            event.set()
        _READY_INFLIGHT.clear()


def _ready_cache_lookup(
    key: tuple[str, str],
) -> tuple[dict[str, Any] | HTTPException, float] | None:
    """Return ``(value, age_seconds)`` for a hit, or ``None`` for a miss.

    The age lets the caller emit a one-line ``ready_probe_cached`` log entry
    so App Insights still sees an outage during the TTL window (without the
    age, a 5 s cache turns a 30 s outage into a single WARN line).
    """
    if _READY_CACHE_TTL_SECONDS <= 0:
        return None
    import time as _time

    now = _time.monotonic()
    with _READY_CACHE_LOCK:
        cached = _READY_CACHE.get(key)
        if not cached:
            return None
        expires, value = cached
        if expires < now:
            _READY_CACHE.pop(key, None)
            return None
        # ``expires == stored_at + ttl`` → age = ttl - (expires - now).
        age = max(0.0, _READY_CACHE_TTL_SECONDS - (expires - now))
        return value, age


def _ready_cache_store(
    key: tuple[str, str], value: dict[str, Any] | HTTPException, *, ttl: float | None = None
) -> None:
    if _READY_CACHE_TTL_SECONDS <= 0:
        return
    import time as _time

    effective_ttl = _READY_CACHE_TTL_SECONDS if ttl is None else ttl
    if effective_ttl <= 0:
        return
    expires = _time.monotonic() + effective_ttl
    with _READY_CACHE_LOCK:
        _READY_CACHE[key] = (expires, value)


def _ready_cache_key(base_url: str | None, api_token: str | None) -> tuple[str, str]:
    """Cache key = normalised base URL + full digest of the token.

    ``base`` is lower-cased and the trailing slash is stripped so callers that
    happen to pass ``https://x.io`` and ``https://x.io/`` share one cache slot
    (otherwise we silently halve the cache hit rate and double sibling load).

    The token itself is never used as a key; we store a full BLAKE2b hex digest
    (64 chars via a 32-byte digest) so the cache never retains the raw token
    string. The original ``[:8]`` truncation gave a birthday collision at
    ~65 k unique tokens.
    """
    import hashlib as _hashlib

    base = (base_url or "").strip().rstrip("/").lower()
    token = (api_token or "").strip()
    digest = (
        _hashlib.blake2b(
            token.encode("utf-8", "ignore"),
            digest_size=32,
            person=b"elb-ready-cache",
        ).hexdigest()
        if token
        else ""
    )
    return base, digest


def _ready_inflight_acquire(key: tuple[str, str]) -> tuple[bool, threading.Event]:
    """Reserve the upstream slot for ``key``.

    Returns ``(is_leader, event)``. The leader is responsible for firing the
    HTTP probe and calling :func:`_ready_inflight_release` (always, in a
    ``finally``) so waiters unblock even if the upstream call raises.
    Waiters block on ``event.wait`` and then re-check the cache.
    """
    with _READY_INFLIGHT_LOCK:
        existing = _READY_INFLIGHT.get(key)
        if existing is not None:
            return False, existing
        event = threading.Event()
        _READY_INFLIGHT[key] = event
        return True, event


def _ready_inflight_release(key: tuple[str, str]) -> None:
    """Wake waiters and drop the slot.

    Idempotent so callers can release in a ``finally`` without checking they
    were actually the leader.
    """
    with _READY_INFLIGHT_LOCK:
        event = _READY_INFLIGHT.pop(key, None)
    if event is not None:
        event.set()


def ready(
    *,
    base_url: str | None = None,
    api_token: str | None = None,
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
) -> dict[str, Any]:
    """Pre-flight the sibling's submit path before issuing ``submit_job``.

    Returns the sibling's ``/v1/ready`` JSON on success. The sibling enforces
    a hard ~2.5s budget and reports three independent probes (k8s API, workload
    node pool, openapi pod). This client adds a 2.5s slack so the structured 503
    always arrives first.

    Successful and 503 responses are cached for ``OPENAPI_READY_CACHE_TTL_SECONDS``
    seconds (default 5s) keyed by ``(base_url, sha256(token))`` so a burst
    of probes from the SPA + the internal submit gate does not flood the
    sibling. Concurrent cache-miss callers serialise on a per-key in-flight
    Event so only one upstream HTTP probe fires even under N workers / N
    parallel requests — prevents the dashboard from melting its own
    30-req/min sibling budget on cold cache. The cache is intentionally
    tiny — long enough to absorb a retry storm, short enough that an
    operator starting AKS sees the gate clear almost immediately.

    Failure modes (all raise ``HTTPException`` so the route handler can pass
    the sanitised detail straight to the caller):

    * ``503 openapi_not_ready``   — sibling answered 503; ``upstream_code`` carries
      the specific sibling code (e.g. ``no_workload_nodes`` /
      ``openapi_pod_not_ready``) so the SPA / 3rd-party caller can map a
      remediation action.
    * ``503 openapi_unreachable`` — transport-level failure. Most common cause is
      AKS stopped; the sibling did not respond at all.
    * Sibling lacking ``/v1/ready`` (older image) → caller-side fail-open:
      returns ``{"ready": True, "skipped": "version_mismatch", "version":
      "unknown"}`` so the submit can proceed. A WARNING is emitted on every
      stale-sibling hit so operators can grep for ``ready_probe_stale_sibling``
      and bump the elb-openapi image to the latest tag (≥ 4.15).
    """
    resolved_base = _base_url(
        base_url,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    resolved_api_token = _headers(
        api_token=api_token,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    ).get("X-ELB-API-Token")
    cache_key = _ready_cache_key(resolved_base, resolved_api_token)
    cached_entry = _ready_cache_lookup(cache_key)
    if cached_entry is not None:
        cached_value, cached_age = cached_entry
        if isinstance(cached_value, HTTPException):
            detail = cached_value.detail if isinstance(cached_value.detail, dict) else {}
            LOGGER.info(
                "ready_probe cache hit (cached failure) status=%s code=%s age=%.2fs",
                cached_value.status_code,
                detail.get("code", "unspecified"),
                cached_age,
                extra={
                    "event": "ready_probe_cached",
                    "cached": True,
                    "status": cached_value.status_code,
                    "code": str(detail.get("code") or "unspecified"),
                    "cached_age_seconds": round(cached_age, 3),
                },
            )
            raise cached_value
        LOGGER.debug(
            "ready_probe cache hit (cached success) age=%.2fs",
            cached_age,
            extra={
                "event": "ready_probe_cached",
                "cached": True,
                "status": 200,
                "cached_age_seconds": round(cached_age, 3),
            },
        )
        return cached_value

    is_leader, inflight_event = _ready_inflight_acquire(cache_key)
    if not is_leader:
        # Another caller is firing the upstream HTTP probe right now. Wait
        # for them, then re-read the cache. The leader-swap loop is bounded
        # by ``_READY_INFLIGHT_MAX_WAIT_ROUNDS`` (critique #20.12) so a
        # pathological "leader keeps timing out and a new caller takes over"
        # cycle cannot pin us forever \u2014 after the cap we fall through
        # and fire our own probe, accepting one extra upstream request as
        # the price of a bounded latency.
        for _round in range(_READY_INFLIGHT_MAX_WAIT_ROUNDS):
            inflight_event.wait(timeout=_READY_INFLIGHT_WAIT_SECONDS)
            cached_entry = _ready_cache_lookup(cache_key)
            if cached_entry is not None:
                cached_value, cached_age = cached_entry
                if isinstance(cached_value, HTTPException):
                    raise cached_value
                return cached_value
            # Try to become leader ourselves \u2014 if we win, exit the
            # wait loop and probe upstream. If a sibling beat us to it,
            # loop and wait on the new leader.
            is_leader, inflight_event = _ready_inflight_acquire(cache_key)
            if is_leader:
                break
        # Loop exited without leadership: cap reached. Fire our own probe
        # without leader status (the active leader still owns the slot;
        # we simply do not register a new one).

    try:
        return _ready_probe_upstream(cache_key, resolved_base, api_token=resolved_api_token)
    finally:
        _ready_inflight_release(cache_key)


def _ready_probe_upstream(
    cache_key: tuple[str, str],
    resolved_base: str,
    *,
    api_token: str | None,
    allow_token_resync: bool = True,
) -> dict[str, Any]:
    """Single upstream HTTP probe + cache store. Always called by the leader.

    Split out of :func:`ready` so the in-flight serialisation in ``ready``
    stays declarative.

    ``allow_token_resync`` guards a one-shot reactive recovery: when the
    sibling answers **401** the dashboard's runtime token cache is almost
    always stale (the ephemeral Redis sidecar was wiped by a control-plane
    redeploy while the elb-openapi pod kept its minted token). We re-read
    the live token from the deployment env, sync it back into the cache,
    and retry once. The retry sets ``allow_token_resync=False`` so a pod
    that genuinely rejects the freshly-read token cannot loop.
    """
    with httpx.Client(
        base_url=resolved_base,
        timeout=_READY_TIMEOUT_SECONDS,
        headers=_headers(api_token=api_token),
    ) as client:
        try:
            resp = client.get("/v1/ready")
        except httpx.HTTPError as exc:
            err = HTTPException(
                503,
                detail={
                    "code": "openapi_unreachable",
                    "message": sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS]),
                    "probe": "ready",
                },
            )
            # Transport errors get half-TTL so a reachable-but-flaky path
            # recovers fast; we still rate-limit retry storms.
            _ready_cache_store(cache_key, err, ttl=_READY_CACHE_TTL_SECONDS / 2)
            raise err from exc
        if resp.status_code == 401 and allow_token_resync:
            # Stale / missing X-ELB-API-Token. Re-read the live token from
            # the elb-openapi deployment env, sync it to the runtime cache,
            # and retry once with the recovered token. The 401 itself is
            # never cached, so a failed recovery simply surfaces the
            # original error and the next probe will try again.
            healed = ""
            try:
                from api.services.openapi.token import (
                    resync_openapi_api_token_from_cluster,
                )

                healed = resync_openapi_api_token_from_cluster()
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.warning(
                    "openapi /v1/ready 401 token resync raised %s",
                    type(exc).__name__,
                )
            if healed:
                LOGGER.warning(
                    "openapi /v1/ready returned 401 — token resynced from "
                    "cluster; retrying probe once",
                    extra={"event": "ready_probe_token_resync"},
                )
                return _ready_probe_upstream(
                    cache_key,
                    resolved_base,
                    api_token=healed,
                    allow_token_resync=False,
                )
        if resp.status_code == 404:
            LOGGER.warning(
                "openapi /v1/ready returned 404 — sibling image is pre-4.15 "
                "and cannot report readiness. Failing open. Bump elb-openapi "
                "to the latest tag to enable real submit-path gating.",
                extra={"event": "ready_probe_stale_sibling"},
            )
            payload = {
                "ready": True,
                "skipped": "version_mismatch",
                "version": "unknown",
            }
            _ready_cache_store(cache_key, payload)
            return payload
        if resp.status_code == 429:
            # Sibling rate-limited the probe. Surface the same structured code
            # so SPA / 3rd-party callers can back off cleanly without retrying.
            try:
                rl_payload: Any = _sanitise_detail(resp.json())
            except Exception:
                rl_payload = {"code": "openapi_ready_rate_limited"}
            upstream_msg = ""
            limit = 0
            if isinstance(rl_payload, dict):
                upstream_msg = str(rl_payload.get("message") or "")
                try:
                    limit = int(rl_payload.get("limit_per_minute") or 0)
                except Exception:
                    limit = 0
            err = HTTPException(
                429,
                detail={
                    "code": "openapi_ready_rate_limited",
                    "message": upstream_msg
                    or "Sibling /v1/ready rate-limited this caller. Retry after 60s.",
                    "limit_per_minute": limit,
                },
            )
            _ready_cache_store(cache_key, err, ttl=_READY_CACHE_TTL_SECONDS)
            raise err
        if resp.status_code == 503:
            try:
                detail_payload: Any = _sanitise_detail(resp.json())
            except Exception:
                detail_payload = {
                    "code": "openapi_not_ready",
                    "message": sanitise(resp.text[:500]),
                }
            upstream_code = ""
            checks: Any = {}
            upstream_message = ""
            if isinstance(detail_payload, dict):
                upstream_code = str(detail_payload.get("code") or "")
                checks = detail_payload.get("checks") or {}
                upstream_message = str(detail_payload.get("message") or "")
            err = HTTPException(
                503,
                detail={
                    "code": "openapi_not_ready",
                    "upstream_code": upstream_code or "unspecified",
                    "message": upstream_message
                    or "Sibling OpenAPI reported the submit path is not ready.",
                    "checks": checks,
                },
            )
            LOGGER.warning(
                "openapi_not_ready upstream_code=%s message=%s",
                upstream_code or "unspecified",
                _compact_log_detail(upstream_message, limit=300),
                extra={"event": "ready_probe", "code": upstream_code or "unspecified"},
            )
            _ready_cache_store(cache_key, err)
            raise err
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_upstream_error(exc)
        payload = cast(dict[str, Any], resp.json())
        _ready_cache_store(cache_key, payload)
        return payload


def download_file(
    job_id: str,
    file_id: str,
    *,
    base_url: str | None = None,
    api_token: str | None = None,
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
) -> DownloadedFile:
    with httpx.Client(
        base_url=_base_url(
            base_url,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        ),
        timeout=_STREAM_TIMEOUT,
        headers=_headers(
            api_token=api_token,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        ),
    ) as client:
        try:
            resp = client.get(
                f"/api/v1/elastic-blast/jobs/{_path_segment(job_id)}/files/{_path_segment(file_id)}"
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_upstream_error(exc)
        except httpx.HTTPError as exc:
            raise HTTPException(
                503,
                detail={
                    "code": "openapi_unreachable",
                    "message": sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS]),
                },
            ) from exc
    content_disposition = resp.headers.get("content-disposition", "")
    filename = "blast_result.xml"
    if "filename=" in content_disposition:
        filename = _safe_filename(content_disposition.split("filename=", 1)[1])
    return DownloadedFile(
        content=resp.content,
        media_type=resp.headers.get("content-type", "application/xml").split(";", 1)[0],
        filename=filename,
    )


def stream_file(
    job_id: str,
    file_id: str,
    *,
    base_url: str | None = None,
    api_token: str | None = None,
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
) -> StreamedFile:
    resolved_base = _base_url(
        base_url,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    target_path = (
        f"/api/v1/elastic-blast/jobs/{_path_segment(job_id)}/files/{_path_segment(file_id)}"
    )

    def _open(token_override: str | None) -> tuple[httpx.Client, httpx.Response]:
        client = httpx.Client(
            base_url=resolved_base,
            timeout=_STREAM_TIMEOUT,
            headers=_headers(
                api_token=token_override,
                subscription_id=subscription_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
            ),
        )
        # Close the just-created client if the connection itself fails (e.g. the
        # AKS cluster — and thus the elb-openapi pod — is stopped). Without this
        # the client leaks a connection pool on every unreachable-openapi
        # download AND the caller's ``client`` stays unbound, so the except
        # blocks below would raise ``UnboundLocalError`` (a 500) instead of the
        # intended 503 ``openapi_unreachable``.
        try:
            request = client.build_request("GET", target_path)
            return client, client.send(request, stream=True)
        except BaseException:
            client.close()
            raise

    client: httpx.Client | None = None
    try:
        client, resp = _open(api_token)
        # Self-heal a stale-token 401 exactly once — same failure mode and
        # recovery as `_request_with_token_resync` (a control-plane redeploy or
        # cluster restart wipes the dashboard's ephemeral token cache while the
        # elb-openapi pod keeps its minted token). Without this, the Service-Bus
        # completion `download_url` and the Results "download" button surface a
        # spurious 401 after every redeploy/restart. Streaming responses can't be
        # retried in place, so close and reopen with the recovered token.
        if resp.status_code == 401:
            resp.close()
            client.close()
            client = None
            healed = _resync_token_after_401()
            if healed:
                LOGGER.warning(
                    "openapi stream_file returned 401 — token resynced from cluster; retrying once",
                    extra={"event": "openapi_token_resync_retry"},
                )
                client, resp = _open(healed)
            else:
                client, resp = _open(api_token)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if client is not None:
            client.close()
        _raise_upstream_error(exc)
    except httpx.HTTPError as exc:
        if client is not None:
            client.close()
        raise HTTPException(
            503,
            detail={
                "code": "openapi_unreachable",
                "message": sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS]),
            },
        ) from exc

    content_disposition = resp.headers.get("content-disposition", "")
    filename = "blast_result.xml"
    if "filename=" in content_disposition:
        filename = _safe_filename(content_disposition.split("filename=", 1)[1])

    def _iter() -> Iterator[bytes]:
        try:
            yield from resp.iter_bytes()
        finally:
            resp.close()
            client.close()

    return StreamedFile(
        chunks=_iter(),
        media_type=resp.headers.get("content-type", "application/xml").split(";", 1)[0],
        filename=filename,
    )


def stream_result_file_from_storage(job_id: str, file_id: str) -> StreamedFile:
    """Stream an external job's result file directly from Storage.

    Fallback used when the elb-openapi proxy is unreachable (the AKS cluster
    auto-stopped). Resolves ``file_id -> blob_path`` from the durable
    ``result_manifest`` column captured on the JobState row at the succeeded
    transition, then streams ``results/{job_id}/{blob_path}`` from the job's
    trusted workload Storage account through the ``api`` sidecar via the shared
    managed identity — never a SAS / direct blob URL handed to the caller
    (charter §9). Raises ``HTTPException(404, result_unavailable_offline)`` when
    the row / manifest / file / account is unknown (e.g. a job that completed
    before the manifest was captured), so the caller can surface the original
    ``openapi_unreachable`` error instead of a misleading failure.
    """
    import json as _json

    from api.services import get_credential
    from api.services.state_repo import get_state_repo
    from api.services.storage.blob_io import stream_blob_bytes

    def _offline_404(message: str) -> HTTPException:
        return HTTPException(
            404,
            detail={"code": "result_unavailable_offline", "message": message},
        )

    try:
        state = get_state_repo().get(job_id)
    except Exception:
        state = None
    if state is None:
        raise _offline_404("job not found for offline result download")

    blob_path = ""
    raw_manifest = getattr(state, "result_manifest", None)
    if raw_manifest:
        try:
            manifest = _json.loads(raw_manifest)
        except Exception:
            manifest = []
        for item in manifest if isinstance(manifest, list) else []:
            if isinstance(item, dict) and str(item.get("file_id") or "") == file_id:
                blob_path = str(item.get("blob_path") or "").strip()
                break
    if not blob_path:
        raise _offline_404("result file is unavailable while the cluster is stopped")

    account = str(getattr(state, "storage_account", "") or "").strip()
    if not account:
        raise _offline_404("result storage account is not recorded for this job")

    # ``blob_path`` is relative to ``results/{job_id}/`` (the sibling's
    # ``_list_result_files`` contract). ``stream_blob_bytes`` validates the full
    # path against traversal and gates concurrency (§9).
    full_path = f"{job_id}/{blob_path.lstrip('/')}"
    filename = _safe_filename(blob_path.rsplit("/", 1)[-1])
    media_type = "application/gzip" if filename.endswith(".gz") else "application/xml"
    return StreamedFile(
        chunks=stream_blob_bytes(get_credential(), account, "results", full_path),
        media_type=media_type,
        filename=filename,
    )
