"""Tests for the `/api/storage/prepare-db/{db}/delete` lifecycle route.

Responsibility: Cover the prepare-db Delete action — happy-path blob +
    metadata removal, the in-flight-copy 409 guard (copying / queued /
    update_in_progress), AKS Job + ConfigMap cleanup via `aks_job_ref`,
    and idempotent behaviour when blobs / metadata are already gone.
Edit boundaries: Stubs the Storage container + `ensure_local_storage_access`
    + `delete_prepare_db_job` + `record_db_op`; never reaches a real cluster
    or Storage account.
Key entry points: `test_delete_ready_db_removes_blobs_and_metadata`,
    `test_delete_refused_while_copy_in_flight`,
    `test_delete_refused_while_update_in_progress`,
    `test_delete_partial_db_with_aks_ref_deletes_job`,
    `test_delete_idempotent_when_absent`.
Risky contracts: The Delete route must NEVER run under a live copy — the
    409 guard is the safety net that stops a race with an azcopy fan-out.
    The audit op name `prepare_db_delete` is consumed by the SPA audit
    filter; keep it stable.
Validation: `uv run pytest -q api/tests/test_prepare_db_delete_route.py`.
"""

from __future__ import annotations

import json as _json
import sys as _sys
from types import SimpleNamespace
from typing import Any

import api.routes.storage.prepare_db  # noqa: F401
import pytest
from azure.core.exceptions import ResourceNotFoundError
from fastapi.testclient import TestClient

prepare_db_module = _sys.modules["api.routes.storage.prepare_db"]


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    with prepare_db_module._PREPARE_DB_LOCK_REGISTRY_GUARD:
        prepare_db_module._PREPARE_DB_LOCK_REGISTRY.clear()
    from api.main import app

    return TestClient(app)


class _FakeMetaBlob:
    def __init__(self, container: _FakeContainer) -> None:
        self._c = container

    def download_blob(self, *, offset: int = 0, length: int | None = None) -> Any:
        del offset, length
        if self._c._meta is None:
            raise ResourceNotFoundError("metadata gone")
        payload = _json.dumps(self._c._meta).encode("utf-8")
        return SimpleNamespace(
            readall=lambda: payload,
            properties=SimpleNamespace(etag="etag-1"),
        )


class _FakeContainer:
    """Tracks staged blobs + a metadata blob and records deletions."""

    def __init__(self, *, meta: dict[str, Any] | None, blobs: list[str]) -> None:
        self._meta = meta
        self._blobs = list(blobs)
        self.deleted: list[str] = []
        # Names that must report a failed delete (batch status 503 / per-blob
        # raise) so partial-failure handling can be exercised.
        self.fail_names: set[str] = set()
        # When True the metadata blob delete raises a transient error.
        self.fail_metadata = False

    def get_blob_client(self, name: str) -> Any:
        if name.endswith("-metadata.json"):
            return _FakeMetaBlob(self)
        raise AssertionError(f"unexpected get_blob_client({name})")

    def list_blobs(self, name_starts_with: str | None = None, include: Any = None) -> Any:
        del include
        prefix = name_starts_with or ""
        # Snapshot the names so deletes during iteration don't skip entries
        # (the real Azure paged iterator is server-snapshotted too).
        return iter(
            [SimpleNamespace(name=n) for n in self._blobs if n.startswith(prefix)]
        )

    def delete_blob(self, name: str, **_kw: Any) -> None:
        if name.endswith("-metadata.json"):
            if self.fail_metadata:
                raise RuntimeError("simulated metadata delete failure")
            if self._meta is None:
                raise ResourceNotFoundError("already gone")
            self._meta = None
            self.deleted.append(name)
            return
        if name in self.fail_names:
            raise RuntimeError(f"simulated delete failure for {name}")
        if name not in self._blobs:
            raise ResourceNotFoundError(name)
        self._blobs.remove(name)
        self.deleted.append(name)

    def delete_blobs(self, *names: str, **_kw: Any) -> list[Any]:
        # Mirror the real ContainerClient batch API: remove each blob and
        # return a per-blob response carrying a status_code the route counts
        # (202 deleted, 404 already gone, 503 failed).
        responses: list[Any] = []
        for name in names:
            if name in self.fail_names:
                responses.append(SimpleNamespace(status_code=503))
            elif name in self._blobs:
                self._blobs.remove(name)
                self.deleted.append(name)
                responses.append(SimpleNamespace(status_code=202))
            else:
                responses.append(SimpleNamespace(status_code=404))
        return responses



class _FakeBlobSvc:
    def __init__(self, container: _FakeContainer) -> None:
        self._container = container

    def get_container_client(self, _name: str) -> _FakeContainer:
        return self._container


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    container: _FakeContainer,
    *,
    delete_job: Any = None,
) -> None:
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
    monkeypatch.setattr(
        "api.services.blast.db_metadata.notify_blast_db_metadata_changed",
        lambda *_a, **_kw: None,
        raising=False,
    )
    if delete_job is not None:
        monkeypatch.setattr(
            "api.services.k8s.prepare_db_jobs.delete_prepare_db_job",
            delete_job,
            raising=True,
        )


_BODY = {
    "subscription_id": "00000000-0000-0000-0000-000000000001",
    "storage_resource_group": "rg-workload",
    "account_name": "stworkload",
}


def test_delete_ready_db_removes_blobs_and_metadata(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _FakeContainer(
        meta={"db_name": "core_nt", "copy_status": {"phase": "completed"}},
        blobs=["core_nt/core_nt.000.nhr", "core_nt/core_nt.000.nin"],
    )

    def _boom(*_a, **_kw):
        raise AssertionError("no aks_job_ref → delete_prepare_db_job must not run")

    _patch_common(monkeypatch, container, delete_job=_boom)

    resp = client.post("/api/storage/prepare-db/core_nt/delete", json=_BODY)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["deleted"] == 2
    assert payload["errors"] == 0
    assert payload["metadata_deleted"] is True
    assert payload["aks_job_deleted"] is None
    assert container._blobs == []
    assert container._meta is None
    assert "core_nt-metadata.json" in container.deleted


def test_delete_partial_failure_keeps_metadata(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One shard fails to delete (transient 503). The metadata blob must be
    # retained so the DB stays listed and re-deletable, and the response must
    # report partial=True with a non-zero error count.
    container = _FakeContainer(
        meta={"db_name": "core_nt", "copy_status": {"phase": "completed"}},
        blobs=[
            "core_nt/core_nt.000.nhr",
            "core_nt/core_nt.000.nin",
            "core_nt/core_nt.001.nhr",
        ],
    )
    container.fail_names = {"core_nt/core_nt.001.nhr"}
    _patch_common(monkeypatch, container)

    resp = client.post("/api/storage/prepare-db/core_nt/delete", json=_BODY)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["deleted"] == 2
    assert payload["errors"] == 1
    assert payload["partial"] is True
    assert payload["metadata_deleted"] is False
    # Metadata blob is intentionally retained for re-delete.
    assert container._meta is not None
    assert "core_nt-metadata.json" not in container.deleted
    # The surviving shard is still present.
    assert container._blobs == ["core_nt/core_nt.001.nhr"]


def test_delete_metadata_failure_reports_partial(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every shard is removed but the metadata blob delete fails. The DB is
    # therefore still listed, so the response must report partial=True even
    # though errors==0, and metadata_deleted=False.
    container = _FakeContainer(
        meta={"db_name": "core_nt", "copy_status": {"phase": "completed"}},
        blobs=["core_nt/core_nt.000.nhr", "core_nt/core_nt.000.nin"],
    )
    container.fail_metadata = True
    _patch_common(monkeypatch, container)

    resp = client.post("/api/storage/prepare-db/core_nt/delete", json=_BODY)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["deleted"] == 2
    assert payload["errors"] == 0
    assert payload["partial"] is True
    assert payload["metadata_deleted"] is False
    assert container._meta is not None


def test_delete_refused_while_copy_in_flight(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _FakeContainer(
        meta={"db_name": "core_nt", "copy_status": {"phase": "copying"}},
        blobs=["core_nt/core_nt.000.nhr"],
    )
    _patch_common(monkeypatch, container)

    resp = client.post("/api/storage/prepare-db/core_nt/delete", json=_BODY)
    assert resp.status_code == 409, resp.text
    # Nothing deleted under a live copy.
    assert container._blobs == ["core_nt/core_nt.000.nhr"]
    assert container._meta is not None


def test_delete_refused_while_update_in_progress(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _FakeContainer(
        meta={
            "db_name": "core_nt",
            "update_in_progress": True,
            "copy_status": {"phase": "completed"},
        },
        blobs=["core_nt/core_nt.000.nhr"],
    )
    _patch_common(monkeypatch, container)

    resp = client.post("/api/storage/prepare-db/core_nt/delete", json=_BODY)
    assert resp.status_code == 409, resp.text
    assert container._meta is not None


def test_delete_partial_db_with_aks_ref_deletes_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    aks_ref = {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "resource_group": "rg-elb",
        "cluster_name": "aks-elb",
        "namespace": "default",
        "job_name": "prepare-db-core-nt-260521010502",
        "configmap_name": "prepare-db-core-nt-260521010502",
    }
    container = _FakeContainer(
        meta={
            "db_name": "core_nt",
            "copy_status": {"phase": "partial"},
            "aks_job_ref": aks_ref,
        },
        blobs=["core_nt/core_nt.000.nhr"],
    )

    calls: list[dict[str, Any]] = []

    def _fake_delete(
        _cred, sub, rg, cluster, *, namespace, job_name, configmap_name=None
    ) -> dict[str, Any]:
        calls.append(
            {
                "rg": rg,
                "cluster": cluster,
                "namespace": namespace,
                "job_name": job_name,
                "configmap_name": configmap_name,
            }
        )
        return {"status": "deleted"}

    _patch_common(monkeypatch, container, delete_job=_fake_delete)

    resp = client.post("/api/storage/prepare-db/core_nt/delete", json=_BODY)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["aks_job_deleted"] == {"status": "deleted"}
    assert payload["deleted"] == 1
    assert container._meta is None
    assert len(calls) == 1
    assert calls[0]["job_name"] == aks_ref["job_name"]
    assert calls[0]["configmap_name"] == aks_ref["configmap_name"]
    assert calls[0]["cluster"] == "aks-elb"


def test_delete_idempotent_when_absent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No metadata blob, no staged blobs → a no-op success.
    container = _FakeContainer(meta=None, blobs=[])
    _patch_common(monkeypatch, container)

    resp = client.post("/api/storage/prepare-db/core_nt/delete", json=_BODY)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["deleted"] == 0
    assert payload["metadata_deleted"] is True
    assert payload["aks_job_deleted"] is None
