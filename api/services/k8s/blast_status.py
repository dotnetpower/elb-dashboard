"""ElasticBLAST search job status and cancellation via the direct Kubernetes API.

Responsibility: Report ElasticBLAST `app=blast` job status and cancel submissions
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code. Session/credential seams (`_get_k8s_session`,
`_namespace_or_default`) stay in `monitoring` and are resolved lazily so tests can
monkeypatch them on that module.
Key entry points: `k8s_check_blast_status`, `k8s_cancel_blast_job`,
`_fetch_blast_pods_and_jobs`, `_reset_blast_status_cache`
Risky contracts: Use direct Kubernetes API helpers; do not reintroduce Azure Run Command.
The 3 s status cache keeps per-row `/api/blast/jobs` polling cheap — keep the TTL well
under the frontend polling cadence.
Validation: `uv run pytest -q api/tests/test_k8s_blast_status.py`.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, cast

from azure.core.credentials import TokenCredential

from api.services.k8s.timestamps import (
    k8s_timestamp_span_payload as _k8s_timestamp_span_payload,
)
from api.services.k8s.timestamps import (
    max_k8s_timestamp as _max_k8s_timestamp,
)
from api.services.k8s.timestamps import (
    min_k8s_timestamp as _min_k8s_timestamp,
)
from api.services.k8s.timestamps import (
    parse_k8s_timestamp as _parse_k8s_timestamp,
)

_K8S_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9._-]{1,63}$")

# ---------------------------------------------------------------------------
# Short-TTL cache for cluster-wide app=blast pods/jobs lookups.
# ---------------------------------------------------------------------------
# ``k8s_check_blast_status`` performs two cluster-wide HTTP roundtrips
# (``GET .../pods?labelSelector=app=blast`` and ``.../jobs?labelSelector=app=blast``)
# before doing in-memory filtering for a specific ``job_id``. The BLAST jobs
# list endpoint calls this helper once per active row, so each browser poll
# pays N × 2 round-trips. A 3 s TTL memoisation reduces this to ~2 round-trips
# per browser poll regardless of how many active rows the user has, while
# keeping the freshness budget well under the frontend polling cadence.

_BLAST_STATUS_CACHE_TTL_SECONDS = 3.0
_BLAST_STATUS_CACHE: dict[tuple[str, str, str, str], tuple[float, dict[str, Any]]] = {}
_BLAST_STATUS_CACHE_LOCK = threading.Lock()


def _reset_blast_status_cache() -> None:
    with _BLAST_STATUS_CACHE_LOCK:
        _BLAST_STATUS_CACHE.clear()


def _fetch_blast_pods_and_jobs(
    session: Any,
    server: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
) -> dict[str, Any]:
    """Return ``{target_ns, all_pods, all_jobs}`` or ``{error: ...}`` for the cluster.

    Short-TTL memoised (~3 s) keyed by the cluster + namespace so multiple
    per-row calls during one ``/api/blast/jobs`` request share one set of
    HTTP roundtrips.
    """

    from api.services.k8s.monitoring import _namespace_or_default

    key = (subscription_id, resource_group, cluster_name, namespace)
    now = time.monotonic()
    with _BLAST_STATUS_CACHE_LOCK:
        cached = _BLAST_STATUS_CACHE.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]

    target_ns = _namespace_or_default(session, server, namespace)
    pods_response = session.get(
        f"{server}/api/v1/namespaces/{target_ns}/pods",
        params={"labelSelector": "app=blast"},
        timeout=10,
    )
    if pods_response.status_code != 200:
        return {
            "error": f"pods API error: {pods_response.status_code}",
            "target_ns": target_ns,
        }
    all_pods = pods_response.json().get("items", [])

    jobs_response = session.get(
        f"{server}/apis/batch/v1/namespaces/{target_ns}/jobs",
        params={"labelSelector": "app=blast"},
        timeout=10,
    )
    if jobs_response.status_code != 200:
        return {
            "error": f"jobs API error: {jobs_response.status_code}",
            "target_ns": target_ns,
            "all_pods": all_pods,
        }
    all_jobs = jobs_response.json().get("items", [])

    result: dict[str, Any] = {
        "target_ns": target_ns,
        "all_pods": all_pods,
        "all_jobs": all_jobs,
    }
    with _BLAST_STATUS_CACHE_LOCK:
        _BLAST_STATUS_CACHE[key] = (now + _BLAST_STATUS_CACHE_TTL_SECONDS, result)
    return result


def _container_terminated_state(container_status: dict[str, Any]) -> dict[str, Any] | None:
    for state_key in ("state", "lastState"):
        state = container_status.get(state_key, {})
        if not isinstance(state, dict):
            continue
        terminated = state.get("terminated")
        if isinstance(terminated, dict):
            return terminated
    return None


def _pod_has_env_value(pod: dict[str, Any], name: str, value: str) -> bool:
    for container in pod.get("spec", {}).get("containers", []) or []:
        for env in container.get("env", []) or []:
            if env.get("name") == name and env.get("value") == value:
                return True
    return False


def _owned_job_names(pods: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for pod in pods:
        for owner in pod.get("metadata", {}).get("ownerReferences", []) or []:
            if owner.get("kind") == "Job" and owner.get("name"):
                names.add(owner["name"])
    return names


def _job_has_label_value(job: dict[str, Any], name: str, value: str) -> bool:
    return cast(bool, job.get("metadata", {}).get("labels", {}).get(name) == value)


def k8s_check_blast_status(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Return ElasticBLAST search status scoped by ElasticBLAST's K8s job id.

    Empty ``app=blast`` Jobs/Pods means the search has not been scheduled yet,
    so the honest status is ``creating`` rather than ``completed``.
    """

    from api.services.k8s.monitoring import _get_k8s_session

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        snapshot = _fetch_blast_pods_and_jobs(
            session,
            server,
            subscription_id,
            resource_group,
            cluster_name,
            namespace,
        )
        target_ns = snapshot.get("target_ns") or namespace
        if "error" in snapshot:
            error = str(snapshot["error"])
            return {
                "status": "unknown",
                "pods": len(snapshot.get("all_pods") or []),
                "detail": error,
            }
        all_pods = snapshot.get("all_pods") or []
        blast_pods = (
            [pod for pod in all_pods if _pod_has_env_value(pod, "BLAST_ELB_JOB_ID", job_id)]
            if job_id
            else all_pods
        )

        all_jobs = snapshot.get("all_jobs") or []
        if job_id:
            scoped_names = _owned_job_names(blast_pods)
            jobs = [
                job
                for job in all_jobs
                if job.get("metadata", {}).get("name") in scoped_names
                or _job_has_label_value(job, "elb-job-id", job_id)
            ]
        else:
            jobs = all_jobs

        if not jobs and not blast_pods:
            return {
                "status": "creating",
                "pods": 0,
                "jobs": 0,
                "detail": "no app=blast jobs/pods yet",
                "namespace": target_ns,
            }

        succeeded = 0
        failed = 0
        active = 0
        started_at_values: list[str] = []
        completed_at_values: list[str] = []
        blast_container_started_at_values: list[str] = []
        blast_container_completed_at_values: list[str] = []
        results_export_container_started_at_values: list[str] = []
        results_export_container_completed_at_values: list[str] = []
        blast_container_count = 0
        results_export_container_count = 0
        for job in jobs:
            job_status = job.get("status", {})
            succeeded += job_status.get("succeeded", 0)
            failed += job_status.get("failed", 0)
            active += job_status.get("active", 0)
            if job_status.get("startTime"):
                started_at_values.append(str(job_status["startTime"]))
            if job_status.get("completionTime"):
                completed_at_values.append(str(job_status["completionTime"]))

        for pod in blast_pods:
            pod_status = pod.get("status", {})
            if pod_status.get("startTime"):
                started_at_values.append(str(pod_status["startTime"]))
            for container_status in pod_status.get("containerStatuses", []) or []:
                if not isinstance(container_status, dict):
                    continue
                terminated = _container_terminated_state(container_status)
                if terminated is None:
                    continue
                container_name = str(container_status.get("name") or "")
                if terminated.get("startedAt"):
                    started_at_values.append(str(terminated["startedAt"]))
                if terminated.get("finishedAt"):
                    completed_at_values.append(str(terminated["finishedAt"]))
                if container_name == "blast":
                    blast_container_count += 1
                    if terminated.get("startedAt"):
                        blast_container_started_at_values.append(str(terminated["startedAt"]))
                    if terminated.get("finishedAt"):
                        blast_container_completed_at_values.append(str(terminated["finishedAt"]))
                elif container_name == "results-export":
                    results_export_container_count += 1
                    if terminated.get("startedAt"):
                        results_export_container_started_at_values.append(
                            str(terminated["startedAt"])
                        )
                    if terminated.get("finishedAt"):
                        results_export_container_completed_at_values.append(
                            str(terminated["finishedAt"])
                        )

        if failed > 0:
            blast_status = "failed"
        elif active > 0:
            blast_status = "running"
        elif succeeded > 0 and succeeded >= len(jobs):
            blast_status = "completed"
        else:
            blast_status = "creating"

        started_at = _min_k8s_timestamp(started_at_values)
        completed_at = _max_k8s_timestamp(completed_at_values)
        result = {
            "status": blast_status,
            "job_id": job_id,
            "pods": len(blast_pods),
            "jobs": len(jobs),
            "succeeded": succeeded,
            "failed": failed,
            "active": active,
            "namespace": target_ns,
            "scoped_by_job_id": bool(job_id),
            "blast_container_count": blast_container_count,
            "results_export_container_count": results_export_container_count,
        }
        if started_at:
            result["started_at"] = started_at
        if completed_at:
            result["completed_at"] = completed_at
        result.update(
            _k8s_timestamp_span_payload(
                "blast_container",
                blast_container_started_at_values,
                blast_container_completed_at_values,
            )
        )
        result.update(
            _k8s_timestamp_span_payload(
                "results_export_container",
                results_export_container_started_at_values,
                results_export_container_completed_at_values,
            )
        )
        return result
    except Exception as exc:
        return {"status": "unknown", "pods": 0, "detail": str(exc)[:200]}
    finally:
        session.close()


def _job_is_terminal(job: dict[str, Any]) -> bool:
    """A K8s Job is terminal once it has any succeeded or failed pod."""
    status = job.get("status", {}) or {}
    try:
        succeeded = int(status.get("succeeded", 0) or 0)
        failed = int(status.get("failed", 0) or 0)
    except (TypeError, ValueError):
        return False
    return succeeded > 0 or failed > 0


def _finalizer_is_terminal(job: dict[str, Any]) -> bool:
    """Fail-closed terminal test for Gate B finalizer counting.

    Stricter than ``_job_is_terminal``: a bare ``status.failed > 0`` does NOT
    prove a Job is done — the Job controller increments ``failed`` per failed Pod
    attempt and keeps retrying until ``backoffLimit`` is exhausted, at which point
    it sets a ``Failed`` condition. Treating ``failed > 0`` as terminal (the
    ``_job_is_terminal`` heuristic) would DROP a still-retrying finalizer from the
    count → under-count → over-admit past the ceiling (round-3 M-B, a fail-OPEN
    error in the wrong direction for an admission gate).

    A finalizer is counted as terminal here ONLY when it is definitively done:

    * ``status.succeeded > 0`` (completed successfully), OR
    * ``status.completionTime`` is set, OR
    * a ``status.conditions`` entry of type ``Complete``/``Failed`` has
      ``status == "True"``.

    Anything else (including a bare ``failed > 0`` with no terminal condition) is
    treated as STILL ACTIVE and counted — the fail-closed direction for a gate.
    """
    status = job.get("status", {}) or {}
    if not isinstance(status, dict):
        return False
    try:
        if int(status.get("succeeded", 0) or 0) > 0:
            return True
    except (TypeError, ValueError):
        return False
    if status.get("completionTime"):
        return True
    conditions = status.get("conditions", []) or []
    if isinstance(conditions, list):
        for cond in conditions:
            if not isinstance(cond, dict):
                continue
            if cond.get("type") in ("Complete", "Failed") and (
                str(cond.get("status", "")).lower() == "true"
            ):
                return True
    return False


def _job_label(job: dict[str, Any], name: str) -> str:
    return str(((job.get("metadata", {}) or {}).get("labels", {}) or {}).get(name) or "")


def _job_age_seconds(job: dict[str, Any], now: float) -> float | None:
    raw = (job.get("metadata", {}) or {}).get("creationTimestamp")
    if not raw:
        return None
    try:
        created = _parse_k8s_timestamp(str(raw)).timestamp()
    except ValueError:
        return None
    return max(0.0, now - created)


def k8s_count_active_blast_submissions(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
) -> int:
    """Gate B count — distinct active ``elastic-blast submit`` units on the cluster.

    Counts distinct ``elb-job-id`` values among NON-TERMINAL ``app=finalizer`` Jobs
    (one finalizer per submit, created synchronously, lifecycle-long). NOT raw
    ``app=blast`` Jobs (N-per-submit, async on Azure) — see the design note
    docs/research/blast-submit-coordination.md §5.1.

    Two correctness contracts this helper MUST keep:

    * **Uncached / fresh read.** This is an admission decision, so it bypasses the
      ~3s ``_fetch_blast_pods_and_jobs`` memo: a finalizer created seconds ago by
      the previous Lease holder must be visible (design §5.2). It does its own
      direct list.
    * **Fail-closed.** A read failure RAISES (caller releases the Lease and
      requeues ``waiting_for_capacity``); it never returns a low count that would
      over-admit past the ceiling (design §5.2).

    Phantom-slot guard: a lone finalizer with no live companion (``app=submit`` /
    ``app=blast``) and older than ``BLAST_FINALIZER_GRACE_SECONDS`` is treated as
    an orphaned phantom and NOT counted; younger ones are counted to cover the
    async batch-creation lag (design §5.1).
    """
    from api.services.blast.coordination import (
        FINALIZER_COMPANION_SELECTORS,
        FINALIZER_JOB_ID_LABEL,
        FINALIZER_LABEL_SELECTOR,
        finalizer_grace_seconds,
    )
    from api.services.k8s.monitoring import _get_k8s_session

    # Gate B reads under the SAME admin kubeconfig as Gate A (the Lease in
    # api.services.k8s.submit_lease uses admin=True). Without this the two gates
    # are credential-asymmetric: Gate A acquires via the RBAC-bypassing admin
    # kubeconfig while Gate B lists Jobs via the non-admin MI token. On a cluster
    # where the shared MI lacks ``jobs:list`` RBAC, Gate B's list 403s → the
    # fail-closed contract requeues EVERY submit forever even though Gate A keeps
    # admitting — a cluster-wide submit outage from an auth split. Reading the
    # admission ceiling from the same cluster-truth credential closes that gap
    # (round-3 H-B).
    session, server = _get_k8s_session(
        credential, subscription_id, resource_group, cluster_name, admin=True
    )
    try:
        # Gate B MUST count in the EXACT same namespace Gate A (the Lease) locked.
        # The Lease (api.services.k8s.submit_lease) scopes itself to the raw
        # ``namespace`` argument, so this count uses the raw namespace too —
        # NOT ``_namespace_or_default`` (a cluster-default lookup that can resolve
        # to a DIFFERENT namespace than the Lease, enforcing the ceiling against
        # the wrong population while the mutex guards another) — critique C3.
        target_ns = (namespace or "default").strip() or "default"
        finalizer_app = FINALIZER_LABEL_SELECTOR.split("=", 1)[-1]
        companion_apps = {sel.split("=", 1)[-1] for sel in FINALIZER_COMPANION_SELECTORS}
        # One set-based selector list keeps the read scoped while still fresh.
        selector = f"app in ({finalizer_app},{','.join(sorted(companion_apps))})"
        resp = session.get(
            f"{server}/apis/batch/v1/namespaces/{target_ns}/jobs",
            params={"labelSelector": selector},
            timeout=10,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"jobs API error: {resp.status_code}")
        items = resp.json().get("items", []) or []

        now = time.time()
        grace = finalizer_grace_seconds()
        live_companion_ids: set[str] = set()
        finalizers: list[dict[str, Any]] = []
        for job in items:
            app = _job_label(job, "app")
            if _finalizer_is_terminal(job):
                continue
            if app == finalizer_app:
                finalizers.append(job)
            elif app in companion_apps:
                job_id = _job_label(job, FINALIZER_JOB_ID_LABEL)
                if job_id:
                    live_companion_ids.add(job_id)

        counted: set[str] = set()
        for job in finalizers:
            job_id = _job_label(job, FINALIZER_JOB_ID_LABEL)
            if not job_id:
                # A non-terminal finalizer with no elb-job-id label cannot be
                # matched to a companion, but it still represents a real submit
                # occupying a slot. Skipping it (the old behaviour) UNDER-COUNTS
                # and over-admits past the ceiling. Count it fail-closed with a
                # synthetic per-Job key so each distinct label-less finalizer
                # occupies exactly one slot (critique M15).
                synthetic = job.get("metadata", {}).get("uid") or job.get("metadata", {}).get(
                    "name"
                )
                counted.add(f"__unlabeled__:{synthetic or id(job)}")
                continue
            if job_id in counted:
                continue
            if job_id in live_companion_ids:
                counted.add(job_id)
                continue
            age = _job_age_seconds(job, now)
            if age is None or age < grace:
                counted.add(job_id)  # within async-creation grace → still a live slot
            # else: companion-less + past grace → phantom, not counted
        return len(counted)
    finally:
        session.close()


def k8s_cancel_blast_job(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    job_id: str,
) -> dict[str, Any]:
    """Delete this submission's Kubernetes Jobs by ``elb-job-id`` label."""

    from api.services.k8s.monitoring import _get_k8s_session, _namespace_or_default

    if not _K8S_LABEL_VALUE_RE.match(job_id):
        raise ValueError("job_id is not a valid Kubernetes label value")

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        target_ns = _namespace_or_default(session, server, namespace)
        deleted: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for app in ("blast", "submit"):
            selector = f"app={app},elb-job-id={job_id}"
            response = session.delete(
                f"{server}/apis/batch/v1/namespaces/{target_ns}/jobs",
                params={"labelSelector": selector, "propagationPolicy": "Background"},
                timeout=10,
            )
            item = {"app": app, "selector": selector, "status_code": response.status_code}
            if response.status_code in (200, 201, 202, 404):
                deleted.append(item)
            else:
                errors.append({**item, "detail": response.text[:200]})

        return {
            "status": "cancelled" if not errors else "unknown",
            "namespace": target_ns,
            "job_id": job_id,
            "deleted": deleted,
            "errors": errors,
        }
    finally:
        session.close()
