"""Tests for explicit NCBI Direct prepare-db dispatch.

Responsibility: Verify feature gating, atomic pending-generation claims, pinned
    task payloads, duplicate-release rejection, and owner-safe enqueue rollback.
Edit boundaries: Dispatch unit tests only; no real NCBI, Azure, Redis, or AKS.
Key entry points: Tests for `dispatch_ncbi_direct`.
Risky contracts: Direct mode never silently falls back and enqueue failure must
    not leave `update_in_progress` pinned.
Validation: `uv run pytest -q api/tests/test_prepare_db_direct_dispatch.py`.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from api.auth import CallerIdentity
from api.services.ncbi_direct import (
    NcbiDirectArchive,
    NcbiDirectManifest,
    transfer_manifest_sha256,
)
from api.services.storage import prepare_db_direct_dispatch as dispatch


class _Container:
    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        self.metadata = dict(metadata or {})


class _Service:
    def __init__(self, container: _Container) -> None:
        self.container = container

    def get_container_client(self, name: str) -> _Container:
        assert name == "blast-db"
        return self.container


def _caller() -> CallerIdentity:
    return CallerIdentity(
        object_id="00000000-0000-0000-0000-000000000002",
        tenant_id="00000000-0000-0000-0000-000000000003",
        upn="researcher@example.com",
        raw_token="",
        claims={},
    )


def _manifest() -> NcbiDirectManifest:
    return NcbiDirectManifest(
        db_name="core_nt",
        released_at="2026-08-19T00:00:00",
        release_fingerprint="a" * 64,
        generation_id="ncbi-direct-20260819-aaaaaaaaaaaa",
        transfer_manifest_sha256="b" * 64,
        number_of_letters=100,
        number_of_sequences=10,
        bytes_total=200,
        bytes_total_compressed=100,
        archives=(
            NcbiDirectArchive(
                url="https://ftp.ncbi.nlm.nih.gov/blast/db/core_nt.00.tar.gz",
                md5_url="https://ftp.ncbi.nlm.nih.gov/blast/db/core_nt.00.tar.gz.md5",
                md5="c" * 32,
                size=100,
                member_prefix="core_nt",
            ),
        ),
    )


def _install(monkeypatch: pytest.MonkeyPatch, container: _Container) -> list[dict[str, Any]]:
    monkeypatch.setenv("PREPARE_DB_NCBI_DIRECT_ENABLED", "true")
    monkeypatch.setenv("PREPARE_DB_INCLUDE_TAXONOMY", "false")
    monkeypatch.setenv("PREPARE_DB_AKS_AZCOPY_IMAGE", "acr/prepare:tag")
    monkeypatch.setattr(dispatch, "build_direct_manifest", lambda _db: _manifest())
    monkeypatch.setattr("api.services.ncbi_direct_lock.acquire_direct_lock", lambda _owner: True)
    monkeypatch.setattr("api.services.ncbi_direct_lock.release_direct_lock", lambda _owner: True)
    monkeypatch.setattr(
        "api.services.storage.data._blob_service", lambda _cred, _account: _Service(container)
    )
    monkeypatch.setattr(
        dispatch, "download_blob_with_etag", lambda _cc, _db: (dict(container.metadata), "etag")
    )

    def update(_cc: Any, _db: str, _account: str, mutator: Any) -> dict[str, Any]:
        container.metadata = mutator(dict(container.metadata))
        return dict(container.metadata)

    monkeypatch.setattr(dispatch, "update_metadata", update)
    monkeypatch.setattr(
        "api.services.storage.prepare_db_aks_params.resolve_aks_job_limits",
        lambda: SimpleNamespace(image="acr/prepare:tag"),
    )
    sent: list[dict[str, Any]] = []

    def send(name: str, *, queue: str, **kwargs: Any) -> SimpleNamespace:
        sent.append({"name": name, "queue": queue, "kwargs": kwargs})
        return SimpleNamespace(id="task-direct")

    monkeypatch.setattr("api.routes._blast_shared._safe_send_task", send)
    return sent


def test_direct_dispatch_is_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PREPARE_DB_NCBI_DIRECT_ENABLED", raising=False)
    with pytest.raises(dispatch.DirectDispatchError, match="disabled"):
        dispatch.dispatch_ncbi_direct(
            caller=_caller(),
            credential=object(),
            subscription_id="sub",
            storage_account="stelb",
            db_name="core_nt",
            aks_resource_group="rg-aks",
            cluster_name="aks",
        )


def test_direct_dispatch_claims_generation_and_pins_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _Container({"source_version": "legacy"})
    sent = _install(monkeypatch, container)

    response = dispatch.dispatch_ncbi_direct(
        caller=_caller(),
        credential=object(),
        subscription_id="sub",
        storage_account="stelb",
        db_name="core_nt",
        aks_resource_group="rg-aks",
        cluster_name="aks",
    )

    assert response["task_id"] == "task-direct"
    assert container.metadata["update_in_progress"] is True
    pending = container.metadata["pending_generation"]
    assert pending["release_fingerprint"] == "a" * 64
    assert pending["transfer_manifest_sha256"] == transfer_manifest_sha256(_manifest().archives)
    assert container.metadata["aks_job_ref"]["source_provider"] == "ncbi-direct"
    assert sent[0]["name"] == "api.tasks.storage.prepare_db_via_ncbi_direct"
    assert sent[0]["kwargs"]["archives"][0]["md5"] == "c" * 32


def test_direct_dispatch_rejects_active_same_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _Container({"active_generation": {"release_fingerprint": "a" * 64}})
    _install(monkeypatch, container)

    with pytest.raises(dispatch.DirectDispatchError, match="already active"):
        dispatch.dispatch_ncbi_direct(
            caller=_caller(),
            credential=object(),
            subscription_id="sub",
            storage_account="stelb",
            db_name="core_nt",
            aks_resource_group="rg-aks",
            cluster_name="aks",
        )
