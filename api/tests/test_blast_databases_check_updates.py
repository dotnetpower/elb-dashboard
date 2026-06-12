"""Integration tests for /api/blast/databases/check-updates.

Responsibility: Cover the per-DB update detection that compares NCBI ETag
    against the ETag stored in each downloaded DB's metadata blob. Without
    storage scope, the route stays in legacy "global latest_version only"
    mode.
Edit boundaries: Mock list_databases + preview_database; never reach Azure
    or NCBI from CI.
Key entry points: `test_no_storage_scope_returns_legacy_shape`,
    `test_per_db_etag_match_returns_no_updates`,
    `test_per_db_etag_diff_lists_update`,
    `test_ncbi_unavailable_degrades`.
Risky contracts: Response keys ``latest_version`` and ``updates_available``
    are part of the SPA contract (web/src/api/blast.ts ``checkUpdates``). The
    ``updates_available_evaluated`` boolean tells the SPA whether the per-DB
    comparison actually ran; an empty list with the flag True is authoritative
    "nothing stale" (the SPA must not re-apply its legacy source_version
    heuristic), while the flag False permits that fallback.
Validation: `uv run pytest -q api/tests/test_blast_databases_check_updates.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


def _patch_resolve(monkeypatch: pytest.MonkeyPatch, snapshot: str) -> None:
    monkeypatch.setattr(
        "api.routes.storage.common._resolve_latest_dir",
        lambda: snapshot,
        raising=True,
    )


def _patch_dbs(monkeypatch: pytest.MonkeyPatch, dbs: list[dict[str, Any]]) -> None:
    def _fake(_cred: Any, _account: str, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        import copy

        return copy.deepcopy(dbs)

    monkeypatch.setattr(
        "api.services.storage.data.list_databases", _fake, raising=True
    )

    def _no_access(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"action": "noop"}

    monkeypatch.setattr(
        "api.services.storage.public_access.ensure_local_storage_access",
        _no_access,
        raising=True,
    )


def _patch_preview(
    monkeypatch: pytest.MonkeyPatch, by_name: dict[str, dict[str, Any]]
) -> None:
    def _fake(name: str) -> dict[str, Any]:
        return dict(by_name.get(name, {"available": False, "db_name": name}))

    monkeypatch.setattr(
        "api.services.ncbi_catalogue.preview_database", _fake, raising=True
    )


def test_no_storage_scope_returns_legacy_shape(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_resolve(monkeypatch, "2026-05-21-01-05-02")
    resp = client.get("/api/blast/databases/check-updates")
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_version"] == "2026-05-21-01-05-02"
    assert body["updates_available"] == []
    # No storage scope -> the per-DB signature comparison did NOT run, so the
    # SPA is allowed to fall back to its legacy source_version heuristic.
    assert body["updates_available_evaluated"] is False


def test_per_db_etag_match_returns_no_updates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_resolve(monkeypatch, "2026-05-21-01-05-02")
    _patch_dbs(
        monkeypatch,
        [
            {
                "name": "swissprot",
                "source": "ncbi",
                "source_version": "2026-05-01-01-05-01",
                "signature_etag": "sig-etag-1",
            }
        ],
    )
    _patch_preview(
        monkeypatch,
        {
            "swissprot": {
                "available": True,
                "snapshot": "2026-05-21-01-05-02",
                "signature_etag": "sig-etag-1",
            }
        },
    )

    resp = client.get(
        "/api/blast/databases/check-updates",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "storage_account": "stworkload",
            "resource_group": "rg-workload",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_version"] == "2026-05-21-01-05-02"
    # ETag unchanged -> no update fires even though latest-dir rotated.
    assert body["updates_available"] == []
    # Per-DB comparison ran and found nothing: this empty list is
    # authoritative, so the SPA must NOT fall back to the legacy heuristic.
    assert body["updates_available_evaluated"] is True


def test_per_db_etag_diff_lists_update(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_resolve(monkeypatch, "2026-05-21-01-05-02")
    _patch_dbs(
        monkeypatch,
        [
            {
                "name": "core_nt",
                "source": "ncbi",
                "source_version": "2026-05-01-01-05-01",
                "signature_etag": "old-etag",
            }
        ],
    )
    _patch_preview(
        monkeypatch,
        {
            "core_nt": {
                "available": True,
                "snapshot": "2026-05-21-01-05-02",
                "signature_etag": "new-etag",
            }
        },
    )
    resp = client.get(
        "/api/blast/databases/check-updates",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "storage_account": "stworkload",
            "resource_group": "rg-workload",
        },
    )
    body = resp.json()
    assert len(body["updates_available"]) == 1
    item = body["updates_available"][0]
    assert item["db"] == "core_nt"
    assert item["signature_etag"] == "new-etag"
    assert item["stored_etag"] == "old-etag"
    assert body["updates_available_evaluated"] is True


def test_ncbi_unavailable_degrades(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.routes.storage.common import NcbiUnavailable

    def _raise() -> str:
        raise NcbiUnavailable("DNS failure")

    monkeypatch.setattr(
        "api.routes.storage.common._resolve_latest_dir", _raise, raising=True
    )
    resp = client.get("/api/blast/databases/check-updates")
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_version"] == ""
    assert body["degraded"] is True
    assert body["degraded_reason"] == "ncbi_unreachable"


def test_legacy_etag_empty_falls_back_to_snapshot_diff(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB prepared before composite/ETag signatures landed has no
    ``signature_etag`` / ``composite_signature`` in its metadata. The route
    must fall back to comparing ``source_version`` against the current
    NCBI snapshot."""
    _patch_resolve(monkeypatch, "2026-05-21-01-05-02")
    _patch_dbs(
        monkeypatch,
        [
            {
                "name": "legacy_pdb",
                "source": "ncbi",
                "source_version": "2026-05-01-01-05-01",  # older snapshot
                # No signature_etag / composite_signature.
            }
        ],
    )
    _patch_preview(
        monkeypatch,
        {
            "legacy_pdb": {
                "available": True,
                "snapshot": "2026-05-21-01-05-02",
                "signature_etag": "new-etag",
                "composite_signature": "comp-new",
            }
        },
    )
    resp = client.get(
        "/api/blast/databases/check-updates",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "storage_account": "stworkload",
            "resource_group": "rg-workload",
        },
    )
    body = resp.json()
    assert len(body["updates_available"]) == 1
    item = body["updates_available"][0]
    assert item["db"] == "legacy_pdb"
    assert item["stored_etag"] is None
    assert item["stored_composite_signature"] is None


def test_composite_signature_takes_precedence_over_etag(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When stored composite_signature is present, route compares composite
    even when the single-key ``signature_etag`` happens to match (legacy
    .000.tar.gz.md5 unchanged but later shard rotated)."""
    _patch_resolve(monkeypatch, "2026-05-21-01-05-02")
    _patch_dbs(
        monkeypatch,
        [
            {
                "name": "core_nt",
                "source": "ncbi",
                "source_version": "2026-05-21-01-05-02",
                "signature_etag": "etag-000-unchanged",
                "composite_signature": "comp-old",
            }
        ],
    )
    _patch_preview(
        monkeypatch,
        {
            "core_nt": {
                "available": True,
                "snapshot": "2026-05-21-01-05-02",
                "signature_etag": "etag-000-unchanged",
                "composite_signature": "comp-new",
            }
        },
    )
    resp = client.get(
        "/api/blast/databases/check-updates",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "storage_account": "stworkload",
            "resource_group": "rg-workload",
        },
    )
    body = resp.json()
    assert len(body["updates_available"]) == 1
    assert body["updates_available"][0]["stored_composite_signature"] == "comp-old"
    assert body["updates_available"][0]["composite_signature"] == "comp-new"
