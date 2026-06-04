"""Unit tests for the Gate B active-submission count (k8s_count_active_blast_submissions).

Responsibility: Lock the distinct-``elb-job-id`` counting rule over non-terminal
``app=finalizer`` Jobs, the phantom-slot grace guard, and the fail-closed contract
(a non-200 list RAISES) of ``api.services.k8s.blast_status``.
Edit boundaries: Pure unit tests with a stubbed ``_get_k8s_session`` /
``_namespace_or_default`` — no real K8s API.
Key entry points: ``test_counts_distinct_finalizers``, ``test_skips_terminal``,
``test_phantom_past_grace_not_counted``, ``test_phantom_within_grace_counted``,
``test_live_companion_counted``, ``test_non_200_fails_closed``.
Risky contracts: A read failure must RAISE (caller releases the Lease + requeues),
never return a low count that over-admits past the ceiling.
Validation: ``uv run pytest -q api/tests/test_blast_gate_b_count.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from api.services.k8s import blast_status
from api.services.k8s import monitoring as km


def _ts(age_seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=age_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _job(
    app: str,
    job_id: str,
    *,
    age_seconds: int = 5,
    succeeded: int = 0,
    failed: int = 0,
    conditions: list[tuple[str, str]] | None = None,
    completion_time: str | None = None,
) -> dict[str, Any]:
    status: dict[str, Any] = {"succeeded": succeeded, "failed": failed}
    if conditions is not None:
        status["conditions"] = [{"type": t, "status": s} for t, s in conditions]
    if completion_time is not None:
        status["completionTime"] = completion_time
    return {
        "metadata": {
            "labels": {"app": app, "elb-job-id": job_id},
            "creationTimestamp": _ts(age_seconds),
        },
        "status": status,
    }


class _FakeResponse:
    def __init__(self, status_code: int, items: list[dict[str, Any]] | None = None) -> None:
        self.status_code = status_code
        self._items = items or []

    def json(self) -> dict[str, Any]:
        return {"items": self._items}


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.closed = False

    def get(self, url: str, *, params: dict[str, Any], timeout: int) -> _FakeResponse:
        return self._response

    def close(self) -> None:
        self.closed = True


def _patch(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> None:
    session = _FakeSession(response)
    monkeypatch.setattr(
        km, "_get_k8s_session", lambda *a, **k: (session, "https://k8s.test")
    )
    monkeypatch.setattr(km, "_namespace_or_default", lambda *a, **k: "default")


def _count() -> int:
    return blast_status.k8s_count_active_blast_submissions(
        SimpleNamespace(),  # type: ignore[arg-type]
        "sub",
        "rg",
        "cluster",
        "default",
    )


def test_counts_distinct_finalizers(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        _job("finalizer", "jid-1"),
        _job("finalizer", "jid-1"),  # duplicate elb-job-id → still 1
        _job("finalizer", "jid-2"),
    ]
    _patch(monkeypatch, _FakeResponse(200, items))
    assert _count() == 2


def test_skips_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail-closed terminal test (round-3 M-B): only a DEFINITIVELY done finalizer
    # is dropped from the count — succeeded>0, a completionTime, or a
    # Complete/Failed condition. A bare failed>0 is NOT proof of termination.
    items = [
        _job("finalizer", "jid-1", succeeded=1),  # terminal (succeeded)
        _job("finalizer", "jid-2", failed=1, conditions=[("Failed", "True")]),  # terminal
        _job("finalizer", "jid-3"),  # active
    ]
    _patch(monkeypatch, _FakeResponse(200, items))
    assert _count() == 1


def test_retrying_finalizer_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    # A finalizer with failed>0 but NO terminal condition is still retrying
    # (backoffLimit not exhausted) and occupies a slot. The old failed>0
    # heuristic dropped it — under-counting and over-admitting past the ceiling
    # (round-3 M-B, fail-OPEN). It must now be counted (fail-closed).
    items = [_job("finalizer", "jid-1", failed=1)]  # young, no terminal condition
    _patch(monkeypatch, _FakeResponse(200, items))
    assert _count() == 1


def test_completion_time_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    # completionTime set (with no succeeded/failed counters) is also terminal.
    items = [_job("finalizer", "jid-1", completion_time=_ts(1))]
    _patch(monkeypatch, _FakeResponse(200, items))
    assert _count() == 0


def test_phantom_past_grace_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLAST_FINALIZER_GRACE_SECONDS", raising=False)  # 300
    items = [_job("finalizer", "jid-1", age_seconds=1000)]  # no companion, old
    _patch(monkeypatch, _FakeResponse(200, items))
    assert _count() == 0


def test_phantom_within_grace_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLAST_FINALIZER_GRACE_SECONDS", raising=False)  # 300
    items = [_job("finalizer", "jid-1", age_seconds=10)]  # no companion, young
    _patch(monkeypatch, _FakeResponse(200, items))
    assert _count() == 1


def test_live_companion_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        _job("finalizer", "jid-1", age_seconds=1000),  # old but has companion
        _job("blast", "jid-1", age_seconds=900),
    ]
    _patch(monkeypatch, _FakeResponse(200, items))
    assert _count() == 1


def test_non_200_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _FakeResponse(500))
    with pytest.raises(RuntimeError):
        _count()


def _finalizer_without_job_id(uid: str, *, age_seconds: int = 5) -> dict[str, Any]:
    return {
        "metadata": {
            "labels": {"app": "finalizer"},  # NO elb-job-id label
            "uid": uid,
            "creationTimestamp": _ts(age_seconds),
        },
        "status": {"succeeded": 0, "failed": 0},
    }


def test_label_less_finalizers_counted_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-terminal finalizer with no elb-job-id label still occupies a slot;
    # counting it fail-closed (one per distinct Job) prevents over-admission
    # past the ceiling (critique M15). Two distinct label-less finalizers +
    # one labelled = 3.
    items = [
        _finalizer_without_job_id("uid-a"),
        _finalizer_without_job_id("uid-b"),
        _job("finalizer", "jid-1"),
    ]
    _patch(monkeypatch, _FakeResponse(200, items))
    assert _count() == 3
