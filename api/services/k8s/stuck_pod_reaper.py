"""Stuck BLAST pod classification — the pure, IO-free reaping decision.

Responsibility: Decide whether a single `app=blast` pod is a reapable zombie
(an unschedulable `Pending`, or a non-resolving waiting reason such as
`CrashLoopBackOff` / `ImagePullBackOff` past an age threshold) WITHOUT ever
selecting a Running / Succeeded / Completed / still-starting pod. IO-free so
every branch is unit-tested.
Edit boundaries: Pure functions + constants only. No Kubernetes / Azure SDK, no
HTTP. The orchestration (cluster enumeration, pod fetch, owner-Job delete, beat
wiring, the enable/dry-run flags) lives in the reaper service/task that consumes
`classify_stuck_blast_pod`; keep that side-effectful code out of this module.
Key entry points: `classify_stuck_blast_pod`, `ReaperThresholds`.
Risky contracts: `classify_stuck_blast_pod` MUST NEVER return ``"reap"`` for a
pod that is or might be making progress — `Running`, `ContainerCreating`,
`PodInitializing`, `Completed`, `Succeeded`, `Terminating`, `Unknown`, or an
empty status are always kept. blastn never starts in a reapable waiting reason,
so reaping can never terminate in-progress search work. `Init:<reason>` is
normalised to `<reason>` before matching so an init-container crash is caught.
Validation: `uv run pytest -q api/tests/test_stuck_pod_reaper.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

# Container waiting reasons that never resolve on their own — the pod sits in
# this state until something deletes it. blastn never starts in any of them, so
# reaping a pod in one of these states cannot kill in-progress search work.
_REAPABLE_WAITING_REASONS = frozenset(
    {
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "ErrImageNeverPull",
        "InvalidImageName",
        "CreateContainerConfigError",
        "CreateContainerError",
    }
)

# Statuses that are, or might be, making progress. ALWAYS kept. `ContainerCreating`
# and `PodInitializing` are the healthy startup path (image pulling / init
# running) — they are NOT reaped; a genuinely wedged image pull surfaces as
# `ImagePullBackOff`/`ErrImagePull` instead, which IS reapable. A bare `Pending`
# is handled separately because an unschedulable pod also reports `Pending`.
_NEVER_REAP_STATUSES = frozenset(
    {
        "Running",
        "Completed",
        "Succeeded",
        "Terminating",
        "ContainerCreating",
        "PodInitializing",
        "Unknown",
    }
)


@dataclass(frozen=True)
class ReaperThresholds:
    """Minimum age (seconds) before a stuck pod becomes eligible for reaping.

    The grace windows are deliberately generous so a pod that is briefly
    unschedulable while the cluster autoscaler adds a node, or briefly pulling a
    large image, is never reaped — only a pod still wedged past the window is.
    """

    pending_seconds: int = 900  # unschedulable Pending (autoscaler grace)
    waiting_seconds: int = 900  # CrashLoopBackOff / ImagePullBackOff / …


def classify_stuck_blast_pod(
    *,
    display_status: str,
    age_seconds: float,
    thresholds: ReaperThresholds,
) -> str:
    """Return ``"reap"`` or ``"keep"`` for one pod. IO-free.

    A Running / Succeeded / Completed / still-starting pod is ALWAYS kept —
    reaping can never terminate in-progress BLAST work. Only a pod that is
    provably wedged (an unschedulable ``Pending``, or a non-resolving waiting
    reason such as ``CrashLoopBackOff`` / ``ImagePullBackOff``) past its age
    threshold is reaped. ``display_status`` is the kubectl-style status from
    ``compute_pod_display_status`` (a bare reason, optionally ``Init:``-prefixed).
    """
    status = (display_status or "").strip()
    if not status:
        return "keep"
    # Init:<reason> -> <reason> so an init-container CrashLoop/ImagePull is caught.
    base = status.split(":")[-1]
    if status in _NEVER_REAP_STATUSES or base in _NEVER_REAP_STATUSES:
        return "keep"
    if base in _REAPABLE_WAITING_REASONS and age_seconds >= thresholds.waiting_seconds:
        return "reap"
    if status == "Pending" and age_seconds >= thresholds.pending_seconds:
        return "reap"
    return "keep"
