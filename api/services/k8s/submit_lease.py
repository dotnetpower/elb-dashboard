"""Gate A — the BLAST submit mutex backed by a Kubernetes Lease (coordination.k8s.io).

Responsibility: Provide ``acquire`` / ``release`` primitives for the per-namespace
submit Lease that serialises ``elastic-blast submit`` across BOTH submit paths
(dashboard Celery + on-AKS ``elb-openapi``) by keeping the coordination truth in
the cluster's etcd rather than the dashboard's ephemeral Redis. This is the FIRST
mutating Kubernetes call path in this repo — every existing ``api/services/k8s``
helper is read-only.
Edit boundaries: Lease HTTP primitives only. Config/tunables live in
``api.services.blast.coordination``; the cluster job count (Gate B) lives in
``api.services.k8s.blast_status``; the admission decision that combines both gates
lives in ``api.services.blast.k8s_gate``. Reuse ``_get_k8s_session`` (admin) like
``manifests.py``; do not reintroduce Azure Run Command.
Key entry points: ``new_holder_identity``, ``k8s_acquire_submit_lease``,
``k8s_release_submit_lease``, ``SubmitLeaseHandle``, ``SubmitLeaseApiError``,
``submit_lease_name``.
Risky contracts: BUSY (a live holder conflict / 409 CAS loss → returns ``None``)
MUST be distinguished from an API error (apiserver unreachable / 5xx → raises
``SubmitLeaseApiError``); mapping an outage to BUSY would silently requeue forever
(design §4.2). ``holderIdentity`` MUST be a globally-unique per-acquisition token
(``<source>-<uuid>``) or the same-holder renew branch lets two distinct submits run
concurrently (design §4.1). Release is CONDITIONAL — it only clears the Lease when
it still holds our identity, so a TTL-reclaimed Lease owned by a newer holder is
never clobbered (design §4.3).
Validation: ``uv run pytest -q api/tests/test_blast_submit_lease.py``.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.blast.coordination import (
    lease_clock_skew_seconds,
    submit_lease_ttl_seconds,
)

LOGGER = logging.getLogger(__name__)

_LEASE_API_GROUP = "coordination.k8s.io/v1"
_SAFE_K8S_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_LEASE_NAME_PREFIX = "elb-blast-submit"


class SubmitLeaseApiError(RuntimeError):
    """A genuine Kubernetes API failure (NOT a holder conflict).

    Raised so the caller maps it to the bounded ``_retry_or_fail`` path and the
    outage surfaces as a visible terminal state, instead of being silently
    requeued every 30s as if it were lock contention.
    """


@dataclass(frozen=True)
class SubmitLeaseHandle:
    """An acquired Lease — carries everything ``k8s_release_submit_lease`` needs."""

    name: str
    namespace: str
    holder: str


def submit_lease_name(namespace: str) -> str:
    """Map a namespace to its Lease name, 1:1 with today's ``submit_lock_key``."""
    ns = (namespace or "default").strip() or "default"
    return f"{_LEASE_NAME_PREFIX}-{ns}"


def new_holder_identity(source: str) -> str:
    """Return a globally-unique per-acquisition holder token.

    ``source`` is a short origin tag (``dashboard`` / ``openapi``); the uuid
    suffix guarantees two distinct submits never collide on the same identity,
    which the same-holder renew branch would otherwise treat as "my own Lease,
    safe to proceed" → silent concurrent submit (design §4.1).
    """
    tag = re.sub(r"[^a-z0-9-]", "", (source or "submit").lower()) or "submit"
    return f"{tag}-{uuid.uuid4().hex}"


def _now() -> datetime:
    return datetime.now(UTC)


def _micro_time(value: datetime) -> str:
    """Render a Kubernetes ``MicroTime`` (RFC3339 with microseconds, ``Z``)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _parse_k8s_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _lease_body(name: str, namespace: str, holder: str, ttl: int, now: datetime) -> dict[str, Any]:
    stamp = _micro_time(now)
    return {
        "apiVersion": _LEASE_API_GROUP,
        "kind": "Lease",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "holderIdentity": holder,
            "leaseDurationSeconds": ttl,
            "acquireTime": stamp,
            "renewTime": stamp,
        },
    }


def _is_expired(spec: dict[str, Any], now: datetime, skew: int) -> bool:
    renew_raw = spec.get("renewTime")
    renew = _parse_k8s_time(renew_raw)
    if renew is None:
        # Distinguish "no renewTime field at all" (Lease never stamped → safe to
        # treat as available) from "renewTime present but unparseable" (e.g. the
        # sibling repo wrote a format we don't recognise). The latter is
        # FAIL-CLOSED: we cannot prove the Lease is dead, so we must NOT take it
        # over, or two paths submit concurrently (critique M16).
        if isinstance(renew_raw, str) and renew_raw.strip():
            LOGGER.info("submit lease renewTime unparseable; treating as held: %r", renew_raw)
            return False
        return True
    try:
        ttl = int(spec.get("leaseDurationSeconds") or submit_lease_ttl_seconds())
    except (TypeError, ValueError):
        ttl = submit_lease_ttl_seconds()
    elapsed = (now - renew).total_seconds()
    return elapsed > (ttl + skew)


def k8s_acquire_submit_lease(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    namespace: str,
    holder: str,
    ttl_seconds: int | None = None,
) -> SubmitLeaseHandle | None:
    """Try to acquire the submit Lease for ``namespace``.

    Returns a :class:`SubmitLeaseHandle` on success, ``None`` if the Lease is
    held by a live/non-expired other holder (BUSY → caller requeues), and raises
    :class:`SubmitLeaseApiError` on any genuine API failure (caller maps to the
    bounded retry path). Uses ``resourceVersion`` CAS so exactly one of two
    racing acquirers wins (the loser gets a 409 → BUSY).
    """
    from api.services.k8s.credentials import _get_k8s_session

    if not _SAFE_K8S_NAME_RE.match(namespace or ""):
        raise SubmitLeaseApiError(f"invalid namespace for submit lease: {namespace!r}")
    name = submit_lease_name(namespace)
    ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else submit_lease_ttl_seconds()
    skew = lease_clock_skew_seconds()

    session, server = _get_k8s_session(
        credential, subscription_id, resource_group, cluster_name, admin=True
    )
    base = f"{server}/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases"
    try:
        get_resp = session.get(f"{base}/{name}", timeout=10)
        if get_resp.status_code == 404:
            return _create_lease(session, base, name, namespace, holder, ttl)
        if get_resp.status_code in (401, 403):
            # admin=True uses a client-cert kubeconfig that bypasses Kubernetes
            # RBAC, so a 401/403 here is almost always a stale/rotated admin
            # credential or a NetworkPolicy blocking the apiserver — NOT a role
            # the dashboard can self-heal by retrying. Surface it loudly so the
            # operator fixes the credential rather than letting it requeue
            # forever as if it were lock contention (critique H7).
            raise SubmitLeaseApiError(
                f"lease GET forbidden ({get_resp.status_code}): admin kubeconfig "
                "rejected by apiserver — check cluster admin credential rotation "
                "or apiserver network reachability"
            )
        if get_resp.status_code != 200:
            raise SubmitLeaseApiError(
                f"lease GET failed: {get_resp.status_code} {get_resp.text[:200]}"
            )

        lease = get_resp.json()
        spec = lease.get("spec") or {}
        current_holder = str(spec.get("holderIdentity") or "")
        resource_version = (lease.get("metadata") or {}).get("resourceVersion")
        now = _now()

        if current_holder == holder:
            # Same-job retry → renew our own Lease (idempotent).
            return _replace_lease(session, base, name, namespace, holder, ttl, resource_version)
        if not current_holder or _is_expired(spec, now, skew):
            # Available or expired → CAS takeover; a racing winner makes us 409 → BUSY.
            return _replace_lease(session, base, name, namespace, holder, ttl, resource_version)
        return None  # live holder, not us → BUSY
    except SubmitLeaseApiError:
        raise
    except Exception as exc:  # transport / JSON / unexpected → treat as API error
        raise SubmitLeaseApiError(f"lease acquire error: {type(exc).__name__}: {exc}") from exc
    finally:
        session.close()


def _create_lease(
    session: Any, base: str, name: str, namespace: str, holder: str, ttl: int
) -> SubmitLeaseHandle | None:
    body = _lease_body(name, namespace, holder, ttl, _now())
    resp = session.post(base, json=body, timeout=10)
    if resp.status_code in (200, 201):
        return SubmitLeaseHandle(name=name, namespace=namespace, holder=holder)
    if resp.status_code == 409:
        return None  # someone created it first → BUSY
    raise SubmitLeaseApiError(f"lease CREATE failed: {resp.status_code} {resp.text[:200]}")


def _replace_lease(
    session: Any,
    base: str,
    name: str,
    namespace: str,
    holder: str,
    ttl: int,
    resource_version: Any,
) -> SubmitLeaseHandle | None:
    body = _lease_body(name, namespace, holder, ttl, _now())
    if resource_version:
        body["metadata"]["resourceVersion"] = str(resource_version)
    resp = session.put(f"{base}/{name}", json=body, timeout=10)
    if resp.status_code in (200, 201):
        return SubmitLeaseHandle(name=name, namespace=namespace, holder=holder)
    if resp.status_code == 409:
        return None  # lost the CAS race → BUSY
    raise SubmitLeaseApiError(f"lease PUT failed: {resp.status_code} {resp.text[:200]}")


def k8s_release_submit_lease(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    handle: SubmitLeaseHandle,
) -> None:
    """Conditionally release a Lease — only if we still hold it.

    A submit that overran the TTL may have been reclaimed by a second holder; an
    unconditional clear would erase that newer holder mid-submit → concurrent
    submit. So this GETs, verifies ``holderIdentity == handle.holder``, and only
    then CAS-clears it. Best-effort: a crashed holder is reclaimed by TTL anyway.
    """
    from api.services.k8s.credentials import _get_k8s_session

    session, server = _get_k8s_session(
        credential, subscription_id, resource_group, cluster_name, admin=True
    )
    base = f"{server}/apis/coordination.k8s.io/v1/namespaces/{handle.namespace}/leases"
    try:
        for _attempt in range(2):
            get_resp = session.get(f"{base}/{handle.name}", timeout=10)
            if get_resp.status_code == 404:
                return
            if get_resp.status_code != 200:
                LOGGER.info(
                    "submit lease release skipped: GET %s", get_resp.status_code
                )
                return
            lease = get_resp.json()
            spec = lease.get("spec") or {}
            if str(spec.get("holderIdentity") or "") != handle.holder:
                return  # newer holder took over — do NOT clobber
            metadata = lease.get("metadata") or {}
            body = {
                "apiVersion": _LEASE_API_GROUP,
                "kind": "Lease",
                "metadata": {
                    "name": handle.name,
                    "namespace": handle.namespace,
                    "resourceVersion": metadata.get("resourceVersion"),
                },
                "spec": {
                    "holderIdentity": "",
                    "leaseDurationSeconds": spec.get("leaseDurationSeconds"),
                    "acquireTime": spec.get("acquireTime"),
                    "renewTime": spec.get("renewTime"),
                },
            }
            put_resp = session.put(f"{base}/{handle.name}", json=body, timeout=10)
            if put_resp.status_code in (200, 201):
                return
            if put_resp.status_code == 409:
                continue  # someone modified it; re-GET and re-check holder once
            LOGGER.info("submit lease release skipped: PUT %s", put_resp.status_code)
            return
    except Exception as exc:
        LOGGER.info("submit lease release skipped: %s", type(exc).__name__)
    finally:
        session.close()
