"""Pre-flight RBAC checks for the prepare-db AKS-fanout path.

Responsibility: Confirm the AKS cluster's kubelet managed identity has the
``Storage Blob Data Contributor`` role assignment on the workload Storage
account before dispatching the per-shard prepare-db Job. Without that
grant every pod's ``azcopy login --identity`` succeeds but every PUT
returns 403, and the operator only sees the failure ~30 s into the Job
as a generic azcopy error.

Edit boundaries: Best-effort ARM probe only. Never raises Azure SDK
exceptions through to the caller; surfaces them as
``status == "probe_failed"`` so the caller can decide whether to fail
fast or fall through. No Kubernetes, no Storage data-plane.

Key entry points: ``kubelet_storage_blob_data_access``.

Risky contracts: Detects role assignments by Role Definition GUID
(``ba92f5b4-2d11-453d-a403-e96b0029c9fe`` for
``Storage Blob Data Contributor``, plus the superset roles
``Storage Blob Data Owner`` and built-in ``Owner`` /
``Contributor``). Role Definition IDs are tenant-stable so this is
safe; do not narrow the allowed set without also updating
``api.tasks.azure.rbac.attach_storage_blob_contributor``.

Validation: ``uv run pytest -q api/tests/test_prepare_db_aks_preflight.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from azure.core.credentials import TokenCredential

LOGGER = logging.getLogger(__name__)

# Role Definition GUIDs that grant blob data-plane write access. The
# kubelet identity needs at least one of these on the storage account
# scope so prepare-db pods can `azcopy copy --from-to=PipeBlob`.
_ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
_ROLE_STORAGE_BLOB_DATA_OWNER = "b7e6dc6d-f1e8-4753-8033-0f276bb0955b"
_ROLE_CONTRIBUTOR = "b24988ac-6180-42a0-ab88-20f7382dd24c"
_ROLE_OWNER = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"

_ACCEPTABLE_ROLE_GUIDS: frozenset[str] = frozenset(
    {
        _ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR,
        _ROLE_STORAGE_BLOB_DATA_OWNER,
        _ROLE_CONTRIBUTOR,
        _ROLE_OWNER,
    }
)

# Module-level slot tests can overwrite with `monkeypatch.setattr(...)`.
# Default ``None`` triggers the lightweight inline resolver inside
# :func:`kubelet_storage_blob_data_access`. The inline path uses
# ``api.services.azure_clients.aks_client`` (already on the import
# graph for every service module) so we do not pull in the heavy
# ``api.tasks.azure`` package just for a single ARM read on every
# prepare-db request — charter §7 lazy-task-loading.
_resolve_kubelet_oid: Any | None = None


def _inline_resolve_kubelet_oid(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> str | None:
    """Lightweight kubelet identity probe.

    Mirrors ``api.tasks.azure.rbac._resolve_kubelet_oid`` but lives in
    the services layer so the pre-flight does not need to import
    ``api.tasks.*`` (which transitively loads the full Celery task
    graph). Returns ``None`` when the cluster has no kubelet identity
    (older SP-mode clusters); callers treat that as ``no_kubelet``.
    """
    from api.services.azure_clients import aks_client

    aks = aks_client(credential, subscription_id)
    cluster = aks.managed_clusters.get(resource_group, cluster_name)
    profile = getattr(cluster, "identity_profile", None) or {}
    kubelet = profile.get("kubeletidentity") if isinstance(profile, dict) else None
    if kubelet is None:
        return None
    oid = getattr(kubelet, "object_id", None)
    if oid is None and isinstance(kubelet, dict):
        oid = kubelet.get("object_id")
    return oid if oid else None


@dataclass
class KubeletStorageAccessResult:
    """Outcome of the pre-flight probe.

    ``status`` is one of:
      * ``"ok"`` — grant present; caller can proceed.
      * ``"missing"`` — probe succeeded and confirmed no qualifying role.
      * ``"probe_failed"`` — ARM lookup raised; treat as "don't know" so
        the caller can fall through (azcopy login will surface the real
        error if the grant is missing).
      * ``"no_kubelet"`` — cluster has no kubelet identity profile; the
        AKS path cannot work and the caller should refuse explicitly.
    """

    status: str
    kubelet_object_id: str = ""
    matched_role_guid: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def should_block(self) -> bool:
        return self.status in {"missing", "no_kubelet"}


def kubelet_storage_blob_data_access(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    storage_resource_group: str,
    storage_account: str,
) -> KubeletStorageAccessResult:
    """Probe whether the AKS kubelet identity has a qualifying role.

    Returns ``KubeletStorageAccessResult`` so callers can distinguish
    "probe failed; fall through" from "confirmed missing; refuse with a
    clear error". Never raises.
    """
    # Lazy import keeps this module decoupled from the heavy `api.tasks`
    # package at import time; reassigning the module attribute (e.g.
    # ``monkeypatch.setattr(prepare_db_preflight, "_resolve_kubelet_oid",
    # ...)``) lets tests override the call without touching the rbac
    # facade contract.
    resolver = _resolve_kubelet_oid or _inline_resolve_kubelet_oid

    try:
        kubelet_oid = resolver(
            credential, subscription_id, resource_group, cluster_name
        )
    except Exception as exc:
        LOGGER.info(
            "prepare-db preflight: kubelet identity lookup failed (%s)",
            type(exc).__name__,
        )
        return KubeletStorageAccessResult(
            status="probe_failed", reason=f"kubelet_lookup: {type(exc).__name__}"
        )
    if not kubelet_oid:
        return KubeletStorageAccessResult(status="no_kubelet")

    try:
        from azure.mgmt.authorization import AuthorizationManagementClient

        from api.services.azure_clients import storage_client

        storage = storage_client(credential, subscription_id).storage_accounts.get_properties(
            storage_resource_group,
            storage_account,
        )
        storage_scope = storage.id
        auth = AuthorizationManagementClient(credential, subscription_id)
        # ``list_for_scope`` returns inherited assignments too, so a
        # subscription-scope grant is honoured.
        filter_expr = f"principalId eq '{kubelet_oid}'"
        assignments: list[Any] = list(
            auth.role_assignments.list_for_scope(
                scope=storage_scope, filter=filter_expr
            )
        )
    except Exception as exc:
        LOGGER.info(
            "prepare-db preflight: role assignment list failed (%s)",
            type(exc).__name__,
        )
        return KubeletStorageAccessResult(
            status="probe_failed",
            kubelet_object_id=kubelet_oid,
            reason=f"role_assignments_list: {type(exc).__name__}",
        )

    for assignment in assignments:
        role_def_id = str(
            getattr(assignment, "role_definition_id", "")
            or (assignment.get("role_definition_id") if isinstance(assignment, dict) else "")
            or ""
        )
        # Role Definition id format:
        # `/subscriptions/<sub>/providers/Microsoft.Authorization/roleDefinitions/<GUID>`
        guid = role_def_id.rsplit("/", 1)[-1].lower()
        if guid in _ACCEPTABLE_ROLE_GUIDS:
            return KubeletStorageAccessResult(
                status="ok",
                kubelet_object_id=kubelet_oid,
                matched_role_guid=guid,
            )

    return KubeletStorageAccessResult(
        status="missing",
        kubelet_object_id=kubelet_oid,
        reason="no matching role assignment found at storage scope",
    )


__all__ = [
    "KubeletStorageAccessResult",
    "kubelet_storage_blob_data_access",
]
