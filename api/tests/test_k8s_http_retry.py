"""Tests for bounded transient Kubernetes GET retries.

Responsibility: Verify Retry-After handling and one-retry bounds for idempotent K8s reads.
Edit boundaries: Pure fake-session tests; never open a network connection.
Key entry points: `test_get_with_transient_retry_recovers_once`.
Risky contracts: Only GET is exposed; retries must remain capped at one.
Validation: `uv run pytest -q api/tests/test_k8s_http_retry.py`.
"""

from __future__ import annotations

from typing import Any


class _Response:
    def __init__(self, status_code: int, retry_after: str = "") -> None:
        self.status_code = status_code
        self.headers = {"Retry-After": retry_after} if retry_after else {}


class _Session:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append((url, kwargs))
        status = self.statuses.pop(0)
        return _Response(status, retry_after="9" if status == 429 else "")


def test_get_with_transient_retry_recovers_once(monkeypatch) -> None:
    from api.services.k8s import client

    session = _Session([429, 200])
    sleeps: list[float] = []
    monkeypatch.setattr(client.time, "sleep", sleeps.append)

    response = client.get_with_transient_retry(
        session,
        "https://k8s/jobs",
        params={"labelSelector": "app=blast"},
    )

    assert response.status_code == 200
    assert len(session.calls) == 2
    assert sleeps == [2.0]


def test_get_with_transient_retry_stops_after_second_failure(monkeypatch) -> None:
    from api.services.k8s import client

    session = _Session([503, 503])
    monkeypatch.setattr(client.time, "sleep", lambda _seconds: None)

    response = client.get_with_transient_retry(session, "https://k8s/jobs")

    assert response.status_code == 503
    assert len(session.calls) == 2
