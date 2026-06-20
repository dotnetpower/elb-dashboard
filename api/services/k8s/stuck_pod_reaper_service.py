"""Stuck BLAST pod reaper — single-cluster orchestration (side-effectful).

Responsibility: Scan one AKS cluster's `app=blast` pods, classify each via the
pure `classify_stuck_blast_pod`, and (unless dry-run) delete the OWNER JOB of
any pod that is wedged (unschedulable Pending / CrashLoopBackOff / ImagePullBackOff
past its threshold). Deleting the Job — not the bare pod — is deliberate: a Job
would just recreate a deleted pod.
Edit boundaries: This is the IO layer that consumes the pure decision in
`stuck_pod_reaper.py`; keep the keep/reap logic there. Cluster enumeration + the
enable / dry-run env flags live in the beat task that calls this.
Key entry points: `reap_stuck_blast_pods_in_cluster`.
Risky contracts: MUST NEVER raise — a beat tick degrades to an empty/partial
summary on any K8s error so it cannot crash the worker. `dry_run=True` (the
default) only logs and reports, never deletes. Only pods the pure classifier
marks `reap` are ever acted on; Running / starting / completed pods are
structurally never selected, so reaping cannot terminate in-progress work.
Validation: `uv run pytest -q api/tests/test_stuck_pod_reaper_service.py`.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.k8s.observability import compute_pod_display_status
from api.services.k8s.stuck_pod_reaper import (
    ReaperThresholds,
    classify_stuck_blast_pod,
)
from api.services.k8s.workload_ops import k8s_job_delete

LOGGER = logging.getLogger(__name__)

_DEFAULT_APP_LABEL = "blast"


def _pod_age_seconds(creation_ts: str, now: datetime) -> float:
    """Seconds since the pod's creationTimestamp; 0 when unknown/unparseable."""
    if not creation_ts:
        return 0.0
    try:
        created = datetime.fromisoformat(creation_ts.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return max(0.0, (now - created).total_seconds())


def _owner_job_name(pod: dict[str, Any]) -> str:
    """The Job that owns this pod — the `job-name` label, else an ownerRef Job."""
    meta = pod.get("metadata", {}) if isinstance(pod.get("metadata"), dict) else {}
    labels = meta.get("labels", {}) if isinstance(meta.get("labels"), dict) else {}
    name = labels.get("job-name")
    if isinstance(name, str) and name:
        return name
    refs = meta.get("ownerReferences")
    if isinstance(refs, list):
        for ref in refs:
            if isinstance(ref, dict) and ref.get("kind") == "Job" and ref.get("name"):
                return str(ref["name"])
    return ""


def reap_stuck_blast_pods_in_cluster(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    namespace: str = "default",
    thresholds: ReaperThresholds | None = None,
    dry_run: bool = True,
    app_label: str = _DEFAULT_APP_LABEL,
) -> dict[str, Any]:
    """Reap wedged `app=<app_label>` pods in one cluster. Never raises.

    Returns a summary dict: ``cluster``, ``scanned`` (pods examined),
    ``reaped_jobs`` (owner Jobs deleted, or that WOULD be deleted in dry-run),
    ``dry_run``, ``errors``. With ``dry_run=True`` no Job is deleted — the
    wedged pods are only logged + reported.
    """
    thresholds = thresholds or ReaperThresholds()
    summary: dict[str, Any] = {
        "cluster": cluster_name,
        "scanned": 0,
        "reaped_jobs": [],
        "dry_run": dry_run,
        "errors": 0,
    }

    # Imported lazily: the session helper pulls the AKS kubeconfig token and is
    # not needed on the import path of callers that never reap.
    from api.services.k8s.monitoring import _get_k8s_session

    try:
        session, server = _get_k8s_session(
            credential, subscription_id, resource_group, cluster_name
        )
    except Exception as exc:
        LOGGER.info(
            "reaper: k8s session unavailable cluster=%s: %s",
            cluster_name,
            type(exc).__name__,
        )
        summary["errors"] += 1
        return summary

    try:
        url = (
            f"{server}/api/v1/namespaces/{namespace}/pods"
            f"?labelSelector=app%3D{app_label}"
        )
        resp = session.get(url, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("items", []) or []
        now = datetime.now(UTC)
        reap_jobs: set[str] = set()
        for pod in items:
            if not isinstance(pod, dict):
                continue
            summary["scanned"] += 1
            status = compute_pod_display_status(pod)
            meta = pod.get("metadata", {}) if isinstance(pod.get("metadata"), dict) else {}
            age = _pod_age_seconds(str(meta.get("creationTimestamp") or ""), now)
            if (
                classify_stuck_blast_pod(
                    display_status=status, age_seconds=age, thresholds=thresholds
                )
                != "reap"
            ):
                continue
            job = _owner_job_name(pod)
            LOGGER.info(
                "reaper: stuck pod=%s status=%s age=%.0fs owner_job=%s dry_run=%s",
                meta.get("name"),
                status,
                age,
                job or "(none)",
                dry_run,
            )
            if job:
                reap_jobs.add(job)
        for job in sorted(reap_jobs):
            if dry_run:
                summary["reaped_jobs"].append(job)
                continue
            try:
                k8s_job_delete(
                    credential,
                    subscription_id,
                    resource_group,
                    cluster_name,
                    namespace,
                    job,
                )
                summary["reaped_jobs"].append(job)
                LOGGER.info("reaper: deleted stuck Job=%s cluster=%s", job, cluster_name)
            except Exception as exc:
                summary["errors"] += 1
                LOGGER.warning(
                    "reaper: failed to delete Job=%s cluster=%s: %s",
                    job,
                    cluster_name,
                    type(exc).__name__,
                )
    except Exception as exc:
        summary["errors"] += 1
        LOGGER.info(
            "reaper: scan failed cluster=%s: %s", cluster_name, type(exc).__name__
        )
    finally:
        with contextlib.suppress(Exception):
            session.close()

    return summary
