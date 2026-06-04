"""Unit tests for the combined k8s admission gate (Gate A + Gate B).

Responsibility: Lock the admission verdicts of ``api.services.blast.k8s_gate`` —
admit, submit-slot-busy, lease-api-error, capacity-full (Lease released),
capacity-count-error fail-closed (Lease released) — and the bounded inline-wait
helper used by the split fan-out.
Edit boundaries: Pure unit tests. Lease HTTP behaviour is covered in
``test_blast_submit_lease.py``; Gate B counting in ``test_blast_gate_b_count.py``.
Key entry points: ``test_admits_when_lease_and_capacity_ok``,
``test_busy_lease_is_retryable``, ``test_lease_api_error_is_error``,
``test_capacity_full_releases_lease``, ``test_count_error_fails_closed``,
``test_wait_returns_lease_when_admitted``, ``test_wait_raises_on_deadline``,
``test_wait_raises_on_api_error``.
Risky contracts: On ANY deny after the Lease was acquired, the Lease MUST be
released here — never leaked to TTL.
Validation: ``uv run pytest -q api/tests/test_blast_k8s_gate.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from api.services.blast import k8s_gate
from api.services.k8s import blast_status
from api.services.k8s.submit_lease import SubmitLeaseApiError, SubmitLeaseHandle

_HANDLE = SubmitLeaseHandle(
    name="elb-blast-submit-default", namespace="default", holder="dashboard-aaa"
)


def _acquire() -> k8s_gate.K8sAdmission:
    return k8s_gate.acquire_k8s_admission(
        SimpleNamespace(),  # type: ignore[arg-type]
        "sub",
        "rg",
        "cluster",
        namespace="default",
        job_id="job-1",
    )


def test_admits_when_lease_and_capacity_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(k8s_gate, "k8s_acquire_submit_lease", lambda *a, **k: _HANDLE)
    release = MagicMock()
    monkeypatch.setattr(k8s_gate, "k8s_release_submit_lease", release)
    monkeypatch.setattr(blast_status, "k8s_count_active_blast_submissions", lambda *a, **k: 0)
    verdict = _acquire()
    assert verdict.admitted is True
    assert verdict.lease is _HANDLE
    release.assert_not_called()


def test_busy_lease_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(k8s_gate, "k8s_acquire_submit_lease", lambda *a, **k: None)
    verdict = _acquire()
    assert verdict.admitted is False
    assert verdict.reason == k8s_gate.REASON_SUBMIT_SLOT_BUSY
    assert verdict.retryable is True
    assert verdict.error is False


def test_lease_api_error_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise SubmitLeaseApiError("apiserver down")

    monkeypatch.setattr(k8s_gate, "k8s_acquire_submit_lease", _boom)
    verdict = _acquire()
    assert verdict.admitted is False
    assert verdict.error is True
    assert verdict.reason == k8s_gate.REASON_LEASE_API_ERROR


def test_capacity_full_releases_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLAST_MAX_RUN_CONCURRENCY", raising=False)  # ceiling = 3
    monkeypatch.setattr(k8s_gate, "k8s_acquire_submit_lease", lambda *a, **k: _HANDLE)
    release = MagicMock()
    monkeypatch.setattr(k8s_gate, "k8s_release_submit_lease", release)
    monkeypatch.setattr(blast_status, "k8s_count_active_blast_submissions", lambda *a, **k: 3)
    verdict = _acquire()
    assert verdict.admitted is False
    assert verdict.reason == k8s_gate.REASON_CAPACITY_FULL
    assert verdict.retryable is True
    assert verdict.active_count == 3
    release.assert_called_once()


def test_count_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(k8s_gate, "k8s_acquire_submit_lease", lambda *a, **k: _HANDLE)
    release = MagicMock()
    monkeypatch.setattr(k8s_gate, "k8s_release_submit_lease", release)

    def _boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("jobs API error: 500")

    monkeypatch.setattr(blast_status, "k8s_count_active_blast_submissions", _boom)
    verdict = _acquire()
    assert verdict.admitted is False
    assert verdict.reason == k8s_gate.REASON_CAPACITY_COUNT_ERROR
    assert verdict.retryable is True
    release.assert_called_once()


def test_wait_returns_lease_when_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _acquire_seq(*_a: Any, **_k: Any) -> k8s_gate.K8sAdmission:
        calls["n"] += 1
        if calls["n"] < 3:
            return k8s_gate.K8sAdmission(
                admitted=False, reason=k8s_gate.REASON_SUBMIT_SLOT_BUSY, retryable=True
            )
        return k8s_gate.K8sAdmission(admitted=True, lease=_HANDLE)

    monkeypatch.setattr(k8s_gate, "acquire_k8s_admission", _acquire_seq)
    slept: list[float] = []
    lease = k8s_gate.wait_for_k8s_admission(
        SimpleNamespace(),  # type: ignore[arg-type]
        "sub",
        "rg",
        "cluster",
        namespace="default",
        job_id="child-1",
        deadline_ts=1e18,
        sleep=lambda s: slept.append(s),
    )
    assert lease is _HANDLE
    assert calls["n"] == 3
    assert len(slept) == 2


def test_wait_raises_on_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        k8s_gate,
        "acquire_k8s_admission",
        lambda *a, **k: k8s_gate.K8sAdmission(
            admitted=False, reason=k8s_gate.REASON_CAPACITY_FULL, retryable=True
        ),
    )
    with pytest.raises(k8s_gate.K8sGateWaitTimeout):
        k8s_gate.wait_for_k8s_admission(
            SimpleNamespace(),  # type: ignore[arg-type]
            "sub",
            "rg",
            "cluster",
            namespace="default",
            job_id="child-1",
            deadline_ts=0.0,  # already past
            sleep=lambda _s: None,
        )


def test_wait_raises_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        k8s_gate,
        "acquire_k8s_admission",
        lambda *a, **k: k8s_gate.K8sAdmission(
            admitted=False, reason=k8s_gate.REASON_LEASE_API_ERROR, error=True
        ),
    )
    with pytest.raises(SubmitLeaseApiError):
        k8s_gate.wait_for_k8s_admission(
            SimpleNamespace(),  # type: ignore[arg-type]
            "sub",
            "rg",
            "cluster",
            namespace="default",
            job_id="child-1",
            deadline_ts=1e18,
            sleep=lambda _s: None,
        )
