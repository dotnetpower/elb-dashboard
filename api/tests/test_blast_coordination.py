"""Unit tests for the BLAST submit coordination config + startup invariants.

Responsibility: Lock the env-driven tunables and the
``assert_coordination_invariants`` ordering contract for the ``k8s`` coordination
backend, including the charter §12a Rule 4 default-OFF behaviour.
Edit boundaries: Pure config/invariant tests — no Kubernetes, Celery, or Azure
calls. Lease/gate behaviour is covered in ``test_blast_submit_lease.py`` /
``test_blast_k8s_gate.py``.
Key entry points: ``test_backend_defaults_to_redis``,
``test_backend_only_k8s_when_explicit``, ``test_int_env_clamps_and_falls_back``,
``test_invariants_noop_when_redis``, ``test_invariants_pass_with_defaults``,
``test_invariants_reject_bad_time_limits``,
``test_invariants_reject_lease_ttl_below_submit_exec``.
Risky contracts: ``BLAST_COORD_BACKEND`` is the deliberate rollout flag — any
value but ``k8s`` MUST resolve to ``redis``.
Validation: ``uv run pytest -q api/tests/test_blast_coordination.py``.
"""

from __future__ import annotations

import pytest
from api.services.blast import coordination as coord


def test_backend_defaults_to_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLAST_COORD_BACKEND", raising=False)
    assert coord.coordination_backend() == "redis"
    assert coord.is_k8s_backend() is False


@pytest.mark.parametrize("value", ["k8s", "K8S", " k8s ", "K8s"])
def test_backend_only_k8s_when_explicit(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("BLAST_COORD_BACKEND", value)
    assert coord.coordination_backend() == "k8s"
    assert coord.is_k8s_backend() is True


@pytest.mark.parametrize("value", ["redis", "kubernetes", "k8", "", "yes"])
def test_backend_typo_falls_back_to_redis(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("BLAST_COORD_BACKEND", value)
    assert coord.coordination_backend() == "redis"


def test_int_env_clamps_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X", "not-an-int")
    assert coord._int_env("X", 5) == 5
    monkeypatch.setenv("X", "0")
    assert coord._int_env("X", 5, minimum=1) == 1
    monkeypatch.setenv("X", "999999999")
    assert coord._int_env("X", 5, maximum=10) == 10
    monkeypatch.delenv("X", raising=False)
    assert coord._int_env("X", 7) == 7


def test_concurrency_and_ttl_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "BLAST_MAX_RUN_CONCURRENCY",
        "BLAST_SUBMIT_LEASE_TTL_SECONDS",
        "BLAST_FINALIZER_GRACE_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    assert coord.max_run_concurrency() == 3
    assert coord.submit_lease_ttl_seconds() == 900
    assert coord.finalizer_grace_seconds() == 300


def test_clock_skew_and_grace_allow_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_LEASE_CLOCK_SKEW_SECONDS", "0")
    monkeypatch.setenv("BLAST_FINALIZER_GRACE_SECONDS", "0")
    assert coord.lease_clock_skew_seconds() == 0
    assert coord.finalizer_grace_seconds() == 0


def test_invariants_noop_when_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLAST_COORD_BACKEND", raising=False)
    # Even with deliberately broken Celery limits, redis mode never asserts.
    monkeypatch.setenv("CELERY_TASK_SOFT_TIME_LIMIT", "10")
    monkeypatch.setenv("CELERY_TASK_TIME_LIMIT", "5")
    coord.assert_coordination_invariants()  # must not raise


def test_invariants_pass_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_COORD_BACKEND", "k8s")
    for key in (
        "CELERY_TASK_SOFT_TIME_LIMIT",
        "CELERY_TASK_TIME_LIMIT",
        "BLAST_SUBMIT_LEASE_TTL_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    coord.assert_coordination_invariants()  # 600 < 3300 < 3600 and 600 < 900


def test_invariants_reject_bad_time_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_COORD_BACKEND", "k8s")
    monkeypatch.setenv("CELERY_TASK_SOFT_TIME_LIMIT", "300")  # < submit_exec(600)
    monkeypatch.setenv("CELERY_TASK_TIME_LIMIT", "3600")
    with pytest.raises(ValueError, match="SOFT_TIME_LIMIT"):
        coord.assert_coordination_invariants()


def test_invariants_reject_lease_ttl_below_submit_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLAST_COORD_BACKEND", "k8s")
    monkeypatch.delenv("CELERY_TASK_SOFT_TIME_LIMIT", raising=False)
    monkeypatch.delenv("CELERY_TASK_TIME_LIMIT", raising=False)
    monkeypatch.setenv("BLAST_SUBMIT_LEASE_TTL_SECONDS", "300")  # < submit_exec(600)
    with pytest.raises(ValueError, match="LEASE_TTL_SECONDS"):
        coord.assert_coordination_invariants()


def test_invariants_parse_celery_limits_unclamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # celery_app parses these limits with a bare int() (no max clamp), so the
    # invariant must validate the SAME large numbers — not a clamped 86_400 that
    # would diverge from what the worker actually runs with (critique M18).
    monkeypatch.setenv("BLAST_COORD_BACKEND", "k8s")
    monkeypatch.delenv("BLAST_SUBMIT_LEASE_TTL_SECONDS", raising=False)
    monkeypatch.setenv("CELERY_TASK_SOFT_TIME_LIMIT", "90000")
    monkeypatch.setenv("CELERY_TASK_TIME_LIMIT", "100000")
    coord.assert_coordination_invariants()  # 600 < 90000 < 100000 → must not raise


def test_split_gate_wait_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "BLAST_SPLIT_CHILD_GATE_WAIT_MAX_SECONDS",
        "BLAST_SPLIT_PARENT_GATE_BUDGET_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    # Per-child cap is much smaller than the requeue-path budget, bounding
    # head-of-line blocking on the single worker (critique H4).
    assert coord.split_child_gate_wait_max_seconds() == 300
    assert coord.split_parent_gate_budget_seconds() == 1800
    assert coord.split_child_gate_wait_max_seconds() < coord.submit_slot_wait_max_seconds()


def test_split_gate_wait_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_SPLIT_CHILD_GATE_WAIT_MAX_SECONDS", "120")
    monkeypatch.setenv("BLAST_SPLIT_PARENT_GATE_BUDGET_SECONDS", "600")
    assert coord.split_child_gate_wait_max_seconds() == 120
    assert coord.split_parent_gate_budget_seconds() == 600
