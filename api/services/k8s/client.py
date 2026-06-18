"""Direct Kubernetes API session helpers for AKS clusters.

Responsibility: Direct Kubernetes API session helpers for AKS clusters
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_K8sCredentialMaterial`, `reset_k8s_credential_cache`,
`_k8s_credential_cache_ttl`, `reset_k8s_session_pool`
Risky contracts: Use direct Kubernetes API helpers; do not reintroduce Azure Run Command.
The cluster CA is trusted via an in-memory SSLContext (`_build_k8s_https_adapter`),
never a temp file, so pool eviction cannot delete a CA bundle a borrowed session
still references.
Validation: `uv run pytest -q api/tests/test_k8s_list_events.py api/tests/test_k8s_session_pool.py`.
"""

from __future__ import annotations

import atexit
import base64
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import yaml  # type: ignore[import-untyped]
from azure.core.credentials import TokenCredential

from api.services.azure_clients import aks_client as aks_client
from api.services.k8s.cluster_breaker import (
    cluster_breaker_check,
    cluster_breaker_key,
    cluster_breaker_record_failure,
    cluster_breaker_record_success,
    reset_cluster_breaker,
)

LOGGER = logging.getLogger(__name__)

_AKS_SERVER_APP_ID = "6dae42f8-4368-4678-94ff-3960e28e3630"
_K8S_CREDENTIAL_CACHE_TTL_SECONDS = 300.0
_K8S_SESSION_POOL_TTL_SECONDS = 300.0
_K8S_SESSION_POOL_MAX_ENTRIES = 32
# Refresh a Bearer-auth session this many seconds before the AAD token
# actually expires so an in-flight request never sees a 401.
_K8S_SESSION_TOKEN_SAFETY_MARGIN_SECONDS = 60.0


@dataclass(frozen=True)
class _K8sCredentialMaterial:
    server: str
    ca_data: bytes | None
    client_cert: bytes | None
    client_key: bytes | None
    expires_at: float


_K8S_CREDENTIAL_CACHE: dict[tuple[str, str, str, bool], _K8sCredentialMaterial] = {}
_K8S_CREDENTIAL_CACHE_LOCK = threading.Lock()


@dataclass
class _K8sSessionEntry:
    session: Any
    server: str
    temp_files: list[str] = field(default_factory=list)
    expires_at: float = 0.0


_K8S_SESSION_POOL: dict[tuple[str, str, str, bool], _K8sSessionEntry] = {}
_K8S_SESSION_POOL_LOCK = threading.Lock()


def reset_k8s_credential_cache() -> None:
    """Clear cached AKS kubeconfig material. Test-only."""
    with _K8S_CREDENTIAL_CACHE_LOCK:
        _K8S_CREDENTIAL_CACHE.clear()
    reset_cluster_breaker()


def _k8s_credential_cache_ttl() -> float:
    raw = os.environ.get("K8S_CREDENTIAL_CACHE_TTL_SECONDS", "")
    if raw:
        try:
            return max(0.0, min(float(raw), 3600.0))
        except ValueError:
            return _K8S_CREDENTIAL_CACHE_TTL_SECONDS
    return _K8S_CREDENTIAL_CACHE_TTL_SECONDS


def _k8s_session_pool_ttl() -> float:
    raw = os.environ.get("K8S_SESSION_POOL_TTL_SECONDS", "")
    if raw:
        try:
            return max(0.0, min(float(raw), 3600.0))
        except ValueError:
            return _K8S_SESSION_POOL_TTL_SECONDS
    return _K8S_SESSION_POOL_TTL_SECONDS


def _k8s_session_pool_max_entries() -> int:
    raw = os.environ.get("K8S_SESSION_POOL_MAX_ENTRIES", "")
    if raw:
        try:
            return max(1, min(int(raw), 4096))
        except ValueError:
            return _K8S_SESSION_POOL_MAX_ENTRIES
    return _K8S_SESSION_POOL_MAX_ENTRIES


_K8S_SESSION_HTTP_POOL_SIZE = 32


def _k8s_session_http_pool_size() -> int:
    """Resolve the urllib3 HTTPAdapter pool size for the pooled K8s session.

    The default ``urllib3.HTTPAdapter(pool_maxsize=10)`` saturated as soon
    as ``k8s_warmup_status``'s 6-way ThreadPoolExecutor lined up next to
    other monitor polls on the same session; each over-cap GET then paid a
    fresh TLS handshake. 32 connections per (cluster, admin) is enough
    headroom for the documented monitor fan-outs plus the per-job log
    fetches, while still bounded so a misbehaving caller cannot exhaust
    the worker's socket table.
    """
    raw = os.environ.get("K8S_SESSION_HTTP_POOL_SIZE", "")
    if raw:
        try:
            return max(1, min(int(raw), 256))
        except ValueError:
            return _K8S_SESSION_HTTP_POOL_SIZE
    return _K8S_SESSION_HTTP_POOL_SIZE


# Single, fast retry on transient connect / DNS failures. The Container
# Apps environment's coredns sidecar occasionally returns NXDOMAIN for a
# few hundred ms during overlay refresh; without this retry each hiccup
# bubbled up as a `requests.exceptions.ConnectionError(NameResolutionError)`
# and recorded an App Insights exception even though the very next poll
# succeeded. The same single retry now also covers connection-level read
# aborts (`RemoteDisconnected`/`ProtocolError`) on a reused keepalive
# socket — the AKS API server silently drops idle pooled connections, and
# the next GET on that dead socket raised
# `requests.exceptions.ConnectionError(ProtocolError('Connection aborted.',
# RemoteDisconnected(...)))`, the single noisiest App Insights exception
# (warmup pod-log fan-out). urllib3 classifies a `RemoteDisconnected` as a
# READ error, so `read=0` made it terminal; retrying it once transparently
# re-establishes the socket and the GET succeeds. This is safe because the
# retry is gated to idempotent methods only (`allowed_methods` =
# GET/HEAD/OPTIONS), so a POST/DELETE k8s mutation is still never replayed.
# HTTP STATUS codes remain NOT retried — a successful TCP connect that
# returns 5xx is the API server's authoritative answer, not a transport
# blip (`status=0`, `status_forcelist=()`).
_K8S_SESSION_RETRY_TOTAL = 1
_K8S_SESSION_RETRY_BACKOFF = 0.5


def _k8s_session_retry_total() -> int:
    raw = os.environ.get("K8S_SESSION_RETRY_TOTAL", "")
    if raw:
        try:
            return max(0, min(int(raw), 5))
        except ValueError:
            return _K8S_SESSION_RETRY_TOTAL
    return _K8S_SESSION_RETRY_TOTAL


def _k8s_session_retry_backoff() -> float:
    raw = os.environ.get("K8S_SESSION_RETRY_BACKOFF", "")
    if raw:
        try:
            return max(0.0, min(float(raw), 5.0))
        except ValueError:
            return _K8S_SESSION_RETRY_BACKOFF
    return _K8S_SESSION_RETRY_BACKOFF


def _build_k8s_retry() -> Any:
    """Build the urllib3 Retry object used by the pooled K8s session.

    Lazy-imported so unit tests that only exercise the caching machinery
    do not require urllib3 to be present, and so this module stays
    importable in stripped-down test environments.
    """
    from urllib3.util.retry import Retry

    return Retry(
        total=_k8s_session_retry_total(),
        connect=_k8s_session_retry_total(),
        # Retry a connection-level read abort (RemoteDisconnected / dropped
        # keepalive socket) once. `total` still bounds the overall retry
        # budget, and `allowed_methods` below restricts replay to idempotent
        # GET/HEAD/OPTIONS, so this never re-sends a mutating request.
        read=_k8s_session_retry_total(),
        status=0,
        redirect=0,
        other=0,
        backoff_factor=_k8s_session_retry_backoff(),
        # Only retry transport-level failures (DNS / TCP reset / dropped
        # keepalive) — never a successful response with a 4xx/5xx body,
        # because those are the API server's authoritative answer.
        status_forcelist=(),
        allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]),
        raise_on_status=False,
        raise_on_redirect=False,
    )


def _build_k8s_https_adapter(ca_data: bytes, pool_size: int) -> Any:
    """Build an HTTPS adapter that trusts the AKS cluster CA in-memory.

    Why this exists: the previous implementation wrote ``ca_data`` to a
    ``NamedTemporaryFile`` and set ``session.verify`` to that path. Pooled
    sessions outlive a single request, and pool eviction / atexit cleanup
    unlinks those temp files at TTL expiry — so a request still holding a
    borrowed session could read a ``session.verify`` path that had already
    been deleted, raising ``OSError: Could not find a suitable TLS CA
    certificate bundle`` (issue #47). Loading the CA into an in-memory
    ``ssl.SSLContext`` removes the on-disk CA bundle entirely, so there is no
    file for eviction to delete out from under an in-flight GET.

    The context still performs full verification (hostname check + required
    cert) against the cluster CA; system roots remain available so client-cert
    (admin) sessions and any future proxy paths keep working.
    """
    import ssl

    import requests as _requests

    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=ca_data.decode("utf-8"))

    class _CADataAdapter(_requests.adapters.HTTPAdapter):
        """HTTPAdapter that injects the in-memory CA SSLContext into the pool."""

        def init_poolmanager(self, *args: Any, **kwargs: Any) -> Any:
            kwargs["ssl_context"] = context
            return super().init_poolmanager(*args, **kwargs)

        def proxy_manager_for(self, *args: Any, **kwargs: Any) -> Any:
            kwargs["ssl_context"] = context
            return super().proxy_manager_for(*args, **kwargs)

    return _CADataAdapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
        pool_block=False,
        max_retries=_build_k8s_retry(),
    )


def _install_cluster_breaker(session: Any, breaker_key: tuple[str, str, str]) -> None:
    """Wrap ``session.request`` so the per-cluster breaker sees every call.
    All of ``session.get``/``post``/``delete`` funnel through ``Session.request``
    (which the OpenTelemetry requests instrumentor has patched at class level).
    Shadowing the *instance* ``request`` with this guard lets the breaker (a)
    reject calls without touching the network — and therefore without recording
    an App Insights exception — while it is open, and (b) trip on a real connect
    failure and close again on the first successful answer (any HTTP status,
    since a 4xx/5xx body still proves the API server is reachable).
    """

    import requests as _requests

    real_request = session.request

    def guarded_request(method: str, url: str, **kwargs: Any) -> Any:
        cluster_breaker_check(breaker_key)
        try:
            response = real_request(method, url, **kwargs)
        except _requests.exceptions.ConnectionError:
            # ConnectTimeout subclasses ConnectionError, so this also covers
            # connect-time timeouts. ReadTimeout (a connected-but-slow answer)
            # is intentionally NOT counted — the cluster is reachable.
            cluster_breaker_record_failure(breaker_key)
            raise
        cluster_breaker_record_success(breaker_key)
        return response

    session.request = guarded_request  # type: ignore[method-assign]


def _get_k8s_credential_material(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    admin: bool,
) -> _K8sCredentialMaterial:
    cache_key = (subscription_id, resource_group, cluster_name, admin)
    now = time.monotonic()
    with _K8S_CREDENTIAL_CACHE_LOCK:
        cached = _K8S_CREDENTIAL_CACHE.get(cache_key)
    if cached is not None and cached.expires_at > now:
        return cached

    breaker_key = cluster_breaker_key(subscription_id, resource_group, cluster_name)
    cluster_breaker_check(breaker_key)
    client = aks_client(credential, subscription_id)
    try:
        if admin:
            creds = client.managed_clusters.list_cluster_admin_credentials(
                resource_group,
                cluster_name,
            )
        else:
            creds = client.managed_clusters.list_cluster_user_credentials(
                resource_group,
                cluster_name,
            )
    except Exception:
        # A deleted / stopped cluster makes the ARM kubeconfig fetch throw on
        # every call. Record it so the breaker trips and short-circuits the
        # next poll instead of re-issuing the failing ARM request. Re-raise so
        # the existing graceful handlers degrade to empty/None unchanged.
        cluster_breaker_record_failure(breaker_key)
        raise
    kubeconfig_bytes = creds.kubeconfigs[0].value
    kubeconfig = yaml.safe_load(bytes(kubeconfig_bytes))

    cluster_info = kubeconfig["clusters"][0]["cluster"]
    user_info = kubeconfig["users"][0]["user"]
    ca_data = cluster_info.get("certificate-authority-data", "")
    client_cert = user_info.get("client-certificate-data")
    client_key = user_info.get("client-key-data")

    material = _K8sCredentialMaterial(
        server=cluster_info["server"],
        ca_data=base64.b64decode(ca_data) if ca_data else None,
        client_cert=base64.b64decode(client_cert) if client_cert else None,
        client_key=base64.b64decode(client_key) if client_key else None,
        expires_at=now + _k8s_credential_cache_ttl(),
    )
    if material.expires_at > now:
        with _K8S_CREDENTIAL_CACHE_LOCK:
            _K8S_CREDENTIAL_CACHE[cache_key] = material
    return material


def _get_k8s_session(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    admin: bool = False,
) -> tuple[Any, str]:
    """Return ``(requests.Session, server_url)`` for direct K8s API calls.

    The session is pooled per ``(subscription_id, resource_group, cluster_name, admin)``
    with a 5-minute TTL — matching the kubeconfig credential cache — so
    repeated dashboard polls reuse the same connection pool (HTTP keep-alive)
    and skip the per-call temp-file write that used to happen every time.

    ``session.close()`` is overridden to a no-op on pooled sessions; the
    pool itself owns the real lifecycle and unlinks the on-disk CA / client
    cert / client key files when an entry expires or the process exits.
    Existing call sites that use ``try: ... finally: session.close()`` keep
    working unchanged — close just becomes a release back to the pool.
    """

    import requests as _requests

    pool_key = (subscription_id, resource_group, cluster_name, admin)
    breaker_key = cluster_breaker_key(subscription_id, resource_group, cluster_name)
    now = time.monotonic()

    # Skip the pooled fast path too when the breaker is open: a cluster that
    # just went down may still have a cached session whose every GET would
    # connect-fail and record an App Insights exception. Raising here short-
    # circuits before any network call.
    cluster_breaker_check(breaker_key)

    # Fast path — fresh pooled entry.
    with _K8S_SESSION_POOL_LOCK:
        entry = _K8S_SESSION_POOL.get(pool_key)
        if entry is not None and entry.expires_at > now:
            return entry.session, entry.server

    # Slow path — build a new session outside the lock so we don't block
    # other callers on the ARM kubeconfig fetch + temp-file IO.
    material = _get_k8s_credential_material(
        credential,
        subscription_id,
        resource_group,
        cluster_name,
        admin=admin,
    )

    session = _requests.Session()
    # Bump urllib3 pool sizes well above the default 10 so concurrent
    # fan-outs (e.g. ``k8s_warmup_status``'s 6-way ThreadPoolExecutor +
    # ``_warmup_pods_and_logs`` per-pod log fetches) do not block on the
    # underlying connection pool. With multiple dashboard pollers in
    # flight the default pool was saturated within seconds, forcing a
    # full TLS handshake per GET. ``pool_block=False`` keeps the
    # behaviour of "allocate over-the-cap connections rather than wait"
    # so a brief burst does not stall behind the pool — at the cost of
    # extra short-lived sockets which urllib3 then closes.
    _k8s_pool_size = _k8s_session_http_pool_size()
    temp_files: list[str] = []

    def write_secret_file(suffix: str, content: bytes) -> str:
        handle = tempfile.NamedTemporaryFile(
            prefix="elb-k8s-", suffix=suffix, delete=False
        )
        try:
            handle.write(content)
            handle.flush()
        finally:
            handle.close()
        os.chmod(handle.name, 0o600)
        temp_files.append(handle.name)
        return handle.name

    # Default to the configured pool TTL, then clamp tighter as we discover
    # auth-material lifetimes that are shorter than it.
    pool_ttl = _k8s_session_pool_ttl()
    entry_expires_at = now + pool_ttl
    # Never outlive the kubeconfig material the session was built from —
    # otherwise a session could survive an AKS cert rotation that the
    # credential cache already noticed.
    entry_expires_at = min(entry_expires_at, material.expires_at)
    try:
        # Trust the cluster CA via an in-memory SSLContext (issue #47) rather
        # than a temp file on disk. Pooled sessions outlive a single request,
        # so a CA file unlinked by pool eviction could be read by an in-flight
        # GET -> OSError. The HTTPS adapter below carries the CA in memory, so
        # ``session.verify`` stays True and there is no file to evict.
        if material.ca_data:
            https_adapter = _build_k8s_https_adapter(material.ca_data, _k8s_pool_size)
        else:
            https_adapter = _requests.adapters.HTTPAdapter(
                pool_connections=_k8s_pool_size,
                pool_maxsize=_k8s_pool_size,
                pool_block=False,
                max_retries=_build_k8s_retry(),
            )
        http_adapter = _requests.adapters.HTTPAdapter(
            pool_connections=_k8s_pool_size,
            pool_maxsize=_k8s_pool_size,
            pool_block=False,
            max_retries=_build_k8s_retry(),
        )
        session.mount("https://", https_adapter)
        session.mount("http://", http_adapter)
        session.verify = True
        _install_cluster_breaker(session, breaker_key)

        if material.client_cert and material.client_key:
            cert_path = write_secret_file(".crt", material.client_cert)
            key_path = write_secret_file(".key", material.client_key)
            session.cert = (cert_path, key_path)
        else:
            token = credential.get_token(f"{_AKS_SERVER_APP_ID}/.default")
            session.headers["Authorization"] = f"Bearer {token.token}"
            # Token-authed sessions must be retired before the AAD token
            # expires; otherwise the next pooled GET returns 401.
            token_expires_monotonic = now + max(
                0.0,
                float(token.expires_on) - time.time()
                - _K8S_SESSION_TOKEN_SAFETY_MARGIN_SECONDS,
            )
            entry_expires_at = min(entry_expires_at, token_expires_monotonic)
    except Exception:
        _evict_temp_files(temp_files)
        try:
            session.close()
        except Exception:  # noqa: S110 - session close failures are non-actionable here
            pass
        raise

    # session.close() becomes a no-op for pooled sessions. The pool owns
    # the real close + temp-file cleanup at TTL expiry / atexit.
    session.close = _noop_close  # type: ignore[method-assign]

    new_entry = _K8sSessionEntry(
        session=session,
        server=material.server,
        temp_files=list(temp_files),
        expires_at=entry_expires_at,
    )

    # If the effective TTL collapsed to non-positive (e.g. credential
    # material already expired or token is about to expire), do not pool
    # — hand the caller a one-shot session and let session.close() in the
    # finally block retire it. The override above made close() a no-op
    # for pool members, so undo it for this throwaway session.
    if entry_expires_at <= now:
        session.close = _make_throwaway_close(session, temp_files)  # type: ignore[method-assign]
        return session, material.server

    # Insert into the pool; if a concurrent caller raced and beat us to it,
    # discard our session (close + unlink files) and reuse theirs so callers
    # don't end up with two competing entries for the same cluster.
    #
    # Lock is released BEFORE any `_retire_entry` call — retirement closes
    # the underlying urllib3 connection pool (network IO) and unlinks temp
    # files (filesystem IO). Holding the global pool lock through that
    # would stall every other `_get_k8s_session` caller across every
    # cluster on every cold-miss race or cap eviction.
    to_retire: list[_K8sSessionEntry] = []
    reused_entry: _K8sSessionEntry | None = None
    with _K8S_SESSION_POOL_LOCK:
        existing = _K8S_SESSION_POOL.get(pool_key)
        if existing is not None and existing.expires_at > now:
            # Another caller won the race — keep theirs, retire ours.
            to_retire.append(new_entry)
            reused_entry = existing
        else:
            if existing is not None:
                to_retire.append(existing)
            _K8S_SESSION_POOL[pool_key] = new_entry
            # Cap pool size — evict the entry closest to expiry first so
            # we keep the hottest sessions alive.
            if len(_K8S_SESSION_POOL) > _k8s_session_pool_max_entries():
                victim_key = min(
                    _K8S_SESSION_POOL.items(),
                    key=lambda kv: kv[1].expires_at,
                )[0]
                if victim_key != pool_key:
                    to_retire.append(_K8S_SESSION_POOL.pop(victim_key))

    # IO outside the lock — see comment above.
    for entry in to_retire:
        _retire_entry(entry)
    if reused_entry is not None:
        return reused_entry.session, reused_entry.server

    return new_entry.session, new_entry.server


def _make_throwaway_close(session: Any, temp_files: list[str]):
    """Return a close() that does a real teardown + unlinks the temp files.

    Used when an entry's effective TTL collapsed to non-positive so we hand
    out a non-pooled session — the caller's ``finally: session.close()``
    must clean up after itself.
    """

    import requests as _requests

    def _close() -> None:
        try:
            _requests.Session.close(session)
        except Exception:  # noqa: S110 - close failures are non-actionable
            pass
        _evict_temp_files(temp_files)

    return _close


def _noop_close() -> None:
    """Replacement for ``Session.close`` on pooled sessions — pool owns lifecycle."""


def _evict_temp_files(paths: list[str]) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def _retire_entry(entry: _K8sSessionEntry) -> None:
    """Close the underlying session for real and unlink its temp files."""
    # Restore the real close so the underlying connection pool actually
    # tears down — our pooled override is a no-op by design.
    try:
        import requests as _requests

        _requests.Session.close(entry.session)
    except Exception:  # noqa: S110 - close failures are non-actionable
        pass
    _evict_temp_files(entry.temp_files)


def reset_k8s_session_pool() -> None:
    """Drop all pooled K8s sessions. Test-only — production code never needs this."""
    reset_cluster_breaker()
    with _K8S_SESSION_POOL_LOCK:
        entries = list(_K8S_SESSION_POOL.values())
        _K8S_SESSION_POOL.clear()
    # Retire each entry outside the lock; isolate failures so one bad
    # entry can't strand the rest.
    for entry in entries:
        try:
            _retire_entry(entry)
        except Exception:  # noqa: S110 - retire failures must not leak siblings
            pass


def _atexit_drain_pool() -> None:
    """Best-effort pool drain at interpreter shutdown.

    Uses a non-blocking lock acquire because daemon threads holding the
    pool lock are forcibly terminated during shutdown without releasing
    it — a blocking acquire here would deadlock the atexit chain.
    """
    if not _K8S_SESSION_POOL_LOCK.acquire(blocking=False):
        return
    try:
        entries = list(_K8S_SESSION_POOL.values())
        _K8S_SESSION_POOL.clear()
    finally:
        _K8S_SESSION_POOL_LOCK.release()
    for entry in entries:
        try:
            _retire_entry(entry)
        except Exception:  # noqa: S110 - best-effort at interpreter exit
            pass


atexit.register(_atexit_drain_pool)
