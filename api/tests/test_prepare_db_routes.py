"""Route-level tests for the new prepare-db hardening surfaces.

Responsibility: Cover the 409 response when a prepare-db daemon is already
    running for the same (account, db), and the new cancel endpoint.
Edit boundaries: Mock the Azure SDK and the lock registry; never reach a
    real Storage account.
Key entry points: ``test_concurrent_prepare_db_returns_409``,
    ``test_cancel_aborts_pending_copies``,
    ``test_cancel_refuses_when_completed``.
Risky contracts: 409 + lock interaction is load-bearing — the test asserts
    BOTH that the second call returns 409 AND that the held lock is not
    released by the second-rejected caller.
Validation: ``uv run pytest -q api/tests/test_prepare_db_routes.py``.
"""

from __future__ import annotations

import sys as _sys
from typing import Any

import api.routes.storage.prepare_db  # noqa: F401 — ensure module is imported
import pytest
from fastapi.testclient import TestClient

prepare_db_module = _sys.modules["api.routes.storage.prepare_db"]


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    # Reset the lock registry between tests so a held lock from a prior test
    # doesn't leak.
    with prepare_db_module._PREPARE_DB_LOCK_REGISTRY_GUARD:
        prepare_db_module._PREPARE_DB_LOCK_REGISTRY.clear()
    from api.main import app

    return TestClient(app)


def _patch_common(monkeypatch: pytest.MonkeyPatch, *, snapshot: str, keys: list[str]) -> None:
    monkeypatch.setattr(
        prepare_db_module,
        "_resolve_latest_dir",
        lambda: snapshot,
        raising=True,
    )
    monkeypatch.setattr(
        prepare_db_module,
        "_list_keys",
        lambda _s, _d: list(keys),
        raising=True,
    )
    monkeypatch.setattr(
        "api.services.storage.public_access.ensure_local_storage_access",
        lambda *_a, **_kw: {"action": "noop"},
        raising=True,
    )


class _FakeBlob:
    def __init__(self, status: str = "success") -> None:
        self._status = status

    def start_copy_from_url(self, _url: str) -> None:
        return None

    def get_blob_properties(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            copy=SimpleNamespace(status=self._status, id="copy-1", status_description="")
        )

    def abort_copy(self, _cid: str) -> None:
        return None


class _FakeListedBlob:
    def __init__(self, name: str, status: str) -> None:
        self.name = name
        self.copy = type(
            "_Copy", (), {"status": status, "id": "copy-1", "status_description": ""}
        )


class _FakeContainer:
    def __init__(self, statuses: dict[str, str]) -> None:
        self._statuses = statuses
        self._meta: dict[str, Any] = {"db_name": "core_nt"}

    def get_blob_client(self, name: str) -> Any:
        if name.endswith("-metadata.json"):
            outer = self

            class _Meta:
                def download_blob(self, *, offset: int = 0, length: int | None = None) -> Any:
                    del offset, length
                    import json as _json

                    payload = _json.dumps(outer._meta).encode("utf-8")
                    stream = type(
                        "_S",
                        (),
                        {
                            "readall": lambda self: payload,
                            "properties": type("_P", (), {"etag": "etag-1"}),
                        },
                    )()
                    return stream

                def upload_blob(self, body: bytes, **_kw: Any) -> dict[str, str]:
                    import json as _json

                    outer._meta = _json.loads(body.decode("utf-8"))
                    return {"etag": '"etag-2"'}

            return _Meta()
        return _FakeBlob(self._statuses.get(name, "success"))

    def list_blobs(self, name_starts_with: str | None = None, include: Any = None) -> Any:
        del include
        prefix = name_starts_with or ""
        for name, status in self._statuses.items():
            if name.startswith(prefix):
                yield _FakeListedBlob(name, status)


class _FakeBlobSvc:
    def __init__(self, container: _FakeContainer) -> None:
        self._container = container

    def get_container_client(self, _name: str) -> _FakeContainer:
        return self._container


def test_concurrent_prepare_db_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = "2026-05-21-01-05-02"
    _patch_common(monkeypatch, snapshot=snapshot, keys=[f"{snapshot}/core_nt.000.nhr"])
    container = _FakeContainer({"core_nt/core_nt.000.nhr": "success"})
    monkeypatch.setattr(
        "azure.storage.blob.BlobServiceClient",
        lambda **_kw: _FakeBlobSvc(container),
    )
    monkeypatch.setattr(
        "api.services.storage.data._blob_service",
        lambda _cred, _account: _FakeBlobSvc(container),
        raising=True,
    )
    monkeypatch.setattr(
        "api.services.db.ops_audit.record_db_op",
        lambda **_kw: "",
        raising=False,
    )

    # Acquire the lock so the route sees it as in-flight and returns 409.
    lock = prepare_db_module._prepare_db_lock("stworkload", "core_nt")
    assert lock.acquire(blocking=False)
    try:
        body = {
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "storage_resource_group": "rg-workload",
            "account_name": "stworkload",
            "db_name": "core_nt",
        }
        resp = client.post("/api/storage/prepare-db", json=body)
        assert resp.status_code == 409
        assert "progress" in resp.json()["detail"].lower()
    finally:
        lock.release()


def test_cancel_aborts_pending_copies(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _FakeContainer(
        {
            "core_nt/core_nt.000.nhr": "pending",
            "core_nt/core_nt.000.nin": "success",
        }
    )
    container._meta = {
        "db_name": "core_nt",
        "update_in_progress": True,
        "copy_status": {"phase": "copying"},
    }
    monkeypatch.setattr(
        "azure.storage.blob.BlobServiceClient",
        lambda **_kw: _FakeBlobSvc(container),
    )
    monkeypatch.setattr(
        "api.services.storage.data._blob_service",
        lambda _cred, _account: _FakeBlobSvc(container),
        raising=True,
    )
    monkeypatch.setattr(
        "api.services.storage.public_access.ensure_local_storage_access",
        lambda *_a, **_kw: {"action": "noop"},
        raising=True,
    )
    monkeypatch.setattr(
        "api.services.db.ops_audit.record_db_op",
        lambda **_kw: "",
        raising=False,
    )

    resp = client.post(
        "/api/storage/prepare-db/core_nt/cancel",
        json={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "storage_resource_group": "rg-workload",
            "account_name": "stworkload",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["aborted"] == 1
    assert container._meta["copy_status"]["phase"] == "cancelled"


def test_cancel_refuses_when_completed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _FakeContainer({"core_nt/core_nt.000.nhr": "success"})
    container._meta = {
        "db_name": "core_nt",
        "update_in_progress": False,
        "copy_status": {"phase": "completed"},
    }
    monkeypatch.setattr(
        "azure.storage.blob.BlobServiceClient",
        lambda **_kw: _FakeBlobSvc(container),
    )
    monkeypatch.setattr(
        "api.services.storage.data._blob_service",
        lambda _cred, _account: _FakeBlobSvc(container),
        raising=True,
    )
    monkeypatch.setattr(
        "api.services.storage.public_access.ensure_local_storage_access",
        lambda *_a, **_kw: {"action": "noop"},
        raising=True,
    )
    resp = client.post(
        "/api/storage/prepare-db/core_nt/cancel",
        json={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "storage_resource_group": "rg-workload",
            "account_name": "stworkload",
        },
    )
    assert resp.status_code == 409
