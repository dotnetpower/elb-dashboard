"""Tests for `api/services/k8s/prepare_db_preflight.py`.

Responsibility: Pin the AKS kubelet RBAC pre-flight probe used by the
prepare-db AKS dispatch path. The probe must (a) return ``ok`` when
the kubelet identity holds Storage Blob Data Contributor (or a
superset role) on the storage account, (b) return ``missing`` when the
role-assignment list is empty / no role matches, (c) return
``probe_failed`` when the ARM probe itself raises so callers can fall
through optimistically, (d) return ``no_kubelet`` when the cluster
has no kubelet identity profile so callers can refuse cleanly.

Edit boundaries: Pure unit tests, no live ARM. Stubs replace
``_resolve_kubelet_oid``, ``storage_client``, and the
``AuthorizationManagementClient`` so the probe runs entirely in
process.

Key entry points: tests under ``pytest`` defaults.

Validation: ``uv run pytest -q api/tests/test_prepare_db_aks_preflight.py``.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from api.services.k8s import prepare_db_preflight


class _FakeAssignment:
    def __init__(self, role_def_id: str) -> None:
        self.role_definition_id = role_def_id


def _patch_kubelet(monkeypatch: pytest.MonkeyPatch, oid: str | None) -> None:
    monkeypatch.setattr(
        prepare_db_preflight,
        "_resolve_kubelet_oid",
        lambda *_a, **_kw: oid,
    )


def _patch_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Storage:
        id = (
            "/subscriptions/sub-1/resourceGroups/rg-st"
            "/providers/Microsoft.Storage/storageAccounts/st1"
        )

    class _Acc:
        def get_properties(self, *_a: Any, **_kw: Any) -> _Storage:
            return _Storage()

    class _SC:
        storage_accounts = _Acc()

    monkeypatch.setattr(
        "api.services.azure_clients.storage_client",
        lambda *_a, **_kw: _SC(),
    )


def _patch_role_assignments(
    monkeypatch: pytest.MonkeyPatch, assignments: list[_FakeAssignment]
) -> None:
    class _RAClient:
        def list_for_scope(
            self, *, scope: str, filter: str
        ) -> list[_FakeAssignment]:
            del scope, filter
            return assignments

    class _AuthCl:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            self.role_assignments = _RAClient()

    # The probe imports `from azure.mgmt.authorization import
    # AuthorizationManagementClient` lazily. Substitute a stub module
    # so we don't require the real SDK.
    module = types.SimpleNamespace(AuthorizationManagementClient=_AuthCl)
    monkeypatch.setitem(sys.modules, "azure.mgmt.authorization", module)


def test_kubelet_with_blob_contributor_role_returns_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_kubelet(monkeypatch, "kub-oid-1")
    _patch_storage(monkeypatch)
    _patch_role_assignments(
        monkeypatch,
        [
            _FakeAssignment(
                "/subscriptions/sub-1/providers/Microsoft.Authorization/"
                "roleDefinitions/ba92f5b4-2d11-453d-a403-e96b0029c9fe"
            )
        ],
    )
    result = prepare_db_preflight.kubelet_storage_blob_data_access(
        object(),  # type: ignore[arg-type]
        subscription_id="sub-1",
        resource_group="rg-aks",
        cluster_name="aks-1",
        storage_resource_group="rg-st",
        storage_account="st1",
    )
    assert result.ok
    assert result.status == "ok"
    assert result.matched_role_guid == "ba92f5b4-2d11-453d-a403-e96b0029c9fe"


def test_kubelet_without_matching_role_returns_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_kubelet(monkeypatch, "kub-oid-1")
    _patch_storage(monkeypatch)
    # Reader-only role — does not qualify.
    _patch_role_assignments(
        monkeypatch,
        [
            _FakeAssignment(
                "/subscriptions/sub-1/providers/Microsoft.Authorization/"
                "roleDefinitions/2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"
            )
        ],
    )
    result = prepare_db_preflight.kubelet_storage_blob_data_access(
        object(),  # type: ignore[arg-type]
        subscription_id="sub-1",
        resource_group="rg-aks",
        cluster_name="aks-1",
        storage_resource_group="rg-st",
        storage_account="st1",
    )
    assert result.status == "missing"
    assert result.should_block


def test_kubelet_lookup_failure_returns_probe_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_kw: Any) -> str:
        raise RuntimeError("aks not found")

    monkeypatch.setattr(prepare_db_preflight, "_resolve_kubelet_oid", boom)
    result = prepare_db_preflight.kubelet_storage_blob_data_access(
        object(),  # type: ignore[arg-type]
        subscription_id="sub-1",
        resource_group="rg-aks",
        cluster_name="aks-1",
        storage_resource_group="rg-st",
        storage_account="st1",
    )
    assert result.status == "probe_failed"
    assert not result.should_block


def test_cluster_without_kubelet_identity_returns_no_kubelet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_kubelet(monkeypatch, None)
    result = prepare_db_preflight.kubelet_storage_blob_data_access(
        object(),  # type: ignore[arg-type]
        subscription_id="sub-1",
        resource_group="rg-aks",
        cluster_name="aks-1",
        storage_resource_group="rg-st",
        storage_account="st1",
    )
    assert result.status == "no_kubelet"
    assert result.should_block
