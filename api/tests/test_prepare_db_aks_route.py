"""Tests for the `/api/storage/prepare-db` `mode` field — issue #7 Phase 1.

Responsibility: Cover the three branches of the new `mode` body field
    (server-side / aks / auto) including the explicit 409 `aks_unavailable`
    response (acceptance criterion #3) and that the `mode=server-side`
    default path is byte-for-byte unchanged (acceptance criterion #1).
Edit boundaries: Stubs the K8s probe + `_safe_send_task` + Storage
    container; never reaches a real cluster or Storage account.
Key entry points: `test_mode_server_side_default_path_unchanged`,
    `test_mode_aks_unavailable_returns_409`,
    `test_mode_aks_dispatches_celery_task`,
    `test_mode_auto_falls_back_when_no_aks_coords`.
Risky contracts: The 409 detail object's `code: aks_unavailable` is the
    SPA hook for showing the actionable "no AKS / not idle" hint —
    renaming it would break the upcoming Phase 2 UI work. The Celery
    task name `api.tasks.storage.prepare_db_via_aks` is the worker's
    registered name; the route test pins it so a typo bricks dispatch.
Validation: `uv run pytest -q api/tests/test_prepare_db_aks_route.py`.
"""

from __future__ import annotations

import sys as _sys
from typing import Any

import api.routes.storage.prepare_db  # noqa: F401
import pytest
from api.tests._fakes import make_send_task_recorder
from fastapi.testclient import TestClient

prepare_db_module = _sys.modules["api.routes.storage.prepare_db"]


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    # Default min nodes for AKS — small so tests with a single fake node pass.
    monkeypatch.setenv("PREPARE_DB_AKS_MIN_IDLE_NODES", "1")
    with prepare_db_module._PREPARE_DB_LOCK_REGISTRY_GUARD:
        prepare_db_module._PREPARE_DB_LOCK_REGISTRY.clear()
    from api.main import app

    return TestClient(app)


class _FakeListedBlob:
    def __init__(self, name: str, status: str) -> None:
        self.name = name
        self.copy = type(
            "_Copy", (), {"status": status, "id": "copy-1", "status_description": ""}
        )


class _FakeBlob:
    def start_copy_from_url(self, _url: str) -> None:
        return None

    def get_blob_properties(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            copy=SimpleNamespace(status="success", id="copy-1", status_description="")
        )


class _FakeContainer:
    def __init__(self) -> None:
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
        return _FakeBlob()

    def list_blobs(self, name_starts_with: str | None = None, include: Any = None) -> Any:
        del include, name_starts_with
        return iter([])


class _FakeBlobSvc:
    def __init__(self, container: _FakeContainer) -> None:
        self._container = container

    def get_container_client(self, _name: str) -> _FakeContainer:
        return self._container


def _baseline_patches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot: str,
    keys_with_sizes: list[tuple[str, int]],
    container: _FakeContainer,
) -> None:
    monkeypatch.setattr(
        prepare_db_module,
        "_resolve_latest_dir",
        lambda: snapshot,
        raising=True,
    )
    monkeypatch.setattr(
        prepare_db_module,
        "_list_keys",
        lambda _s, _d: [k for k, _ in keys_with_sizes],
        raising=True,
    )
    monkeypatch.setattr(
        "api.routes.storage.common._list_keys_with_sizes",
        lambda _s, _d: list(keys_with_sizes),
        raising=True,
    )
    monkeypatch.setattr(
        "api.routes.storage.common.shared_taxonomy_keys",
        lambda _s: [],
        raising=True,
    )
    monkeypatch.setattr(
        prepare_db_module,
        "shared_taxonomy_keys",
        lambda _s: [],
        raising=True,
    )
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


def test_mode_server_side_default_path_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = "2026-05-21-01-05-02"
    container = _FakeContainer()
    _baseline_patches(
        monkeypatch,
        snapshot=snapshot,
        keys_with_sizes=[(f"{snapshot}/core_nt.000.nhr", 1024)],
        container=container,
    )

    # Confirm that no AKS probe is invoked when mode is omitted.
    def _boom(*_a, **_kw):
        raise AssertionError("k8s_ready_warmup_node_names must not be called for server-side mode")

    monkeypatch.setattr(
        "api.services.k8s.nodes.k8s_ready_warmup_node_names", _boom, raising=True
    )
    # And no Celery dispatch.
    calls, fake_send = make_send_task_recorder("task-aks-not-called")
    monkeypatch.setattr("api.celery_app.celery_app.send_task", fake_send)

    body = {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "storage_resource_group": "rg-workload",
        "account_name": "stworkload",
        "db_name": "core_nt",
    }
    resp = client.post("/api/storage/prepare-db", json=body)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload.get("mode") != "aks", payload
    # AKS Celery task NOT dispatched on server-side path.
    aks_calls = [c for c in calls if "prepare_db_via_aks" in c["task_name"]]
    assert aks_calls == []


def test_mode_aks_requires_aks_resource_group_and_cluster_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = "2026-05-21-01-05-02"
    container = _FakeContainer()
    _baseline_patches(
        monkeypatch,
        snapshot=snapshot,
        keys_with_sizes=[(f"{snapshot}/core_nt.000.nhr", 1024)],
        container=container,
    )
    body = {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "storage_resource_group": "rg-workload",
        "account_name": "stworkload",
        "db_name": "core_nt",
        "mode": "aks",
    }
    resp = client.post("/api/storage/prepare-db", json=body)
    assert resp.status_code == 400
    assert "aks_resource_group" in resp.json()["detail"]


def test_mode_aks_unavailable_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = "2026-05-21-01-05-02"
    container = _FakeContainer()
    _baseline_patches(
        monkeypatch,
        snapshot=snapshot,
        keys_with_sizes=[(f"{snapshot}/core_nt.000.nhr", 1024)],
        container=container,
    )
    # No nodes ready.
    monkeypatch.setattr(
        "api.services.k8s.nodes.k8s_ready_warmup_node_names",
        lambda *_a, **_kw: [],
        raising=True,
    )

    body = {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "storage_resource_group": "rg-workload",
        "account_name": "stworkload",
        "db_name": "core_nt",
        "mode": "aks",
        "aks_resource_group": "rg-elb",
        "cluster_name": "aks-elb",
    }
    resp = client.post("/api/storage/prepare-db", json=body)
    assert resp.status_code == 409, resp.text
    detail = resp.json()
    assert detail["code"] == "aks_unavailable"
    assert detail["ready_nodes"] == 0
    assert detail["required_nodes"] >= 1


def test_mode_aks_dispatches_celery_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = "2026-05-21-01-05-02"
    container = _FakeContainer()
    _baseline_patches(
        monkeypatch,
        snapshot=snapshot,
        keys_with_sizes=[
            (f"{snapshot}/core_nt.000.nhr", 1024),
            (f"{snapshot}/core_nt.000.nin", 4096),
        ],
        container=container,
    )
    monkeypatch.setattr(
        "api.services.k8s.nodes.k8s_ready_warmup_node_names",
        lambda *_a, **_kw: ["aks-node-1"],
        raising=True,
    )
    calls, fake_send = make_send_task_recorder("task-aks-1")
    monkeypatch.setattr("api.celery_app.celery_app.send_task", fake_send)

    body = {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "storage_resource_group": "rg-workload",
        "account_name": "stworkload",
        "db_name": "core_nt",
        "mode": "aks",
        "aks_resource_group": "rg-elb",
        "cluster_name": "aks-elb",
    }
    resp = client.post("/api/storage/prepare-db", json=body)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["mode"] == "aks"
    assert payload["task_id"] == "task-aks-1"
    assert payload["files_total"] == 2
    assert payload["ready_nodes"] == 1

    aks_calls = [c for c in calls if c["task_name"] == "api.tasks.storage.prepare_db_via_aks"]
    assert len(aks_calls) == 1
    kwargs = aks_calls[0]["kwargs"]
    assert kwargs["db_name"] == "core_nt"
    assert kwargs["storage_account"] == "stworkload"
    assert kwargs["aks_resource_group"] == "rg-elb"
    assert kwargs["cluster_name"] == "aks-elb"
    assert kwargs["source_version"] == snapshot
    assert kwargs["file_sizes"] == {
        f"{snapshot}/core_nt.000.nhr": 1024,
        f"{snapshot}/core_nt.000.nin": 4096,
    }
    assert aks_calls[0]["queue"] == "storage"

    # Metadata transition recorded mode=aks
    assert container._meta["update_in_progress"] is True
    assert container._meta["copy_status"]["mode"] == "aks"
    assert container._meta["copy_status"]["phase"] == "queued"


def test_mode_aks_concurrent_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = "2026-05-21-01-05-02"
    container = _FakeContainer()
    _baseline_patches(
        monkeypatch,
        snapshot=snapshot,
        keys_with_sizes=[(f"{snapshot}/core_nt.000.nhr", 1024)],
        container=container,
    )
    monkeypatch.setattr(
        "api.services.k8s.nodes.k8s_ready_warmup_node_names",
        lambda *_a, **_kw: ["aks-node-1"],
        raising=True,
    )

    # Hold the lock so the route 409s on the AKS path too.
    lock = prepare_db_module._prepare_db_lock("stworkload", "core_nt")
    assert lock.acquire(blocking=False)
    try:
        body = {
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "storage_resource_group": "rg-workload",
            "account_name": "stworkload",
            "db_name": "core_nt",
            "mode": "aks",
            "aks_resource_group": "rg-elb",
            "cluster_name": "aks-elb",
        }
        resp = client.post("/api/storage/prepare-db", json=body)
        assert resp.status_code == 409
        assert "progress" in resp.json()["detail"].lower()
    finally:
        lock.release()


def test_mode_auto_falls_back_when_no_aks_coords(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = "2026-05-21-01-05-02"
    container = _FakeContainer()
    _baseline_patches(
        monkeypatch,
        snapshot=snapshot,
        keys_with_sizes=[(f"{snapshot}/core_nt.000.nhr", 1024)],
        container=container,
    )

    def _boom(*_a, **_kw):
        raise AssertionError("AKS probe must not run when no coords supplied for mode=auto")

    monkeypatch.setattr(
        "api.services.k8s.nodes.k8s_ready_warmup_node_names", _boom, raising=True
    )
    calls, fake_send = make_send_task_recorder("task-not-aks")
    monkeypatch.setattr("api.celery_app.celery_app.send_task", fake_send)

    body = {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "storage_resource_group": "rg-workload",
        "account_name": "stworkload",
        "db_name": "core_nt",
        "mode": "auto",
    }
    resp = client.post("/api/storage/prepare-db", json=body)
    assert resp.status_code == 200
    # Server-side path, AKS Celery task NOT dispatched
    aks_calls = [c for c in calls if "prepare_db_via_aks" in c["task_name"]]
    assert aks_calls == []


def test_mode_auto_uses_aks_when_available(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = "2026-05-21-01-05-02"
    container = _FakeContainer()
    _baseline_patches(
        monkeypatch,
        snapshot=snapshot,
        keys_with_sizes=[(f"{snapshot}/core_nt.000.nhr", 1024)],
        container=container,
    )
    monkeypatch.setattr(
        "api.services.k8s.nodes.k8s_ready_warmup_node_names",
        lambda *_a, **_kw: ["aks-node-1", "aks-node-2"],
        raising=True,
    )
    calls, fake_send = make_send_task_recorder("task-aks-auto")
    monkeypatch.setattr("api.celery_app.celery_app.send_task", fake_send)

    body = {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "storage_resource_group": "rg-workload",
        "account_name": "stworkload",
        "db_name": "core_nt",
        "mode": "auto",
        "aks_resource_group": "rg-elb",
        "cluster_name": "aks-elb",
    }
    resp = client.post("/api/storage/prepare-db", json=body)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["mode"] == "aks"
    aks_calls = [c for c in calls if c["task_name"] == "api.tasks.storage.prepare_db_via_aks"]
    assert len(aks_calls) == 1


def test_invalid_mode_returns_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "storage_resource_group": "rg-workload",
        "account_name": "stworkload",
        "db_name": "core_nt",
        "mode": "fancy-mode",
    }
    resp = client.post("/api/storage/prepare-db", json=body)
    assert resp.status_code == 400
    assert "invalid mode" in resp.json()["detail"].lower()


def test_mode_aks_probe_failure_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = "2026-05-21-01-05-02"
    container = _FakeContainer()
    _baseline_patches(
        monkeypatch,
        snapshot=snapshot,
        keys_with_sizes=[(f"{snapshot}/core_nt.000.nhr", 1024)],
        container=container,
    )

    def _raise(*_a, **_kw):
        raise RuntimeError("AKS API down")

    monkeypatch.setattr(
        "api.services.k8s.nodes.k8s_ready_warmup_node_names", _raise, raising=True
    )

    body = {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "storage_resource_group": "rg-workload",
        "account_name": "stworkload",
        "db_name": "core_nt",
        "mode": "aks",
        "aks_resource_group": "rg-elb",
        "cluster_name": "aks-elb",
    }
    resp = client.post("/api/storage/prepare-db", json=body)
    assert resp.status_code == 409
    detail = resp.json()
    assert detail["code"] == "aks_unavailable"
