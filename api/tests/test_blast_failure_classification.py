"""Tests for BLAST terminal-failure classification.

Responsibility: Lock the error_code -> category + auto_retryable mapping that the
auto-retry sweep, the API projection, and the UI all depend on.
Edit boundaries: Test-only. When a new terminal error_code is added to the
codebase, add an assertion here in the same change.
Key entry points: pytest test functions.
Risky contracts: Only TRANSIENT_INFRA is auto_retryable; this file is the guard.
Validation: ``uv run pytest -q api/tests/test_blast_failure_classification.py``.
"""

from __future__ import annotations

import pytest
from api.services.blast.failure_classification import FailureCategory, classify_failure


@pytest.mark.parametrize(
    "code",
    [
        "terminal_az_login_failed",
        "terminal_kubeconfig_failed",
        "terminal_exec_unavailable",
        "terminal_sidecar_unavailable",
        "exec_token_missing",
        "blast_submit_lease_api_error",
        "blast_submit_requeue_failed",
        "node_warmup_wait_deadline_exceeded",
    ],
)
def test_transient_infra_is_auto_retryable(code: str) -> None:
    result = classify_failure(code)
    assert result.category is FailureCategory.TRANSIENT_INFRA
    assert result.auto_retryable is True


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("blast_search_failed", FailureCategory.RUNTIME),
        ("worker_lost", FailureCategory.CLUSTER_STATE),
        ("cluster_stopped", FailureCategory.CLUSTER_STATE),
        ("cluster_not_found", FailureCategory.CLUSTER_STATE),
        ("cluster_lifecycle_interrupted", FailureCategory.CLUSTER_STATE),
        ("database_not_ready", FailureCategory.DATABASE_WAIT),
        ("database_updating", FailureCategory.DATABASE_WAIT),
        ("blast_capacity_full", FailureCategory.CAPACITY),
        ("blast_submit_slot_busy", FailureCategory.CAPACITY),
        ("submit_failed", FailureCategory.PERMANENT),
        ("program_database_incompatible", FailureCategory.PERMANENT),
        ("blocked_by_preflight", FailureCategory.PERMANENT),
    ],
)
def test_non_transient_is_not_auto_retryable(code: str, category: FailureCategory) -> None:
    result = classify_failure(code)
    assert result.category is category
    assert result.auto_retryable is False


def test_capacity_prefix_classified() -> None:
    assert classify_failure("capacity_gate_ceiling_full").category is FailureCategory.CAPACITY
    assert classify_failure("blast_capacity_xyz").category is FailureCategory.CAPACITY


def test_empty_and_unknown_codes() -> None:
    assert classify_failure("").category is FailureCategory.UNKNOWN
    assert classify_failure("totally_made_up").category is FailureCategory.UNKNOWN
    assert classify_failure("totally_made_up").auto_retryable is False


def test_as_dict_shape() -> None:
    d = classify_failure("terminal_az_login_failed").as_dict()
    assert d["category"] == "transient_infra"
    assert d["auto_retryable"] is True
    assert isinstance(d["reason"], str) and d["reason"]
