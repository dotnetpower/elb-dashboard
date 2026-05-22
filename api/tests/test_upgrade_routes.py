"""HTTP-level tests for the read-only upgrade routes.

Module summary: Drives the FastAPI app through `TestClient` with
`AUTH_DEV_BYPASS=true` and the in-memory upgrade-state backend so the
status / candidates / check endpoints can be exercised end-to-end without
network or Azure.

Responsibility: Verify routing, auth gating, payload shapes, and the
  synchronous `check` endpoint's state-row mutation.
Edit boundaries: New endpoints land here; their service-layer behaviour
  is covered by the dedicated unit tests next door.
Key entry points: Test functions for status, candidates (configured /
  unconfigured / error), check.
Risky contracts: Confirms anonymous requests are rejected so PR1 cannot
  regress the auth gate.
Validation: `uv run pytest -q api/tests/test_upgrade_routes.py`.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from api.services.upgrade import remote_tags, state
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    # AUTH_DEV_BYPASS synthesises an identity with this oid (see api/auth.py);
    # admin routes only pass when that oid is in the allowlist.
    monkeypatch.setenv(
        "UPGRADE_ADMIN_OIDS", "00000000-0000-0000-0000-000000000000"
    )
    state.set_backend(state.InMemoryBackend())

    from api.services.upgrade import build_logs, history

    build_logs.set_backend(build_logs.InMemoryBuildLogBackend())
    history.set_backend(history.InMemoryHistoryBackend())
    from api.main import app
    from api.routes.upgrade import reset_check_throttle_for_tests

    reset_check_throttle_for_tests()
    with TestClient(app) as c:
        yield c
    reset_check_throttle_for_tests()
    history.set_backend(None)
    build_logs.set_backend(None)
    state.set_backend(None)


def test_status_returns_defaults(client: TestClient) -> None:
    resp = client.get("/api/upgrade/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == state.STATE_IDLE
    assert body["running_version"] == ""
    assert body["current_images"] == {}
    assert body["rollback_target"] == {}
    assert "etag" not in body


def test_status_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    state.set_backend(state.InMemoryBackend())
    try:
        from api.main import app

        with TestClient(app) as c:
            resp = c.get("/api/upgrade/status")
        assert resp.status_code == 401
    finally:
        state.set_backend(None)


def test_candidates_returns_unconfigured_when_remote_unset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(remote_tags.UPGRADE_GIT_REMOTE_ENV, raising=False)
    resp = client.get("/api/upgrade/candidates")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["remote"] is None
    assert body["candidates"] == []


def test_candidates_returns_filtered_list(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        remote_tags.UPGRADE_GIT_REMOTE_ENV, "https://example.test/foo.git"
    )
    # Seed running_version so filtering kicks in.
    state.update_state(lambda s: setattr(s, "running_version", "0.2.0"))

    fake = [
        remote_tags.RemoteTag(name="0.4.1", raw_ref="refs/tags/v0.4.1", commit_sha="d" * 40),
        remote_tags.RemoteTag(name="0.3.0", raw_ref="refs/tags/v0.3.0", commit_sha="c" * 40),
        remote_tags.RemoteTag(name="0.2.0", raw_ref="refs/tags/v0.2.0", commit_sha="b" * 40),
        remote_tags.RemoteTag(name="0.1.0", raw_ref="refs/tags/v0.1.0", commit_sha="a" * 40),
    ]
    monkeypatch.setattr(
        "api.services.upgrade.remote_tags.fetch_release_tags",
        lambda _url: fake,
    )

    resp = client.get("/api/upgrade/candidates")
    body = resp.json()
    assert body["configured"] is True
    assert body["remote"] == "https://example.test/foo.git"
    assert body["running_version"] == "0.2.0"
    assert [c["name"] for c in body["candidates"]] == ["0.4.1", "0.3.0"]


def test_candidates_surfaces_remote_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        remote_tags.UPGRADE_GIT_REMOTE_ENV, "https://example.test/foo.git"
    )

    def boom(_url: str) -> list[remote_tags.RemoteTag]:
        raise remote_tags.RemoteTagsError("simulated failure")

    monkeypatch.setattr(
        "api.services.upgrade.remote_tags.fetch_release_tags", boom
    )

    resp = client.get("/api/upgrade/candidates")
    body = resp.json()
    assert body["configured"] is True
    assert body["candidates"] == []
    assert "simulated failure" in body["error"]


def test_check_updates_state_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        remote_tags.UPGRADE_GIT_REMOTE_ENV, "https://example.test/foo.git"
    )
    fake = [
        remote_tags.RemoteTag(name="0.4.0", raw_ref="refs/tags/v0.4.0", commit_sha="f" * 40),
    ]
    monkeypatch.setattr(
        "api.services.upgrade.remote_tags.fetch_release_tags",
        lambda _url: fake,
    )

    resp = client.post("/api/upgrade/check")
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_version"] == "0.4.0"
    assert body["latest_sha"] == "f" * 40
    assert body["git_remote"] == "https://example.test/foo.git"
    assert body["latest_checked_at"]

    # Persisted across reads.
    again = client.get("/api/upgrade/status").json()
    assert again["latest_version"] == "0.4.0"


def test_check_clears_latest_when_remote_unset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(remote_tags.UPGRADE_GIT_REMOTE_ENV, raising=False)
    state.update_state(lambda s: (setattr(s, "latest_version", "0.9.0"), None)[-1])

    resp = client.post("/api/upgrade/check")
    body = resp.json()
    assert body["latest_version"] == ""
    assert body["git_remote"] == ""


def test_check_marks_remote_failure_without_setting_error_field(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        remote_tags.UPGRADE_GIT_REMOTE_ENV, "https://example.test/foo.git"
    )

    def boom(_url: str) -> list[remote_tags.RemoteTag]:
        raise remote_tags.RemoteTagsError("network down")

    monkeypatch.setattr(
        "api.services.upgrade.remote_tags.fetch_release_tags", boom
    )

    resp = client.post("/api/upgrade/check")
    body = resp.json()
    # PR1 intentionally omits an `error` field from the state row; the SPA
    # treats an empty `latest_version` plus a recent `latest_checked_at`
    # as a soft failure. PR3 introduces a separate execution-error field.
    assert "error" not in body
    assert body["latest_version"] == ""
    assert body["git_remote"] == "https://example.test/foo.git"
    assert body["latest_checked_at"]


def test_check_is_throttled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        remote_tags.UPGRADE_GIT_REMOTE_ENV, "https://example.test/foo.git"
    )
    monkeypatch.setattr(
        "api.services.upgrade.remote_tags.fetch_release_tags",
        lambda _url: [],
    )

    first = client.post("/api/upgrade/check")
    assert first.status_code == 200
    second = client.post("/api/upgrade/check")
    assert second.status_code == 429
    assert second.headers.get("Retry-After")


def test_candidates_masks_credentialed_remote(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        remote_tags.UPGRADE_GIT_REMOTE_ENV,
        "https://x-access-token:supersecret@example.test/foo.git",
    )
    monkeypatch.setattr(
        "api.services.upgrade.remote_tags.fetch_release_tags",
        lambda _url: [],
    )

    resp = client.get("/api/upgrade/candidates")
    body = resp.json()
    assert body["remote"] == "https://example.test/foo.git"
    assert "supersecret" not in resp.text


def test_start_requires_confirm_downtime(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        remote_tags.UPGRADE_GIT_REMOTE_ENV, "https://example.test/foo.git"
    )
    resp = client.post(
        "/api/upgrade/start",
        json={"target_version": "0.3.0", "confirm_downtime": False},
    )
    assert resp.status_code == 422


def test_start_enforces_admin_role(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        remote_tags.UPGRADE_GIT_REMOTE_ENV, "https://example.test/foo.git"
    )
    monkeypatch.delenv("UPGRADE_ADMIN_OIDS", raising=False)
    resp = client.post(
        "/api/upgrade/start",
        json={"target_version": "0.3.0", "confirm_downtime": True},
    )
    assert resp.status_code == 403


def test_start_queues_and_enqueues(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        remote_tags.UPGRADE_GIT_REMOTE_ENV, "https://example.test/foo.git"
    )
    submitted: list[str] = []

    class _NoOpResult:
        id = "celery-task-id"

    def _fake_delay(*args: object) -> _NoOpResult:
        submitted.append("called")
        return _NoOpResult()

    monkeypatch.setattr(
        "api.tasks.upgrade.execute_upgrade.delay", _fake_delay
    )

    resp = client.post(
        "/api/upgrade/start",
        json={"target_version": "0.4.1", "confirm_downtime": True},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["state"] == "queued"
    assert body["target_version"] == "0.4.1"
    assert body["job_id"]
    assert submitted == ["called"]


def test_start_second_call_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        remote_tags.UPGRADE_GIT_REMOTE_ENV, "https://example.test/foo.git"
    )
    monkeypatch.setattr(
        "api.tasks.upgrade.execute_upgrade.delay", lambda *args: None
    )
    first = client.post(
        "/api/upgrade/start",
        json={"target_version": "0.4.1", "confirm_downtime": True},
    )
    assert first.status_code == 202
    second = client.post(
        "/api/upgrade/start",
        json={"target_version": "0.4.2", "confirm_downtime": True},
    )
    assert second.status_code == 409


def test_build_log_endpoint_returns_blob(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services.upgrade import build_logs as _logs

    writer = _logs.open_writer("jobABCD", "api")
    writer.write_line("hello")
    writer.flush()

    resp = client.get("/api/upgrade/jobs/jobABCD/build-log/api")
    assert resp.status_code == 200
    assert resp.text.startswith("hello")


def test_build_log_endpoint_404_for_missing(client: TestClient) -> None:
    resp = client.get("/api/upgrade/jobs/jobABCD/build-log/api")
    assert resp.status_code == 404


def test_build_log_endpoint_rejects_invalid_component(client: TestClient) -> None:
    resp = client.get("/api/upgrade/jobs/jobABCD/build-log/redis")
    assert resp.status_code == 400


def test_build_log_endpoint_requires_admin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UPGRADE_ADMIN_OIDS", raising=False)
    resp = client.get("/api/upgrade/jobs/jobABCD/build-log/api")
    assert resp.status_code == 403


def _seed_rollback_snapshot() -> None:
    import json as _json

    state.update_state(
        lambda s: (
            setattr(s, "state", state.STATE_SUCCEEDED),
            setattr(
                s,
                "rollback_target_json",
                _json.dumps(
                    {
                        "api": "myacr.azurecr.io/elb-api:v0.2.1",
                        "frontend": "myacr.azurecr.io/elb-frontend:v0.2.1",
                        "terminal": "myacr.azurecr.io/elb-terminal:v0.2.1",
                    }
                ),
            ),
        )[-1]
    )


def test_rollback_requires_admin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UPGRADE_ADMIN_OIDS", raising=False)
    resp = client.post("/api/upgrade/rollback")
    assert resp.status_code == 403


def test_rollback_refuses_without_snapshot(client: TestClient) -> None:
    resp = client.post("/api/upgrade/rollback")
    assert resp.status_code == 409


def test_rollback_happy_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_rollback_snapshot()
    # Stub aca_template surface used by start_rollback_inline. The route
    # imports `start_rollback_inline` by name, so we monkeypatch the
    # reference held by `api.routes.upgrade` (not `api.tasks.upgrade`).
    from api.routes import upgrade as upgrade_route
    from api.tasks import upgrade as upgrade_task

    class _Aca:
        def apply_images(self, *, images, revision_suffix=None) -> str:
            return "poller-rb"

    def _wrap(*args, **kwargs):
        kwargs["aca"] = _Aca()
        return upgrade_task.start_rollback_inline(*args, **kwargs)

    monkeypatch.setattr(upgrade_route, "start_rollback_inline", _wrap)

    resp = client.post("/api/upgrade/rollback")
    assert resp.status_code == 202
    body = resp.json()
    assert body["state"] == state.STATE_ROLLED_BACK


def test_escape_hatch_returns_commands(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-1")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg-elb")
    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    _seed_rollback_snapshot()
    resp = client.get("/api/upgrade/escape-hatch")
    assert resp.status_code == 200
    body = resp.json()
    assert body["container_app"] == "ca-elb-dashboard"
    assert any("--container-name api" in c for c in body["commands"])


def test_escape_hatch_404_without_snapshot(client: TestClient) -> None:
    resp = client.get("/api/upgrade/escape-hatch")
    assert resp.status_code == 404


def test_history_returns_tail(client: TestClient) -> None:
    from api.services.upgrade import history as hist

    hist.record_event("start", job_id="j1", target_version="0.3.0")
    hist.record_event("succeeded", job_id="j1", running_version="0.3.0")
    resp = client.get("/api/upgrade/history?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert [e["event"] for e in body["events"]] == ["succeeded", "start"]
    assert body["events"][0]["running_version"] == "0.3.0"


def test_history_requires_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    state.set_backend(state.InMemoryBackend())
    try:
        from api.main import app

        with TestClient(app) as c:
            resp = c.get("/api/upgrade/history")
        assert resp.status_code == 401
    finally:
        state.set_backend(None)


def test_httpx_client_pkt_parser_smoke() -> None:
    """Belt-and-braces — exercise the parser through a MockTransport so the
    integration of fetch_release_tags + httpx pipeline stays covered after
    monkeypatching takes over in the route tests."""

    sha = "a" * 40
    ref_line = f"{sha} refs/tags/v0.1.0\n".encode()
    length = len(ref_line) + 4
    payload = (
        b"001e# service=git-upload-pack\n0000"
        + f"{length:04x}".encode()
        + ref_line
        + b"0000"
    )

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    class _Stub(httpx.Client):
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[override]
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    tags = remote_tags.fetch_release_tags(
        "https://example.test/foo.git", http_client_factory=_Stub
    )
    assert [t.name for t in tags] == ["0.1.0"]
