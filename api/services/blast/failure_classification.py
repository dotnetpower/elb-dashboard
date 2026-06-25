"""Failure classification for terminal BLAST jobs — the single source of truth.

Responsibility: Map a terminal BLAST job's ``error_code`` (and phase) to a coarse
failure category and an ``auto_retryable`` verdict, so the auto-retry sweep, the
API, and the UI all agree on whether a failed job may be re-submitted safely.
Edit boundaries: Pure classification — no Azure, no Storage, no Celery. Adding a
new terminal ``error_code`` to the codebase means adding it to one of the sets
below in the SAME change.
Key entry points: ``classify_failure``, ``FailureCategory``.
Risky contracts: Only ``TRANSIENT_INFRA`` codes are ``auto_retryable``. K8s
runtime failures (``blast_search_failed``) and cluster-state failures
(``worker_lost`` / ``cluster_stopped`` / ``cluster_not_found``) are deliberately
NOT auto-retryable: re-submitting them orphans a K8s job, re-stages the database
(cost), or queues forever against a stopped cluster. Widening
``_TRANSIENT_INFRA_CODES`` changes what the auto-retry sweep will resubmit.
Validation: ``uv run pytest -q api/tests/test_blast_failure_classification.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FailureCategory(StrEnum):
    """Coarse failure family for a terminal BLAST job."""

    TRANSIENT_INFRA = "transient_infra"
    CAPACITY = "capacity"
    DATABASE_WAIT = "database_wait"
    RUNTIME = "runtime"
    CLUSTER_STATE = "cluster_state"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


# Submit-phase infrastructure failures that never reached the cluster (no K8s
# job created, nothing to orphan) and are genuinely transient — a later attempt
# can succeed once the terminal sidecar / Azure auth / node warmup settles.
# These are the ONLY codes the auto-retry sweep will resubmit.
_TRANSIENT_INFRA_CODES = frozenset(
    {
        "terminal_az_login_failed",
        "terminal_kubeconfig_failed",
        "terminal_exec_unavailable",
        "terminal_sidecar_unavailable",
        "exec_token_missing",
        "blast_submit_lease_api_error",
        "blast_submit_requeue_failed",
        "node_warmup_wait_deadline_exceeded",
    }
)

# Capacity / admission waits. The submit task already re-enqueues these in-flight
# (bounded by a deadline), so a terminal job carrying one of these is rare; the
# auto-retry sweep does NOT resubmit them (the gate would just deny again).
_CAPACITY_CODES = frozenset(
    {
        "blast_capacity_full",
        "blast_submit_slot_busy",
        "blast_submit_lock_busy",
    }
)
_CAPACITY_PREFIXES = ("capacity_gate_", "blast_capacity_")

# Database-staging waits. Also re-enqueued in-flight by the submit task; a
# terminal job here means the prepare-db pipeline never finished — operator
# action, not an automatic resubmit.
_DATABASE_WAIT_CODES = frozenset(
    {
        "database_not_ready",
        "database_updating",
    }
)

# The K8s search itself failed. Re-submitting orphans the failed K8s job and
# re-stages the DB — the user must inspect the error / fix the query first.
_RUNTIME_CODES = frozenset(
    {
        "blast_search_failed",
    }
)

# The cluster is gone / stopped. A resubmit would queue forever (or fail) until
# the cluster is provably running again — out of scope for an unattended sweep.
_CLUSTER_STATE_CODES = frozenset(
    {
        "worker_lost",
        "cluster_stopped",
        "cluster_not_found",
    }
)

# Configuration / contract failures — deterministic, retrying changes nothing.
_PERMANENT_CODES = frozenset(
    {
        "submit_failed",
        "config_invalid",
        "program_database_incompatible",
        "blocked_by_preflight",
        "sharding_precision_invalid",
        "sharding_precision_blocked",
        "web_blast_compatibility_blocked",
        "split_submit_invalid",
    }
)


@dataclass(frozen=True)
class FailureClassification:
    category: FailureCategory
    auto_retryable: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "auto_retryable": self.auto_retryable,
            "reason": self.reason,
        }


def _category_for_code(error_code: str) -> FailureCategory:
    code = (error_code or "").strip()
    if not code:
        return FailureCategory.UNKNOWN
    if code in _TRANSIENT_INFRA_CODES:
        return FailureCategory.TRANSIENT_INFRA
    if code in _RUNTIME_CODES:
        return FailureCategory.RUNTIME
    if code in _CLUSTER_STATE_CODES:
        return FailureCategory.CLUSTER_STATE
    if code in _DATABASE_WAIT_CODES:
        return FailureCategory.DATABASE_WAIT
    if code in _CAPACITY_CODES or code.startswith(_CAPACITY_PREFIXES):
        return FailureCategory.CAPACITY
    if code in _PERMANENT_CODES:
        return FailureCategory.PERMANENT
    return FailureCategory.UNKNOWN


_REASONS = {
    FailureCategory.TRANSIENT_INFRA: (
        "Transient submit-phase infrastructure failure "
        "(no cluster job created); safe to resubmit."
    ),
    FailureCategory.CAPACITY: (
        "Capacity/admission wait; the submit task already retries this in-flight."
    ),
    FailureCategory.DATABASE_WAIT: (
        "Database staging not finished; needs the prepare-db pipeline, not a resubmit."
    ),
    FailureCategory.RUNTIME: (
        "BLAST search failed on the cluster; inspect the error before resubmitting."
    ),
    FailureCategory.CLUSTER_STATE: (
        "Cluster is stopped or missing; resubmit only after it is running again."
    ),
    FailureCategory.PERMANENT: (
        "Configuration/contract failure; retrying changes nothing."
    ),
    FailureCategory.UNKNOWN: "Unrecognised failure; not auto-retried.",
}


def classify_failure(error_code: str, phase: str = "") -> FailureClassification:
    """Classify a terminal failed BLAST job from its ``error_code``.

    ``phase`` is accepted for forward-compatibility (a future caller may refine
    by phase) but the current mapping is keyed on ``error_code`` alone — the
    code is the precise signal and the phase is already encoded in which code
    was assigned. Only ``TRANSIENT_INFRA`` is ``auto_retryable``.
    """
    del phase  # reserved; error_code is the authoritative signal today
    category = _category_for_code(error_code)
    return FailureClassification(
        category=category,
        auto_retryable=category is FailureCategory.TRANSIENT_INFRA,
        reason=_REASONS[category],
    )
