"""Runtime endpoint cache for the ElasticBLAST OpenAPI service.

Responsibility: Runtime endpoint and API token cache for the ElasticBLAST OpenAPI service
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_redis_url`, `_normalise_base_url`, `save_openapi_base_url`,
`get_openapi_base_url`, `save_openapi_api_token`, `get_openapi_api_token`,
`save_openapi_public_base_url`, `get_openapi_public_base_url`,
`clear_openapi_public_base_url`, `list_openapi_public_base_urls`,
`get_public_tls_base_url`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries. `get_public_tls_base_url` returns an empty string when neither
`OPENAPI_PUBLIC_BASE_URL` env nor the public-base-url cache is set, so legacy
call sites can short-circuit and keep using the IP-based path with zero
behaviour change.
The IP-based runtime endpoint (`save_openapi_base_url` / `get_openapi_base_url`)
is now mirrored into the durable `dashboardsingletons` Storage Table in addition
to ops Redis, so a Container App revision restart (which wipes the in-revision
Redis) does not lose the last-known endpoint. `get_openapi_base_url` rehydrates
Redis from the durable copy on a cold read, gated by a freshness TTL
(`OPENAPI_RUNTIME_ENDPOINT_MAX_AGE_SECONDS`, default 1 h) so a long-Stopped
cluster's now-unreachable cached IP is not served indefinitely.
Multi-cluster: when a setup / disable / reconcile call passes
``cluster_arm_id`` the cache writes a per-cluster key under
``openapi:runtime:public-base-url:cluster:<sha256[:16]>`` so a second
cluster cannot silently overwrite the first cluster's entry. The legacy
``openapi:runtime:public-base-url`` key is still maintained as the
"most recently set cluster" display fallback for SPA polling routes
that do not yet pass cluster context.
The API token cache is keyed the same way: ``save_openapi_api_token``
writes both the legacy global ``openapi:runtime:api-token`` key and, when
its ``metadata`` carries ``subscription_id`` / ``resource_group`` /
``cluster_name``, a per-cluster key ``openapi:runtime:api-token:cluster:
<sha256[:16]>``. ``get_openapi_api_token`` reads the per-cluster key first
when given cluster context (deploy path) and falls back to the global key
for context-less readers that pair with the global base-url.
``save_openapi_public_base_url`` returns False when the durable Storage
Table write fails so the caller can mark the task partially-degraded
even if the hot Redis write succeeded.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import os
import time
from typing import Any

from api.services.redis_clients import get_ops_redis_client

LOGGER = logging.getLogger(__name__)

_RUNTIME_KEY = "openapi:runtime:base-url"
_TOKEN_KEY = "openapi:runtime:api-token"  # noqa: S105 - Redis key name, not a secret value.
_TOKEN_CLUSTER_PREFIX = f"{_TOKEN_KEY}:cluster:"
_PUBLIC_BASE_URL_KEY = "openapi:runtime:public-base-url"
_PUBLIC_BASE_URL_CLUSTER_PREFIX = f"{_PUBLIC_BASE_URL_KEY}:cluster:"

# Upper bound on how long a durably-cached IP-based runtime endpoint may be
# served after a Container App revision restart wiped the in-revision Redis.
# The ephemeral Redis cache (and the in-memory client-kwargs cache) are lost on
# every deploy; rehydrating the last-known endpoint from the durable Storage
# Table lets external-job features keep working immediately after a restart
# (cluster still Running) instead of waiting for the next live ``k8s_get_service_ip``
# resolution or the 120 s reconciler tick. The freshness guard bounds the
# staleness: a cluster that has been Stopped longer than this max-age no longer
# serves its (now-unreachable) cached IP, so the caller degrades to
# ``openapi_not_configured`` exactly as before this durable backing existed.
_RUNTIME_ENDPOINT_MAX_AGE_ENV = "OPENAPI_RUNTIME_ENDPOINT_MAX_AGE_SECONDS"
_RUNTIME_ENDPOINT_MAX_AGE_DEFAULT = 3600.0


def _redis_url() -> str:
    return os.environ.get("OPS_REDIS_URL", "redis://127.0.0.1:6379/2")


def _runtime_endpoint_max_age() -> float:
    """Max age (seconds) a durably-cached runtime endpoint may be served.

    Override with ``OPENAPI_RUNTIME_ENDPOINT_MAX_AGE_SECONDS``; default 1 hour.
    A non-positive / unparseable override disables the durable rehydration
    (returns ``0`` → the cold-path read is skipped, preserving pre-durable
    behaviour exactly).
    """
    raw = os.environ.get(_RUNTIME_ENDPOINT_MAX_AGE_ENV, "").strip()
    if not raw:
        return _RUNTIME_ENDPOINT_MAX_AGE_DEFAULT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _RUNTIME_ENDPOINT_MAX_AGE_DEFAULT
    return value if value > 0 else 0.0


def _payload_age_seconds(payload: dict[str, Any]) -> float | None:
    """Seconds since ``payload['updated_at']`` (UTC ``%Y-%m-%dT%H:%M:%SZ``).

    Returns ``None`` when the timestamp is missing or unparseable so the
    caller can treat an undatable payload as *not fresh* (fail-closed) rather
    than serving a potentially-ancient endpoint.
    """
    raw = str(payload.get("updated_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = time.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None
    return max(0.0, time.time() - calendar.timegm(parsed))



def _normalise_base_url(value: str) -> str:
    return value.strip().rstrip("/")


def _normalise_cluster_arm_id(cluster_arm_id: str) -> str:
    """Lower-case + strip so two callers that pass the same ARM id with
    different casing (SDK quirks: ``managedClusters.get`` returns mixed
    case but kubelet identity references are lower-cased) derive the
    same per-cluster key and compare equal."""
    return (cluster_arm_id or "").strip().lower()


def _per_cluster_key(cluster_arm_id: str) -> str:
    """Return the deterministic per-cluster cache key.

    The ARM id is normalised to lower-case before hashing so two
    callers (api sidecar vs worker, ARM SDK vs kubectl) that pass the
    same cluster with different casing always derive the same key.
    """
    digest = hashlib.sha256(
        _normalise_cluster_arm_id(cluster_arm_id).encode("utf-8")
    ).hexdigest()[:16]
    return f"{_PUBLIC_BASE_URL_CLUSTER_PREFIX}{digest}"


def _cluster_arm_id_from_metadata(metadata: dict[str, Any]) -> str:
    """Re-derive the lower-cased ARM id from a stored metadata dict.

    Used by the dedupe pass in :func:`list_openapi_public_base_urls`
    and the CAS guard in :func:`_clear_legacy` so per-cluster rows and
    a legacy mirror that describe the same cluster never get processed
    twice. Returns an empty string when any of the three fields is
    missing.
    """
    sub = str(metadata.get("subscription_id") or "").strip()
    rg = str(metadata.get("resource_group") or "").strip()
    name = str(metadata.get("cluster_name") or "").strip()
    if not (sub and rg and name):
        return ""
    return (
        f"/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.ContainerService/managedClusters/{name}"
    ).lower()


def _token_cluster_key(metadata: dict[str, Any] | None) -> str:
    """Return the deterministic per-cluster Redis key for the API token.

    Derived from the same lower-cased ARM id as the public base-url
    per-cluster key so the two stay in lock-step. Returns an empty string
    when ``metadata`` is missing any of ``subscription_id`` /
    ``resource_group`` / ``cluster_name`` — the caller then falls back to
    the legacy global key.
    """
    arm_id = _cluster_arm_id_from_metadata(metadata or {})
    if not arm_id:
        return ""
    digest = hashlib.sha256(arm_id.encode("utf-8")).hexdigest()[:16]
    return f"{_TOKEN_CLUSTER_PREFIX}{digest}"


def save_openapi_base_url(
    base_url: str,
    *,
    metadata: dict[str, Any] | None = None,
    client: Any | None = None,
) -> bool:
    """Persist the currently reachable OpenAPI base URL in ops Redis + durably.

    The hot path is the in-revision Redis sidecar. We ALSO mirror the value
    into the durable ``dashboardsingletons`` Storage Table (same store the
    public-HTTPS endpoint already uses) so a Container App revision restart —
    which wipes the ephemeral Redis — does not lose the last-known endpoint.
    ``get_openapi_base_url`` rehydrates Redis from the durable copy on a cold
    read, gated by a freshness TTL. Returns the Redis-write success bit (the
    durable write is best-effort and never fails the call).
    """
    url = _normalise_base_url(base_url)
    if not url:
        return False
    payload = {
        "base_url": url,
        "metadata": metadata or {},
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # Durable mirror first (best-effort) so a Redis-write failure does not also
    # skip the durable persist. Failures are logged inside the helper.
    _durable_save_safe(_RUNTIME_KEY, payload)
    redis_client = client or get_ops_redis_client(socket_timeout=1.5)
    try:
        redis_client.set(_RUNTIME_KEY, json.dumps(payload, separators=(",", ":")))
        return True
    except Exception as exc:
        LOGGER.warning("openapi runtime endpoint cache write failed: %s", exc)
        return False


def get_openapi_base_url(*, client: Any | None = None) -> str:
    """Return the cached OpenAPI base URL, or an empty string if unavailable.

    Fast path: the in-revision Redis sidecar. Cold path (Redis miss, e.g.
    immediately after a revision restart wiped Redis): the durable
    ``dashboardsingletons`` Storage Table, but only when the stored endpoint
    is still fresh (within ``OPENAPI_RUNTIME_ENDPOINT_MAX_AGE_SECONDS``). A
    durable hit re-populates Redis so subsequent reads are hot again. A stale
    or undatable durable row is ignored (returns ``""``) so a long-Stopped
    cluster's unreachable IP is not served indefinitely.
    """
    redis_client = client or get_ops_redis_client(socket_timeout=1.5)
    try:
        raw = redis_client.get(_RUNTIME_KEY)
    except Exception as exc:
        LOGGER.debug("openapi runtime endpoint cache read failed: %s", exc)
        raw = None
    if raw is not None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(str(raw))
        except json.JSONDecodeError:
            return _normalise_base_url(str(raw))
        if isinstance(payload, dict):
            url = _normalise_base_url(str(payload.get("base_url") or ""))
            if url:
                return url
    return _rehydrate_runtime_base_url_from_durable(redis_client)


def _rehydrate_runtime_base_url_from_durable(redis_client: Any) -> str:
    """Cold-read the durable runtime endpoint, freshness-gated, rehydrate Redis.

    Returns the base URL when a fresh durable row exists, else ``""``. Never
    raises — a durable-read failure or missing table degrades to ``""`` (the
    pre-durable behaviour). The freshness guard is disabled (cold read skipped)
    when the max-age env is set to a non-positive value.
    """
    max_age = _runtime_endpoint_max_age()
    if max_age <= 0:
        return ""
    try:
        from api.services.state.singletons import load_singleton

        durable = load_singleton(_RUNTIME_KEY) or {}
    except Exception as exc:
        LOGGER.debug("openapi runtime endpoint durable read failed: %s", type(exc).__name__)
        return ""
    if not isinstance(durable, dict):
        return ""
    url = _normalise_base_url(str(durable.get("base_url") or ""))
    if not url:
        return ""
    age = _payload_age_seconds(durable)
    if age is None or age > max_age:
        # Undatable or stale: do not serve a possibly-unreachable endpoint.
        return ""
    durable["base_url"] = url
    try:
        redis_client.set(_RUNTIME_KEY, json.dumps(durable, separators=(",", ":")))
    except Exception as exc:
        LOGGER.debug(
            "openapi runtime endpoint cache re-populate failed: %s", type(exc).__name__
        )
    return url



def get_openapi_runtime_metadata(*, client: Any | None = None) -> dict[str, Any]:
    """Return the metadata dict stored alongside the cached OpenAPI base URL.

    The base-url payload written by ``save_openapi_base_url`` (at deploy
    time) carries ``subscription_id`` / ``resource_group`` /
    ``cluster_name`` in its ``metadata``. The reactive token-resync path
    (``token.resync_openapi_api_token_from_cluster``) reads this to learn
    which AKS cluster to re-read the live ``ELB_OPENAPI_API_TOKEN`` env
    from after a 401. Returns an empty dict when no endpoint is cached or
    the payload carries no metadata.
    """
    redis_client = client or get_ops_redis_client(socket_timeout=1.5)
    try:
        raw = redis_client.get(_RUNTIME_KEY)
    except Exception as exc:
        LOGGER.debug("openapi runtime metadata read failed: %s", exc)
        return {}
    if raw is None:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    metadata = payload.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def save_openapi_api_token(
    token: str,
    *,
    metadata: dict[str, Any] | None = None,
    client: Any | None = None,
) -> bool:
    """Persist the current OpenAPI API token in ops Redis.

    Writes both the legacy global key (``openapi:runtime:api-token``,
    consumed by context-less readers that pair with the global base-url)
    AND — when ``metadata`` carries ``subscription_id`` / ``resource_group``
    / ``cluster_name`` — a per-cluster key so a second cluster's token
    cannot silently overwrite the first cluster's cached token. This
    mirrors the per-cluster keying already used for the public base-url
    (``_per_cluster_key``). The deploy path reads the per-cluster key with
    explicit cluster context, so the global key staying "most recently
    written cluster" is intentional and only used by the global readers.
    """
    value = token.strip()
    if not value:
        return False
    payload = {
        "token": value,
        "metadata": metadata or {},
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    serialised = json.dumps(payload, separators=(",", ":"))
    redis_client = client or get_ops_redis_client(socket_timeout=1.5)
    ok = True
    try:
        redis_client.set(_TOKEN_KEY, serialised)
    except Exception as exc:
        LOGGER.warning("openapi runtime token cache write failed: %s", type(exc).__name__)
        ok = False
    cluster_key = _token_cluster_key(metadata)
    if cluster_key:
        try:
            redis_client.set(cluster_key, serialised)
        except Exception as exc:
            LOGGER.warning(
                "openapi runtime per-cluster token cache write failed: %s",
                type(exc).__name__,
            )
            ok = False
    return ok


def get_openapi_api_token(
    *,
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
    client: Any | None = None,
) -> str:
    """Return the cached OpenAPI API token, or an empty string if unavailable.

    When the caller passes ``subscription_id`` / ``resource_group`` /
    ``cluster_name`` the per-cluster key is tried first so a multi-cluster
    dashboard reads the token for the *requested* cluster rather than the
    globally most-recently-written one. Falls back to the legacy global
    key when no per-cluster entry exists yet (e.g. a token minted before
    this keying landed). Context-less callers keep reading the global key
    unchanged.
    """
    redis_client = client or get_ops_redis_client(socket_timeout=1.5)
    cluster_key = _token_cluster_key(
        {
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "cluster_name": cluster_name,
        }
    )
    if cluster_key:
        token = _read_token_key(redis_client, cluster_key)
        if token:
            return token
    return _read_token_key(redis_client, _TOKEN_KEY)


def _read_token_key(redis_client: Any, key: str) -> str:
    """Read + decode a token payload from a single Redis key."""
    try:
        raw = redis_client.get(key)
    except Exception as exc:
        LOGGER.debug("openapi runtime token cache read failed: %s", type(exc).__name__)
        return ""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError:
        return str(raw).strip()
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("token") or "").strip()


# Public TLS endpoint hook. When `OPENAPI_PUBLIC_BASE_URL` is set (e.g.
# `https://openapi.example.com`) the dashboard's outbound calls to the
# sibling OpenAPI service prefer this URL over the in-cluster Service IP
# discovered via `k8s_get_service_ip`. Keeps the IP path 100% intact when
# the env is unset — domain rollout is opt-in at the env layer.
_PUBLIC_BASE_URL_ENV = "OPENAPI_PUBLIC_BASE_URL"


def _build_public_payload(
    url: str, metadata: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "base_url": url,
        "metadata": metadata or {},
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _redis_set_safe(client: Any, key: str, payload: dict[str, Any]) -> bool:
    try:
        client.set(key, json.dumps(payload, separators=(",", ":")))
        return True
    except Exception as exc:
        LOGGER.warning("openapi public base url cache write failed for %s: %s", key, exc)
        return False


def _durable_save_safe(key: str, payload: dict[str, Any]) -> bool:
    try:
        from api.services.state.singletons import save_singleton

        ok = save_singleton(key, payload)
        if not ok:
            LOGGER.warning(
                "openapi public base url durable write returned False for %s", key
            )
        return bool(ok)
    except Exception as exc:
        LOGGER.warning("openapi public base url durable write failed for %s: %s", key, exc)
        return False


def _normalise_metadata_cluster_fields(
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a shallow copy with subscription/RG/cluster fields lower-cased.

    Azure ARM is case-insensitive on identity fields but the SDK
    returns mixed case (``managedClusters.get`` capitalises Resource
    Group as the operator typed it). Lower-casing the three identity
    fields at write time keeps the SPA's display + the
    ``_cluster_arm_id_from_metadata`` dedupe in lock-step regardless
    of which call path created the row.
    """
    if not isinstance(metadata, dict):
        return {}
    out = dict(metadata)
    for field in ("subscription_id", "resource_group", "cluster_name"):
        value = out.get(field)
        if isinstance(value, str) and value:
            out[field] = value.lower()
    return out


def save_openapi_public_base_url(
    base_url: str,
    *,
    metadata: dict[str, Any] | None = None,
    cluster_arm_id: str = "",
    client: Any | None = None,
) -> bool:
    """Persist the public HTTPS endpoint URL durably (Storage Table) + Redis.

    When ``cluster_arm_id`` is provided the value is keyed per-cluster so
    a second cluster's setup cannot silently overwrite the first. The
    legacy ``openapi:runtime:public-base-url`` key is also refreshed so
    SPA polling routes that do not yet pass cluster context keep
    surfacing the most-recently-set cluster's URL.

    Returns ``False`` when the *durable* write (Storage Table) fails so
    a caller can mark the task partially-degraded. The hot Redis write
    is still attempted in that case — it just won't survive a Container
    App revision restart, and the next reconcile tick may not be able
    to rehydrate the cache from durable storage either.
    """
    url = _normalise_base_url(base_url)
    if not url:
        return False
    normalised_metadata = _normalise_metadata_cluster_fields(metadata)
    payload = _build_public_payload(url, normalised_metadata)
    redis_client = client or get_ops_redis_client(socket_timeout=1.5)

    if cluster_arm_id:
        per_cluster_key = _per_cluster_key(cluster_arm_id)
        durable_ok = _durable_save_safe(per_cluster_key, payload)
        # Legacy single-key fallback gets a best-effort write so the SPA
        # status route (which doesn't yet pass cluster context) keeps
        # rendering "the most recently set cluster". Failures here do not
        # affect the per-cluster authoritative durability bit.
        _durable_save_safe(_PUBLIC_BASE_URL_KEY, payload)
        _redis_set_safe(redis_client, per_cluster_key, payload)
        _redis_set_safe(redis_client, _PUBLIC_BASE_URL_KEY, payload)
        return durable_ok

    # No cluster id supplied — legacy single-key path (kept for tests and
    # any caller that has not adopted the new signature yet).
    durable_ok = _durable_save_safe(_PUBLIC_BASE_URL_KEY, payload)
    _redis_set_safe(redis_client, _PUBLIC_BASE_URL_KEY, payload)
    return durable_ok


def _load_public_payload(
    redis_client: Any, key: str, *, recache_on_durable_hit: bool = True
) -> dict[str, Any]:
    """Read a single public-base-url payload (Redis fast path → durable cold path).

    When ``recache_on_durable_hit`` is False the cold-path read does NOT
    write the durable value back into Redis. The disable / CAS flow
    sets this to avoid the awkward "we re-cached the legacy mirror and
    then decided to skip the clear because of CAS" race where Redis
    ends up with a freshly resurrected stale entry.
    """
    try:
        raw = redis_client.get(key)
    except Exception as exc:
        LOGGER.debug("openapi public base url cache read failed for %s: %s", key, exc)
        raw = None
    redis_payload: dict[str, Any] = {}
    if raw is not None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(str(raw))
        except json.JSONDecodeError:
            url = _normalise_base_url(str(raw))
            redis_payload = {"base_url": url} if url else {}
        else:
            if isinstance(parsed, dict):
                redis_payload = parsed
        if redis_payload:
            redis_payload["base_url"] = _normalise_base_url(
                str(redis_payload.get("base_url") or "")
            )
            if redis_payload["base_url"]:
                return redis_payload
    try:
        from api.services.state.singletons import load_singleton

        durable = load_singleton(key) or {}
    except Exception as exc:
        LOGGER.debug("openapi public base url durable read failed for %s: %s", key, exc)
        return {}
    durable_url = _normalise_base_url(str(durable.get("base_url") or ""))
    if not durable_url:
        return {}
    durable["base_url"] = durable_url
    if recache_on_durable_hit:
        try:
            redis_client.set(key, json.dumps(durable, separators=(",", ":")))
        except Exception as exc:
            LOGGER.debug("openapi public base url cache re-populate failed for %s: %s", key, exc)
    return durable


def get_openapi_public_base_url(
    *,
    cluster_arm_id: str = "",
    client: Any | None = None,
) -> dict[str, Any]:
    """Return the cached public HTTPS endpoint payload, or ``{}``.

    When ``cluster_arm_id`` is provided we read the per-cluster key
    first. A miss falls back to the legacy single key only if that
    legacy entry's metadata happens to match the same cluster ARM id —
    otherwise we return ``{}`` so the SPA does not display a different
    cluster's URL by accident.

    Without ``cluster_arm_id`` the function returns the legacy single
    key payload (same behaviour as the pre-multi-cluster version).
    """
    redis_client = client or get_ops_redis_client(socket_timeout=1.5)

    if cluster_arm_id:
        per_cluster = _load_public_payload(
            redis_client, _per_cluster_key(cluster_arm_id)
        )
        if per_cluster:
            return per_cluster
        legacy = _load_public_payload(redis_client, _PUBLIC_BASE_URL_KEY)
        legacy_meta = legacy.get("metadata") if isinstance(legacy, dict) else None
        if not isinstance(legacy_meta, dict):
            return {}
        legacy_id = _cluster_arm_id_from_metadata(legacy_meta)
        if legacy_id and legacy_id == _normalise_cluster_arm_id(cluster_arm_id):
            return legacy
        return {}

    return _load_public_payload(redis_client, _PUBLIC_BASE_URL_KEY)


def list_openapi_public_base_urls(
    *, client: Any | None = None
) -> list[dict[str, Any]]:
    """Enumerate every per-cluster public HTTPS entry.

    Returns a list of payload dicts (same shape as
    ``get_openapi_public_base_url`` returns). The legacy single-key
    entry is included only if no per-cluster row with the matching
    ``metadata.subscription_id/resource_group/cluster_name`` exists, so
    callers (the reconciler) do not process the same cluster twice.
    """
    del client  # durable path drives this — Redis is only a hot cache.
    try:
        from api.services.state.singletons import list_singletons_by_prefix

        per_cluster_rows = list_singletons_by_prefix(_PUBLIC_BASE_URL_CLUSTER_PREFIX)
    except Exception as exc:
        LOGGER.warning(
            "openapi public base url enumerate failed: %s", type(exc).__name__
        )
        per_cluster_rows = []

    results: list[dict[str, Any]] = []
    seen_cluster_ids: set[str] = set()
    for _row_key, payload in per_cluster_rows:
        if not isinstance(payload, dict):
            continue
        url = _normalise_base_url(str(payload.get("base_url") or ""))
        if not url:
            continue
        payload["base_url"] = url
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        cluster_id = _cluster_arm_id_from_metadata(metadata)
        if cluster_id:
            seen_cluster_ids.add(cluster_id)
        results.append(payload)

    # Pull the legacy entry only when it represents a cluster the
    # per-cluster prefix didn't cover (covers the migration window for
    # an existing deployment that hasn't gone through a per-cluster
    # save yet).
    try:
        from api.services.state.singletons import load_singleton

        legacy = load_singleton(_PUBLIC_BASE_URL_KEY) or {}
    except Exception as exc:
        LOGGER.debug("openapi public base url legacy load failed: %s", exc)
        legacy = {}
    legacy_meta = legacy.get("metadata") if isinstance(legacy, dict) else None
    legacy_url = _normalise_base_url(str(legacy.get("base_url") or ""))
    if isinstance(legacy_meta, dict) and legacy_url:
        legacy_id = _cluster_arm_id_from_metadata(legacy_meta)
        if legacy_id and legacy_id not in seen_cluster_ids:
            legacy["base_url"] = legacy_url
            results.append(legacy)
    return results


def clear_openapi_public_base_url(
    *,
    cluster_arm_id: str = "",
    client: Any | None = None,
) -> bool:
    """Drop the cached public HTTPS endpoint.

    When ``cluster_arm_id`` is provided we clear the per-cluster entry
    and *also* the legacy entry if the legacy entry's metadata matches
    the same cluster (so disabling cluster A does not strand cluster
    B's "current display" in the legacy key).
    """
    redis_client = client or get_ops_redis_client(socket_timeout=1.5)
    ok = True

    if cluster_arm_id:
        per_cluster_key = _per_cluster_key(cluster_arm_id)
        try:
            from api.services.state.singletons import clear_singleton

            clear_singleton(per_cluster_key)
        except Exception as exc:
            LOGGER.debug(
                "openapi public base url durable delete failed for %s: %s",
                per_cluster_key,
                exc,
            )
        try:
            redis_client.delete(per_cluster_key)
        except Exception as exc:
            LOGGER.warning(
                "openapi public base url cache delete failed for %s: %s",
                per_cluster_key,
                exc,
            )
            ok = False
        # Legacy mirror only cleared when the displayed cluster matches.
        # Read both Redis (hot) and durable (cold) but suppress the
        # Redis re-cache side effect — otherwise a cold-path durable
        # read would resurrect a stale legacy mirror into Redis right
        # before we decide whether to clear it.
        legacy = _load_public_payload(
            redis_client,
            _PUBLIC_BASE_URL_KEY,
            recache_on_durable_hit=False,
        )
        legacy_meta = legacy.get("metadata") if isinstance(legacy, dict) else None
        if isinstance(legacy_meta, dict):
            legacy_id = _cluster_arm_id_from_metadata(legacy_meta)
            if legacy_id and legacy_id == _normalise_cluster_arm_id(cluster_arm_id):
                _clear_legacy(redis_client, expected_metadata=legacy_meta)
        return ok

    return _clear_legacy(redis_client)


def _clear_legacy(
    redis_client: Any,
    *,
    expected_metadata: dict[str, Any] | None = None,
) -> bool:
    """Delete the legacy single-key entry.

    When ``expected_metadata`` is provided we re-load the durable
    legacy row first and compare cluster identity. If a different
    cluster has just overwritten the mirror (e.g. a second operator
    disabled their own cluster between our load + clear) we leave it
    alone so we do not accidentally clobber the other cluster's
    "currently displayed" entry. This is the compare-and-set guard for
    the cross-operator race described in critique #8.
    """
    if expected_metadata is not None:
        try:
            from api.services.state.singletons import load_singleton

            durable_now = load_singleton(_PUBLIC_BASE_URL_KEY) or {}
        except Exception as exc:
            LOGGER.debug(
                "openapi public base url legacy CAS load failed: %s", exc
            )
            durable_now = {}
        live_meta = durable_now.get("metadata") if isinstance(durable_now, dict) else None
        if isinstance(live_meta, dict):
            live_id = _cluster_arm_id_from_metadata(live_meta)
            expected_id = _cluster_arm_id_from_metadata(expected_metadata)
            if live_id and expected_id and live_id != expected_id:
                LOGGER.info(
                    "openapi public base url legacy mirror moved to a different "
                    "cluster between load + clear; skipping clear to avoid "
                    "clobbering the other operator's entry."
                )
                return True
    try:
        from api.services.state.singletons import clear_singleton

        clear_singleton(_PUBLIC_BASE_URL_KEY)
    except Exception as exc:
        LOGGER.debug("openapi public base url durable delete failed: %s", exc)
    try:
        redis_client.delete(_PUBLIC_BASE_URL_KEY)
        return True
    except Exception as exc:
        LOGGER.warning("openapi public base url cache delete failed: %s", exc)
        return False


def get_public_tls_base_url(
    *,
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
) -> str:
    """Return the operator-configured public TLS endpoint, or empty string.

    Empty string means "no domain configured yet — use the legacy IP
    path". Resolution order:
    1. ``OPENAPI_PUBLIC_BASE_URL`` env — operator can still hard-pin a
       custom domain (e.g. behind App Gateway) by setting this on the
       api / worker sidecars.
    2. Ops Redis cache populated by `setup_openapi_public_https` — lets
       the dashboard flip to HTTPS as soon as the Celery task finishes,
       no Container App revision required.

    When the full cluster context is supplied the cache lookup is scoped
    to that cluster's per-cluster key. This is critical for the data
    plane: without it a cluster that enabled public HTTPS would leak its
    FQDN onto a *different* cluster's submit / spec / proxy calls (the
    legacy global key returns the most-recently-set cluster), silently
    misrouting BLAST submissions across clusters. Without context the
    legacy global key is read (backward compatible).
    """
    env_url = _normalise_base_url(os.environ.get(_PUBLIC_BASE_URL_ENV, ""))
    if env_url:
        return env_url
    cluster_arm_id = ""
    if subscription_id and resource_group and cluster_name:
        cluster_arm_id = (
            f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.ContainerService/managedClusters/{cluster_name}"
        )
    cached = get_openapi_public_base_url(cluster_arm_id=cluster_arm_id)
    return str(cached.get("base_url") or "")
