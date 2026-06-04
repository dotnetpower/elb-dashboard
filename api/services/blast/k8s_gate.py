"""Combine Gate A (submit Lease) + Gate B (cluster job-count) into one decision.

Responsibility: Provide the single admission entry point the submit task calls
when ``BLAST_COORD_BACKEND=k8s``. It acquires the per-namespace submit Lease
(Gate A), and *while still holding it* checks the cluster-wide active-submission
count against the ceiling (Gate B). On any deny it releases the Lease before
returning so a rejected submitter never parks a slot.
Edit boundaries: Orchestration only — it owns NO HTTP. Gate A primitives live in
``api.services.k8s.submit_lease``; the Gate B count lives in
``api.services.k8s.blast_status``; tunables live in
``api.services.blast.coordination``. The submit task owns the requeue / state-row
mapping; this helper only returns a verdict.
Key entry points: ``acquire_k8s_admission``, ``release_k8s_admission``,
``K8sAdmission``.
Risky contracts: Gate A and Gate B MUST share one scope (both per-namespace) or
the mutex and the ceiling disagree (design I1) — both are keyed on the same
``namespace`` here. A BUSY Lease (live other holder) is RETRYABLE
(``submit_slot_busy``); a Lease API error is an ERROR (``lease_api_error`` →
bounded ``_retry_or_fail``); a Gate B count failure is FAIL-CLOSED and RETRYABLE
(``capacity_count_error``) — never admit on an unknown count. When Gate B denies,
the Lease acquired microseconds earlier MUST be released here, not leaked to TTL.
Validation: ``uv run pytest -q api/tests/test_blast_k8s_gate.py``.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

from azure.core.credentials import TokenCredential

from api.services.blast.coordination import max_run_concurrency
from api.services.k8s.submit_lease import (
    SubmitLeaseApiError,
    SubmitLeaseHandle,
    k8s_acquire_submit_lease,
    k8s_release_submit_lease,
    new_holder_identity,
)

LOGGER = logging.getLogger(__name__)

# Deny reasons. ``error=True`` maps to the bounded retry path; the others map to
# a deadline-bounded requeue in the named phase.
REASON_SUBMIT_SLOT_BUSY = "submit_slot_busy"
REASON_CAPACITY_FULL = "capacity_full"
REASON_CAPACITY_COUNT_ERROR = "capacity_count_error"
REASON_LEASE_API_ERROR = "lease_api_error"

# Inline-wait poll interval for the split fan-out path (§7.1.4). The regular
# submit path re-enqueues via Celery instead of blocking, but split children are
# dispatched sequentially inside one parent task and cannot requeue mid-fan-out,
# so they wait inline with a bounded deadline.
_GATE_RETRY_INTERVAL_SECONDS = 5


class K8sGateWaitTimeout(RuntimeError):
    """Inline admission wait (split path) exceeded its deadline."""



@dataclass(frozen=True)
class K8sAdmission:
    """The verdict of the combined k8s admission gate.

    ``admitted`` carries the held ``lease`` (release it in ``finally`` via
    :func:`release_k8s_admission`). A denial carries a ``reason`` and the
    ``retryable`` / ``error`` routing the submit task uses to pick the phase.
    """

    admitted: bool
    lease: SubmitLeaseHandle | None = None
    reason: str | None = None
    retryable: bool = False
    error: bool = False
    active_count: int | None = None


def acquire_k8s_admission(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    namespace: str,
    job_id: str,
    source: str = "dashboard",
    check_capacity: bool = True,
) -> K8sAdmission:
    """Acquire Gate A then check Gate B; release Gate A on any deny.

    Returns an admitted verdict carrying the Lease handle, or a denied verdict
    whose routing flags tell the caller how to react:

    * ``submit_slot_busy`` — another live holder owns the Lease (retryable wait).
    * ``lease_api_error`` — the apiserver call failed (error → bounded retry).
    * ``capacity_full`` — ceiling reached (retryable wait); Lease released.
    * ``capacity_count_error`` — count read failed; fail-closed (retryable wait);
      Lease released.

    ``check_capacity=False`` runs **Gate A only** — acquire the submit Lease and
    admit without consulting the Gate B ceiling. This is for the split fan-out
    (critique re-review): a split is ONE logical submit whose shards are
    dispatched sequentially inside one parent task. Each shard's finalizer is
    lifecycle-long, so counting shards against the global ceiling would make any
    split with more shards than the ceiling deadlock on itself (the earlier
    shards permanently occupy the slots the later shards wait for) AND let one
    split starve unrelated submitters. The shards still serialise through Gate A
    (the submit Lease) against every other submitter; the split planner — not
    Gate B — bounds shard concurrency.
    """
    holder = new_holder_identity(source)
    try:
        lease = k8s_acquire_submit_lease(
            credential,
            subscription_id,
            resource_group,
            cluster_name,
            namespace=namespace,
            holder=holder,
        )
    except SubmitLeaseApiError as exc:
        LOGGER.info("k8s_gate lease api error job_id=%s: %s", job_id, exc)
        return K8sAdmission(
            admitted=False, reason=REASON_LEASE_API_ERROR, retryable=False, error=True
        )
    if lease is None:
        return K8sAdmission(
            admitted=False, reason=REASON_SUBMIT_SLOT_BUSY, retryable=True
        )

    if not check_capacity:
        # Gate A only — see the docstring. The split parent owns shard
        # concurrency; the ceiling cannot be enforced per-shard without a
        # self-deadlock.
        LOGGER.info(
            "k8s_gate admit (gate-a-only) job_id=%s holder=%s", job_id, holder
        )
        return K8sAdmission(admitted=True, lease=lease)

    # Gate B — counted while holding Gate A so the count cannot race a peer
    # admission. Import lazily to keep this module free of k8s SDK seams at
    # import time (tests patch the symbol on this module).
    from api.services.k8s.blast_status import k8s_count_active_blast_submissions

    try:
        active = k8s_count_active_blast_submissions(
            credential, subscription_id, resource_group, cluster_name, namespace
        )
    except Exception as exc:  # fail-closed: release Gate A, requeue
        LOGGER.info(
            "k8s_gate capacity count error job_id=%s: %s", job_id, type(exc).__name__
        )
        release_k8s_admission(
            credential, subscription_id, resource_group, cluster_name, lease
        )
        return K8sAdmission(
            admitted=False, reason=REASON_CAPACITY_COUNT_ERROR, retryable=True
        )

    ceiling = max_run_concurrency()
    if active >= ceiling:
        LOGGER.info(
            "k8s_gate capacity full job_id=%s active=%s ceiling=%s",
            job_id,
            active,
            ceiling,
        )
        release_k8s_admission(
            credential, subscription_id, resource_group, cluster_name, lease
        )
        return K8sAdmission(
            admitted=False,
            reason=REASON_CAPACITY_FULL,
            retryable=True,
            active_count=active,
        )

    LOGGER.info(
        "k8s_gate admit job_id=%s holder=%s active=%s ceiling=%s",
        job_id,
        holder,
        active,
        ceiling,
    )
    return K8sAdmission(admitted=True, lease=lease, active_count=active)


def release_k8s_admission(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    lease: SubmitLeaseHandle | None,
) -> None:
    """Best-effort conditional release of the submit Lease."""
    if lease is None:
        return
    k8s_release_submit_lease(
        credential, subscription_id, resource_group, cluster_name, lease
    )


def wait_for_k8s_admission(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    namespace: str,
    job_id: str,
    deadline_ts: float,
    source: str = "dashboard-split",
    check_capacity: bool = True,
    sleep: object = None,
) -> SubmitLeaseHandle:
    """Block until admitted, then return the held Lease (split fan-out only).

    Loops ``acquire_k8s_admission`` until it admits (returns the Lease handle the
    caller MUST release in ``finally``), the ``deadline_ts`` passes
    (:class:`K8sGateWaitTimeout`), or a genuine Lease API error occurs
    (:class:`SubmitLeaseApiError`). A retryable deny (Lease busy / ceiling full)
    sleeps ``_GATE_RETRY_INTERVAL_SECONDS`` and retries. ``sleep`` is injectable
    for tests; it defaults to ``time.sleep``. ``check_capacity`` is forwarded to
    :func:`acquire_k8s_admission` (the split path passes ``False`` for Gate-A-only
    serialisation — see that function's docstring).
    """
    do_sleep = sleep if callable(sleep) else time.sleep
    while True:
        # Pre-check the deadline BEFORE acquiring so an already-expired wait
        # raises immediately instead of taking the submit Lease one more time
        # (a wasted CAS write that briefly blocks live submitters).
        if time.time() >= deadline_ts:
            raise K8sGateWaitTimeout(
                f"split child {job_id} gate wait exceeded deadline before acquire"
            )
        admission = acquire_k8s_admission(
            credential,
            subscription_id,
            resource_group,
            cluster_name,
            namespace=namespace,
            job_id=job_id,
            source=source,
            check_capacity=check_capacity,
        )
        if admission.admitted and admission.lease is not None:
            return admission.lease
        if admission.error:
            raise SubmitLeaseApiError(
                f"submit lease unavailable for split child {job_id}: {admission.reason}"
            )
        remaining = deadline_ts - time.time()
        if remaining <= 0:
            raise K8sGateWaitTimeout(
                f"split child {job_id} gate wait exceeded deadline: {admission.reason}"
            )
        # Jittered sleep so a thundering herd of split children (or sibling
        # submitters) released at the same instant don't re-poll the apiserver
        # in lock-step (critique L25). +/-20% around the base interval, clamped
        # to whatever budget remains so we never overshoot the deadline.
        nap = _GATE_RETRY_INTERVAL_SECONDS * random.uniform(0.8, 1.2)  # noqa: S311 - jitter, not crypto
        do_sleep(min(nap, remaining))

