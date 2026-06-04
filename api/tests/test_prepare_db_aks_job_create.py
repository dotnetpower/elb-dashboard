"""Tests for `_create_job_if_absent` cancel-then-resubmit race handling.

Responsibility: Exercise the terminating-Job detection in
    `api.services.k8s.prepare_db_jobs._create_job_if_absent` so a
    cancel-then-Get within the same NCBI snapshot day creates a fresh Job
    instead of polling the dying one.
Edit boundaries: Pure unit test with a scripted fake session; never reaches
    a real Kubernetes API. Keep `time.sleep` patched to a no-op so the wait
    loop runs instantly.
Key entry points: `test_*` functions below.
Risky contracts: A healthy existing Job (no `deletionTimestamp`) must still
    report `existing`; a terminating Job must be waited out then `created`;
    a never-collected terminating Job must time out to `error`.
Validation: `uv run pytest -q api/tests/test_prepare_db_aks_job_create.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.k8s import prepare_db_jobs


class _Resp:
    def __init__(
        self, status_code: int, body: dict[str, Any] | None = None, text: str = ""
    ) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._body


class _ScriptedSession:
    """Returns queued GET responses in order; records POST calls."""

    def __init__(self, get_responses: list[_Resp], post_response: _Resp) -> None:
        self._get_responses = list(get_responses)
        self._post_response = post_response
        self.get_calls = 0
        self.post_calls = 0

    def get(self, _url: str, timeout: int = 10) -> _Resp:
        self.get_calls += 1
        if self._get_responses:
            return self._get_responses.pop(0)
        # Default to the last scripted response if the loop polls more than
        # expected.
        return _Resp(404)

    def post(self, _url: str, json: Any = None, timeout: int = 10) -> _Resp:
        self.post_calls += 1
        return self._post_response


_MANIFEST = {"metadata": {"name": "prepare-db-nt-202606040101", "namespace": "default"}}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prepare_db_jobs.time, "sleep", lambda *_a, **_kw: None)


def test_absent_job_is_created() -> None:
    session = _ScriptedSession([_Resp(404)], _Resp(201))
    out = prepare_db_jobs._create_job_if_absent(session, "https://aks", _MANIFEST)
    assert out["status"] == "created"
    assert session.post_calls == 1


def test_healthy_existing_job_reports_existing() -> None:
    # 200 with no deletionTimestamp = a genuine in-flight duplicate.
    session = _ScriptedSession([_Resp(200, {"metadata": {"name": "x"}})], _Resp(201))
    out = prepare_db_jobs._create_job_if_absent(session, "https://aks", _MANIFEST)
    assert out["status"] == "existing"
    assert session.post_calls == 0


def test_terminating_job_is_waited_out_then_created() -> None:
    # First GET: terminating (deletionTimestamp present). Second GET: gone
    # (404). Then create succeeds.
    session = _ScriptedSession(
        [
            _Resp(200, {"metadata": {"deletionTimestamp": "2026-06-04T01:02:03Z"}}),
            _Resp(404),
        ],
        _Resp(201),
    )
    out = prepare_db_jobs._create_job_if_absent(
        session, "https://aks", _MANIFEST, terminating_wait_seconds=10.0
    )
    assert out["status"] == "created"
    assert session.get_calls == 2
    assert session.post_calls == 1


def test_terminating_job_that_never_clears_times_out() -> None:
    # GET always returns a terminating Job → loop exhausts the wait budget
    # and reports an honest error instead of spawning against the zombie.
    session = _ScriptedSession(
        [_Resp(200, {"metadata": {"deletionTimestamp": "2026-06-04T01:02:03Z"}})] * 5,
        _Resp(201),
    )
    out = prepare_db_jobs._create_job_if_absent(
        session, "https://aks", _MANIFEST, terminating_wait_seconds=0.0
    )
    assert out["status"] == "error"
    assert out["terminating"] is True
    assert session.post_calls == 0


def test_create_409_reevaluates_and_reports_existing_when_healthy() -> None:
    # 404 GET → POST 409 (a peer raced us) → second GET shows a healthy Job →
    # reported as existing (no duplicate spawn).
    session = _ScriptedSession(
        [_Resp(404), _Resp(200, {"metadata": {"name": "x"}})],
        _Resp(409),
    )
    out = prepare_db_jobs._create_job_if_absent(
        session, "https://aks", _MANIFEST, terminating_wait_seconds=10.0
    )
    assert out["status"] == "existing"
    assert session.post_calls == 1


def test_get_error_status_is_surfaced() -> None:
    session = _ScriptedSession([_Resp(500, text="boom")], _Resp(201))
    out = prepare_db_jobs._create_job_if_absent(session, "https://aks", _MANIFEST)
    assert out["status"] == "error"
    assert out["status_code"] == 500
