"""Tests for bounded foreground deletion of prepare-db Kubernetes Jobs.

Responsibility: Verify user-cancel Job deletion waits for dependent pod removal while
    background worker cleanup remains non-blocking and idempotent.
Edit boundaries: Pure scripted Kubernetes-session tests for `delete_prepare_db_job`;
    route metadata ownership and task cancellation live in their focused test modules.
Key entry points: `test_*` functions.
Risky contracts: A waited delete reports success only after Job 404 and deletes the
    ConfigMap afterward; timeout preserves the ConfigMap and returns partial status.
Validation: `uv run pytest -q api/tests/test_prepare_db_aks_job_delete.py`.
"""

from __future__ import annotations

import pytest
from api.services.k8s import prepare_db_jobs


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _Session:
    def __init__(self, get_statuses: list[int]) -> None:
        self.get_statuses = list(get_statuses)
        self.delete_calls: list[tuple[str, dict[str, str] | None]] = []

    def delete(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        timeout: int,
    ) -> _Response:
        del timeout
        self.delete_calls.append((url, params))
        return _Response(200)

    def get(self, _url: str, *, timeout: int) -> _Response:
        del timeout
        return _Response(self.get_statuses.pop(0) if self.get_statuses else 200)

    def close(self) -> None:
        return None


def _install(monkeypatch: pytest.MonkeyPatch, session: _Session) -> None:
    monkeypatch.setattr(
        prepare_db_jobs,
        "_get_k8s_session",
        lambda *_args, **_kwargs: (session, "https://aks"),
    )
    monkeypatch.setattr(prepare_db_jobs.time, "sleep", lambda _seconds: None)


def test_foreground_delete_waits_for_job_absence_before_configmap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([200, 404])
    _install(monkeypatch, session)

    result = prepare_db_jobs.delete_prepare_db_job(
        object(),
        "sub",
        "rg",
        "cluster",
        namespace="default",
        job_name="prepare-core",
        configmap_name="prepare-core",
        wait_for_absence_seconds=60,
    )

    assert result["status"] == "deleted"
    assert result["job"]["absent"] is True
    assert result["job"]["waited"] is True
    assert session.delete_calls[0][1] == {"propagationPolicy": "Foreground"}
    assert session.delete_calls[1][0].endswith("/configmaps/prepare-core")


def test_foreground_delete_timeout_defers_configmap_and_reports_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([200])
    _install(monkeypatch, session)
    monotonic_values = iter([0.0, 0.0, 61.0])
    monkeypatch.setattr(prepare_db_jobs.time, "monotonic", lambda: next(monotonic_values))

    result = prepare_db_jobs.delete_prepare_db_job(
        object(),
        "sub",
        "rg",
        "cluster",
        namespace="default",
        job_name="prepare-core",
        configmap_name="prepare-core",
        wait_for_absence_seconds=60,
    )

    assert result["status"] == "partial"
    assert result["job"]["wait_timed_out"] is True
    assert result["configmap"]["deferred"] is True
    assert len(session.delete_calls) == 1


def test_default_delete_remains_background_and_non_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([])
    _install(monkeypatch, session)

    result = prepare_db_jobs.delete_prepare_db_job(
        object(),
        "sub",
        "rg",
        "cluster",
        namespace="default",
        job_name="prepare-core",
        configmap_name="prepare-core",
    )

    assert result["status"] == "deleted"
    assert result["job"]["waited"] is False
    assert session.delete_calls[0][1] == {"propagationPolicy": "Background"}
    assert len(session.delete_calls) == 2
