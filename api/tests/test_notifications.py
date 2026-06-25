"""Tests for the in-app notification center (service + routes).

Responsibility: Cover ``api.services.notifications`` feed/marker logic and the
``/api/notifications`` routes (terminal/child filtering, unread accounting,
first-read seeding, mark-seen, and graceful degradation).
Edit boundaries: Test-only; monkeypatches the job listing and marker storage so
no Azure Table is touched.
Key entry points: pytest test functions.
Risky contracts: Mirrors the ``list_for_owner`` signature and the marker helper
names; update these fakes if those contracts change.
Validation: ``uv run pytest -q api/tests/test_notifications.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from api.services import notifications as notif
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    from api.main import app

    return TestClient(app)


@dataclass
class FakeJob:
    job_id: str
    status: str
    updated_at: str
    parent_job_id: str | None = None
    job_title: str = ""
    program: str = ""
    db: str = ""
    error_code: str = ""


class FakeRepo:
    def __init__(self, jobs: list[FakeJob]) -> None:
        self._jobs = jobs

    def list_for_owner(
        self, _oid: str, limit: int = 50, *, include_payload: bool = True
    ) -> list[FakeJob]:
        del limit, include_payload
        return list(self._jobs)


def _patch_repo(monkeypatch: pytest.MonkeyPatch, jobs: list[FakeJob]) -> None:
    monkeypatch.setattr(
        "api.services.state.repository.get_state_repo",
        lambda: FakeRepo(jobs),
        raising=True,
    )


def _patch_marker(monkeypatch: pytest.MonkeyPatch, last_seen: str) -> list[str]:
    """Patch the marker helpers; return a list that captures set_last_seen writes."""
    writes: list[str] = []
    monkeypatch.setattr(notif, "get_last_seen", lambda _oid: last_seen)
    monkeypatch.setattr(notif, "set_last_seen", lambda _oid, ts: writes.append(ts))
    return writes


def test_build_notifications_keeps_only_terminal_non_child_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [
        FakeJob("j-run", "running", "2026-06-25T00:00:05+00:00"),
        FakeJob("j-done", "completed", "2026-06-25T00:00:04+00:00"),
        FakeJob("j-fail", "failed", "2026-06-25T00:00:03+00:00"),
        FakeJob("j-cancel", "cancelled", "2026-06-25T00:00:02+00:00"),
        FakeJob("j-child", "completed", "2026-06-25T00:00:06+00:00", parent_job_id="j-done"),
    ]
    _patch_repo(monkeypatch, jobs)
    _patch_marker(monkeypatch, "2026-06-25T00:00:00+00:00")

    result = notif.build_notifications("oid-1", seed_if_missing=False)

    ids = [item["job_id"] for item in result["items"]]
    # running + child excluded; terminal kept, most-recent first.
    assert ids == ["j-done", "j-fail", "j-cancel"]


def test_unread_counts_jobs_newer_than_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [
        FakeJob("j-new", "completed", "2026-06-25T00:00:09+00:00"),
        FakeJob("j-old", "failed", "2026-06-25T00:00:01+00:00"),
    ]
    _patch_repo(monkeypatch, jobs)
    _patch_marker(monkeypatch, "2026-06-25T00:00:05+00:00")

    result = notif.build_notifications("oid-1", seed_if_missing=False)

    assert result["unread_count"] == 1
    by_id = {item["job_id"]: item["unread"] for item in result["items"]}
    assert by_id == {"j-new": True, "j-old": False}


def test_first_read_seeds_marker_and_reports_zero_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [FakeJob("j-done", "completed", "2026-06-25T00:00:04+00:00")]
    _patch_repo(monkeypatch, jobs)
    writes = _patch_marker(monkeypatch, "")  # no marker yet

    result = notif.build_notifications("oid-1", seed_if_missing=True)

    assert writes, "first read must seed the marker"
    assert result["unread_count"] == 0
    assert result["items"][0]["unread"] is False


def test_no_seed_when_marker_missing_and_seed_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [FakeJob("j-done", "completed", "2026-06-25T00:00:04+00:00")]
    _patch_repo(monkeypatch, jobs)
    writes = _patch_marker(monkeypatch, "")

    result = notif.build_notifications("oid-1", seed_if_missing=False)

    assert writes == []
    # An empty marker means "never seen" — with seeding off, nothing is unread.
    assert result["unread_count"] == 0


def test_mark_all_seen_advances_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    writes = _patch_marker(monkeypatch, "")

    result = notif.mark_all_seen("oid-1")

    assert result["unread_count"] == 0
    assert result["last_seen_at"]
    assert writes == [result["last_seen_at"]]


def test_listing_failure_degrades_to_empty_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    class BoomRepo:
        def list_for_owner(self, *_a: object, **_k: object) -> list[FakeJob]:
            raise RuntimeError("table unavailable")

    monkeypatch.setattr(
        "api.services.state.repository.get_state_repo",
        lambda: BoomRepo(),
        raising=True,
    )
    _patch_marker(monkeypatch, "2026-06-25T00:00:00+00:00")

    result = notif.build_notifications("oid-1", seed_if_missing=False)

    assert result == {
        "items": [],
        "unread_count": 0,
        "last_seen_at": "2026-06-25T00:00:00+00:00",
    }


def test_marker_read_failure_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """A storage fault reading the marker degrades to "" (all seen), never raises."""

    def boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("creds blip")

    monkeypatch.setattr(notif, "_ensure_table", boom)
    assert notif.get_last_seen("oid-1") == ""


def test_get_notifications_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr(
        notif,
        "build_notifications",
        lambda _oid, *, limit=50: {
            "items": [
                {
                    "job_id": "j-1",
                    "status": "completed",
                    "title": "demo",
                    "program": "blastn",
                    "db": "nt",
                    "updated_at": "2026-06-25T00:00:04+00:00",
                    "error_code": "",
                    "unread": True,
                }
            ],
            "unread_count": 1,
            "last_seen_at": "2026-06-25T00:00:00+00:00",
        },
    )

    r = client.get("/api/notifications")
    assert r.status_code == 200
    body = r.json()
    assert body["unread_count"] == 1
    assert body["items"][0]["job_id"] == "j-1"


def test_seen_route_marks_all(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr(
        notif,
        "mark_all_seen",
        lambda _oid: {"last_seen_at": "2026-06-25T01:00:00+00:00", "unread_count": 0},
    )

    r = client.post("/api/notifications/seen")
    assert r.status_code == 200
    assert r.json() == {"last_seen_at": "2026-06-25T01:00:00+00:00", "unread_count": 0}
