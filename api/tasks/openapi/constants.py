"""Constants and well-known identifiers for the ``elb-openapi`` AKS deploy.

Responsibility: Hold the static names that must stay aligned with the sibling
    ``elastic-blast-azure`` repo and the on-cluster manifests (MI name, K8s SA/namespace,
    federated credential name, role definition IDs).
Edit boundaries: Constants only. Any change here must also be applied to the matching
    legacy values in the sibling repo so existing clusters keep a single MI/SA pair.
Key entry points: `MI_NAME`, `K8S_SA_NAME`, `K8S_NAMESPACE`, `FED_CRED_NAME`,
    `ROLE_CONTRIBUTOR`, `ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR`, `ROLE_AKS_CLUSTER_USER`,
    `pls_config_from_env`.
Risky contracts: Renaming any constant changes the workload identity contract on
    deployed clusters — existing federated credentials and role assignments would
    appear duplicated, not migrated.
Validation: `uv run pytest -q api/tests/test_openapi_deploy.py` (if present) or the
    package smoke `uv run pytest -q api/tests/test_smoke.py`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Workload-identity / K8s naming — must match the legacy values so existing
# clusters do not see a duplicate MI / SA pair.
MI_NAME = "id-elb-openapi"
K8S_SA_NAME = "elb-openapi-sa"
K8S_NAMESPACE = "default"
FED_CRED_NAME = "fc-elb-openapi"

# Built-in role definition IDs (well-known).
ROLE_CONTRIBUTOR = "b24988ac-6180-42a0-ab88-20f7382dd24c"
ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
ROLE_AKS_CLUSTER_USER = "4abbcc35-e782-43d8-92c5-2d3f1bd2253f"


@dataclass(frozen=True)
class PlsConfig:
    """Private Link Service exposure for the ``elb-openapi`` Service.

    When ``enabled`` is True the deploy injects the AKS-provided
    ``service.beta.kubernetes.io/azure-pls-*`` annotations onto the
    LoadBalancer Service so external consumers (different subscription,
    overlapping CIDR, or three-plus VNets) can reach the API through a
    Private Link Endpoint without VNet peering. Same VNet / same tenant
    callers can still hit the ILB IP directly — PLS is additive, not a
    replacement.
    """

    enabled: bool
    name: str
    lb_subnet: str
    visibility: str
    auto_approval: str


def pls_config_from_env() -> PlsConfig:
    """Read the PLS configuration from environment variables.

    Returns a frozen :class:`PlsConfig`. When ``OPENAPI_PLS_ENABLED`` is unset
    or falsey the other fields are returned at their default values and the
    manifest builder will skip the annotations entirely.

    Raises:
        ValueError: ``OPENAPI_PLS_ENABLED`` is truthy but
            ``OPENAPI_PLS_LB_SUBNET`` is empty. PLS requires an explicit
            subnet inside the AKS LB's VNet; the controller cannot infer it,
            and silently picking the default subnet would silently expose
            the Service on the wrong network.
    """
    raw = (os.environ.get("OPENAPI_PLS_ENABLED") or "").strip().lower()
    enabled = raw in {"1", "true", "yes", "on"}
    name = (os.environ.get("OPENAPI_PLS_NAME") or "pls-elb-openapi").strip()
    lb_subnet = (os.environ.get("OPENAPI_PLS_LB_SUBNET") or "").strip()
    # Allowed visibility values map directly to the AKS annotation. ``*``
    # lets any subscription request a connection (auto-approval required).
    visibility = (os.environ.get("OPENAPI_PLS_VISIBILITY") or "*").strip() or "*"
    auto_approval = (os.environ.get("OPENAPI_PLS_AUTO_APPROVAL") or "").strip()
    if enabled and not lb_subnet:
        raise ValueError(
            "OPENAPI_PLS_ENABLED is set but OPENAPI_PLS_LB_SUBNET is empty. "
            "Private Link Service activation requires an explicit subnet name "
            "inside the AKS load-balancer VNet — see "
            "docs/operate/openapi-direct-access.md."
        )
    return PlsConfig(
        enabled=enabled,
        name=name,
        lb_subnet=lb_subnet,
        visibility=visibility,
        auto_approval=auto_approval,
    )


__all__ = (
    "FED_CRED_NAME",
    "K8S_NAMESPACE",
    "K8S_SA_NAME",
    "MI_NAME",
    "ROLE_AKS_CLUSTER_USER",
    "ROLE_CONTRIBUTOR",
    "ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR",
    "PlsConfig",
    "pls_config_from_env",
)
