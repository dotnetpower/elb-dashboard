"""Unit tests for the Gate A submit Lease primitives (coordination.k8s.io).

Responsibility: Lock the acquire / CAS-conflict / expiry-takeover / same-holder-
renew / conditional-release behaviour of ``api.services.k8s.submit_lease`` and the
load-bearing BUSY-vs-API-error distinction, with a stubbed ``_get_k8s_session`` so
no real Kubernetes API is contacted.
Edit boundaries: Pure unit tests — no real credentials, no real K8s API.
Key entry points: ``test_acquire_creates_when_absent``,
``test_acquire_busy_when_live_other_holder``, ``test_acquire_takes_over_expired``,
``test_acquire_renews_same_holder``, ``test_acquire_cas_conflict_is_busy``,
``test_acquire_api_error_raises``, ``test_release_clears_when_holder_matches``,
``test_release_skips_when_holder_differs``, ``test_invalid_namespace_raises``.
Risky contracts: A 409 (CAS loss) is BUSY (``None``); a 5xx / transport failure is
``SubmitLeaseApiError`` — conflating them would silently requeue forever.
Validation: ``uv run pytest -q api/tests/test_blast_submit_lease.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from api.services.k8s import submit_lease as sl


class _FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = str(self._body)

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeSession:
    def __init__(self) -> None:
        self.get_responses: list[_FakeResponse] = []
        self.post_responses: list[_FakeResponse] = []
        self.put_responses: list[_FakeResponse] = []
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def _pop(self, queue: list[_FakeResponse], method: str, url: str) -> _FakeResponse:
        self.calls.append((method, url))
        if not queue:
            raise AssertionError(f"unexpected {method} {url}")
        return queue.pop(0)

    def get(self, url: str, *, timeout: int) -> _FakeResponse:
        return self._pop(self.get_responses, "GET", url)

    def post(self, url: str, *, json: dict[str, Any], timeout: int) -> _FakeResponse:
        return self._pop(self.post_responses, "POST", url)

    def put(self, url: str, *, json: dict[str, Any], timeout: int) -> _FakeResponse:
        return self._pop(self.put_responses, "PUT", url)

    def close(self) -> None:
        self.closed = True


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    from api.services.k8s import credentials as creds

    def _fake(*_a: Any, **_k: Any) -> tuple[_FakeSession, str]:
        return session, "https://k8s.test"

    monkeypatch.setattr(creds, "_get_k8s_session", _fake)


def _lease_body(holder: str, *, age_seconds: int, ttl: int = 900) -> dict[str, Any]:
    renew = datetime.now(UTC) - timedelta(seconds=age_seconds)
    stamp = renew.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    return {
        "metadata": {"name": "elb-blast-submit-default", "resourceVersion": "42"},
        "spec": {
            "holderIdentity": holder,
            "leaseDurationSeconds": ttl,
            "acquireTime": stamp,
            "renewTime": stamp,
        },
    }


def _acquire(session: _FakeSession, holder: str) -> Any:
    return sl.k8s_acquire_submit_lease(
        SimpleNamespace(),  # type: ignore[arg-type]
        "sub",
        "rg",
        "cluster",
        namespace="default",
        holder=holder,
    )


def test_acquire_creates_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    session.get_responses = [_FakeResponse(404)]
    session.post_responses = [_FakeResponse(201)]
    _patch_session(monkeypatch, session)
    handle = _acquire(session, "dashboard-aaa")
    assert handle is not None
    assert handle.holder == "dashboard-aaa"
    assert session.closed is True


def test_acquire_busy_when_live_other_holder(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    session.get_responses = [_FakeResponse(200, _lease_body("openapi-bbb", age_seconds=5))]
    _patch_session(monkeypatch, session)
    assert _acquire(session, "dashboard-aaa") is None


def test_acquire_takes_over_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    session.get_responses = [
        _FakeResponse(200, _lease_body("openapi-bbb", age_seconds=2000, ttl=900))
    ]
    session.put_responses = [_FakeResponse(200)]
    _patch_session(monkeypatch, session)
    handle = _acquire(session, "dashboard-aaa")
    assert handle is not None
    assert handle.holder == "dashboard-aaa"


def test_acquire_renews_same_holder(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    session.get_responses = [
        _FakeResponse(200, _lease_body("dashboard-aaa", age_seconds=5))
    ]
    session.put_responses = [_FakeResponse(200)]
    _patch_session(monkeypatch, session)
    handle = _acquire(session, "dashboard-aaa")
    assert handle is not None


def test_acquire_cas_conflict_is_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    session.get_responses = [
        _FakeResponse(200, _lease_body("", age_seconds=5))  # empty holder → takeover
    ]
    session.put_responses = [_FakeResponse(409)]  # lost CAS race
    _patch_session(monkeypatch, session)
    assert _acquire(session, "dashboard-aaa") is None


def test_acquire_create_conflict_is_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    session.get_responses = [_FakeResponse(404)]
    session.post_responses = [_FakeResponse(409)]
    _patch_session(monkeypatch, session)
    assert _acquire(session, "dashboard-aaa") is None


def test_acquire_api_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    session.get_responses = [_FakeResponse(500)]
    _patch_session(monkeypatch, session)
    with pytest.raises(sl.SubmitLeaseApiError):
        _acquire(session, "dashboard-aaa")


def test_acquire_forbidden_raises_with_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    # admin kubeconfig bypasses K8s RBAC, so a 401/403 is a credential/network
    # fault the dashboard cannot retry away — surface it loudly (critique H7).
    for code in (401, 403):
        session = _FakeSession()
        session.get_responses = [_FakeResponse(code)]
        _patch_session(monkeypatch, session)
        with pytest.raises(sl.SubmitLeaseApiError) as excinfo:
            _acquire(session, "dashboard-aaa")
        assert "forbidden" in str(excinfo.value).lower()


def test_acquire_unparseable_renewtime_is_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    # A PRESENT-but-unparseable renewTime must be treated as held (fail-closed),
    # NOT taken over — otherwise two paths submit concurrently (critique M16).
    body = _lease_body("openapi-bbb", age_seconds=5)
    body["spec"]["renewTime"] = "not-a-timestamp"
    session = _FakeSession()
    session.get_responses = [_FakeResponse(200, body)]
    _patch_session(monkeypatch, session)
    assert _acquire(session, "dashboard-aaa") is None


def test_acquire_absent_renewtime_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    # A truly absent renewTime key → cannot prove the Lease is alive → available.
    body = _lease_body("openapi-bbb", age_seconds=5)
    body["spec"].pop("renewTime", None)
    session = _FakeSession()
    session.get_responses = [_FakeResponse(200, body)]
    session.put_responses = [_FakeResponse(200)]
    _patch_session(monkeypatch, session)
    handle = _acquire(session, "dashboard-aaa")
    assert handle is not None
    assert handle.holder == "dashboard-aaa"


def test_acquire_transport_exception_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services.k8s import credentials as creds

    class _BoomSession:
        def get(self, *_a: Any, **_k: Any) -> Any:
            raise ConnectionError("apiserver unreachable")

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        creds, "_get_k8s_session", lambda *a, **k: (_BoomSession(), "https://k8s.test")
    )
    with pytest.raises(sl.SubmitLeaseApiError):
        _acquire(_FakeSession(), "dashboard-aaa")


def test_invalid_namespace_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    _patch_session(monkeypatch, session)
    with pytest.raises(sl.SubmitLeaseApiError):
        sl.k8s_acquire_submit_lease(
            SimpleNamespace(),  # type: ignore[arg-type]
            "sub",
            "rg",
            "cluster",
            namespace="Bad NS",
            holder="dashboard-aaa",
        )


def test_release_clears_when_holder_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    session.get_responses = [
        _FakeResponse(200, _lease_body("dashboard-aaa", age_seconds=5))
    ]
    session.put_responses = [_FakeResponse(200)]
    _patch_session(monkeypatch, session)
    handle = sl.SubmitLeaseHandle(
        name="elb-blast-submit-default", namespace="default", holder="dashboard-aaa"
    )
    sl.k8s_release_submit_lease(SimpleNamespace(), "sub", "rg", "cluster", handle)  # type: ignore[arg-type]
    assert any(m == "PUT" for m, _ in session.calls)


def test_release_skips_when_holder_differs(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    session.get_responses = [
        _FakeResponse(200, _lease_body("openapi-ccc", age_seconds=5))
    ]
    _patch_session(monkeypatch, session)
    handle = sl.SubmitLeaseHandle(
        name="elb-blast-submit-default", namespace="default", holder="dashboard-aaa"
    )
    sl.k8s_release_submit_lease(SimpleNamespace(), "sub", "rg", "cluster", handle)  # type: ignore[arg-type]
    # newer holder took over → no PUT issued (do not clobber)
    assert all(m != "PUT" for m, _ in session.calls)


def test_release_missing_lease_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    session.get_responses = [_FakeResponse(404)]
    _patch_session(monkeypatch, session)
    handle = sl.SubmitLeaseHandle(
        name="elb-blast-submit-default", namespace="default", holder="dashboard-aaa"
    )
    sl.k8s_release_submit_lease(SimpleNamespace(), "sub", "rg", "cluster", handle)  # type: ignore[arg-type]
    assert all(m != "PUT" for m, _ in session.calls)


def test_holder_identity_globally_unique() -> None:
    a = sl.new_holder_identity("dashboard")
    b = sl.new_holder_identity("dashboard")
    assert a != b
    assert a.startswith("dashboard-")
