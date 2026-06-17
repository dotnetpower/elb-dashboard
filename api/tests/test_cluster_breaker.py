"""Unit tests for the per-cluster AKS API circuit breaker.

Module summary: Verifies the in-memory breaker trips after the failure
threshold, rejects calls while open, optimistically closes after the cooldown,
resets on success, and honours its env knobs / disable flag. Also covers the
``_get_k8s_session`` integration: once the breaker is open the pooled fast path
is skipped and ``ClusterApiUnreachable`` is raised before any ARM/HTTP call.
Responsibility: Lock in the breaker contract so a stopped/deleted cluster can
never re-flood App Insights with one ConnectionError per poll.
Edit boundaries: Test-only; touches no production state beyond the breaker
reset helper.
Key entry points: ``cluster_breaker_*`` helpers, ``_get_k8s_session``.
Risky contracts: ``ClusterApiUnreachable`` must subclass ``ConnectionError`` so
existing broad ``except Exception`` graceful handlers keep catching it.
Validation: `uv run pytest -q api/tests/test_cluster_breaker.py`.
"""

from __future__ import annotations

import time

import pytest
from api.services.k8s import cluster_breaker as cb


@pytest.fixture(autouse=True)
def _clean_breaker(monkeypatch: pytest.MonkeyPatch):
    # Deterministic knobs and a clean slate for every test.
    monkeypatch.delenv("K8S_CLUSTER_BREAKER_DISABLED", raising=False)
    monkeypatch.setenv("K8S_CLUSTER_BREAKER_THRESHOLD", "2")
    monkeypatch.setenv("K8S_CLUSTER_BREAKER_COOLDOWN_SECONDS", "120")
    cb.reset_cluster_breaker()
    yield
    cb.reset_cluster_breaker()


KEY = ("sub", "rg", "elb-cluster-01")


def test_breaker_closed_by_default_does_not_raise():
    cb.cluster_breaker_check(KEY)  # no state recorded → no raise


def test_breaker_trips_after_threshold_and_raises():
    cb.cluster_breaker_record_failure(KEY)
    # One failure (threshold 2) is below the trip point — still closed.
    cb.cluster_breaker_check(KEY)
    cb.cluster_breaker_record_failure(KEY)
    with pytest.raises(cb.ClusterApiUnreachable):
        cb.cluster_breaker_check(KEY)


def test_cluster_api_unreachable_is_a_connection_error():
    # Existing graceful handlers catch ConnectionError / OSError / Exception.
    assert issubclass(cb.ClusterApiUnreachable, ConnectionError)


def test_success_closes_the_breaker():
    cb.cluster_breaker_record_failure(KEY)
    cb.cluster_breaker_record_failure(KEY)
    with pytest.raises(cb.ClusterApiUnreachable):
        cb.cluster_breaker_check(KEY)
    cb.cluster_breaker_record_success(KEY)
    cb.cluster_breaker_check(KEY)  # closed again


def test_cooldown_elapsed_optimistically_closes(monkeypatch: pytest.MonkeyPatch):
    cb.cluster_breaker_record_failure(KEY)
    cb.cluster_breaker_record_failure(KEY)
    with pytest.raises(cb.ClusterApiUnreachable):
        cb.cluster_breaker_check(KEY)

    # Jump past the cooldown deadline via a fake monotonic clock.
    real = time.monotonic()
    monkeypatch.setattr(cb.time, "monotonic", lambda: real + 1000.0)
    cb.cluster_breaker_check(KEY)  # cooldown elapsed → entry dropped, no raise
    # And the entry really was dropped (next check still closed).
    cb.cluster_breaker_check(KEY)


def test_disabled_flag_is_a_total_noop(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("K8S_CLUSTER_BREAKER_DISABLED", "true")
    cb.cluster_breaker_record_failure(KEY)
    cb.cluster_breaker_record_failure(KEY)
    cb.cluster_breaker_record_failure(KEY)
    cb.cluster_breaker_check(KEY)  # never raises while disabled


def test_threshold_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("K8S_CLUSTER_BREAKER_THRESHOLD", "1")
    cb.cluster_breaker_record_failure(KEY)
    with pytest.raises(cb.ClusterApiUnreachable):
        cb.cluster_breaker_check(KEY)


def test_get_k8s_session_short_circuits_when_open(monkeypatch: pytest.MonkeyPatch):
    """An open breaker must raise before the pooled fast path or any ARM call."""

    from api.services.k8s import client as k8s_client

    k8s_client.reset_cluster_breaker()
    monkeypatch.setenv("K8S_CLUSTER_BREAKER_THRESHOLD", "1")

    sub, rg, cluster = "sub", "rg", "elb-cluster-01"
    key = cb.cluster_breaker_key(sub, rg, cluster)
    cb.cluster_breaker_record_failure(key)  # trip (threshold 1)

    # Poison the ARM credential fetch: if the breaker did NOT short-circuit,
    # this would be called and raise a different error.
    def _boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("ARM credential fetch should be short-circuited")

    monkeypatch.setattr(k8s_client, "_get_k8s_credential_material", _boom)

    with pytest.raises(cb.ClusterApiUnreachable):
        k8s_client._get_k8s_session(object(), sub, rg, cluster)

    cb.reset_cluster_breaker()
