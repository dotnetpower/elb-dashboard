"""Constants and well-known identifiers for the ``elb-openapi`` AKS deploy.

Responsibility: Hold the static names that must stay aligned with the sibling
    ``elastic-blast-azure`` repo and the on-cluster manifests (MI name prefix, K8s
    SA/namespace, federated credential name, role definition IDs) and derive the
    per-cluster managed-identity name.
Edit boundaries: Constants + the ``mi_name_for_cluster`` / ``pls_config_from_env``
    derivers only. The K8s SA/namespace must match the sibling repo's manifests.
Key entry points: `MI_NAME`, `mi_name_for_cluster`, `K8S_SA_NAME`, `K8S_NAMESPACE`,
    `FED_CRED_NAME`, `ROLE_CONTRIBUTOR`, `ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR`,
    `ROLE_AKS_CLUSTER_USER`, `pls_config_from_env`.
Risky contracts: ``MI_NAME`` is a prefix, not the literal identity name — the live
    identity is ``mi_name_for_cluster`` (per-cluster). Changing the digest scheme or
    the prefix renames every cluster's identity and orphans the old MI + federated
    credential + role assignments (they appear duplicated, not migrated).
Validation: `uv run pytest -q api/tests/test_openapi_deploy.py` (if present) or the
    package smoke `uv run pytest -q api/tests/test_smoke.py`.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

# Workload-identity / K8s naming.
#
# ``MI_NAME`` is the *prefix* for the per-cluster managed identity, NOT the
# literal identity name — see ``mi_name_for_cluster``. A user-assigned MI is
# keyed in ARM by ``(resource_group, name)``, and federated credentials are
# nested under that MI. When two AKS clusters live in the same resource group
# (the ``elb-cluster-01`` / ``elb-cluster-02`` layout), a single shared
# ``id-elb-openapi`` identity would make the second cluster's deploy overwrite
# the first cluster's federated credential ``issuer`` (each cluster has a
# distinct OIDC issuer URL), silently breaking the first cluster's pods with
# 401/403 on every ARM/Storage call. Deriving the identity name per cluster
# keeps each cluster's workload identity isolated while staying deterministic
# across idempotent re-runs.
MI_NAME = "id-elb-openapi"
K8S_SA_NAME = "elb-openapi-sa"
K8S_NAMESPACE = "default"
FED_CRED_NAME = "fc-elb-openapi"

# Manifest generation stamp. ``build_manifests`` writes this as the Deployment
# annotation ``elb-dashboard/manifest-revision`` so the dashboard can detect a
# live elb-openapi Deployment whose manifest predates a change that only takes
# effect on redeploy (Bicep/azd never touch this in-cluster Deployment — it is
# applied by the "Deploy elb-openapi" task). ``get_openapi_deployment_status``
# compares the live annotation against this constant and surfaces
# ``manifest_outdated`` so the API Reference page can prompt a redeploy.
#
# Bump this by 1 whenever a manifest change in ``manifests.py`` must be
# redeployed to take effect (replica count, env, probes, PDB, tolerations, …).
# A live Deployment with a missing or lower revision is reported as outdated.
#
# History:
#   1 — implicit baseline (two replicas; pre-annotation deployments report None).
#   2 — single queue owner (replicas 1 + maxUnavailable rollout + PDB
#       maxUnavailable:1) so ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS is authoritative.
#   3 — burst resilience (issue #54): memory limit 512Mi -> 2Gi (was OOMKilled
#       under a ~50-concurrent core_nt submit burst), cpu limit 500m -> 1, and a
#       more forgiving liveness probe (timeout 5s -> 10s, failureThreshold
#       3 -> 6) so a transient load spike no longer triggers a restart loop on
#       the single-replica submit/dispatch path.
#   4 — run-concurrency 2 -> 3: admit cap default 2 -> 3 + ELB_OPENAPI_NUM_CPUS=7
#       (shard request 6 -> 5) so 3 jobs co-schedule per E16 node. The old
#       admit cap of 2 bottlenecked the sibling's intended 3-way concurrency
#       (BLAST_MAX_RUN_CONCURRENCY) down to <=2; validated live 2026-06-19
#       (3 distinct running jobs, 0 Pending).
#   5 — SB throughput Tier B (2026-06-27): admit cap default 3 -> 4. Live
#       memory model openapi_mem ~ 268 + 70 * MAX_ACTIVE MiB; at MAX=4 peak is
#       ~548 MiB on a 2 Gi limit (~73% headroom). Combined with dashboard
#       SERVICEBUS_DRAIN_CONCURRENCY=4 + SERVICEBUS_RESIDENT_CONSUMER=true, the
#       N=10 warmed E2E SLO measured p95=7.2 min (SLO <=10 min) on customer
#       dev. Below MAX=4 the resident long-poll cannot fill OpenAPI's dispatch
#       queue and throughput stays at the legacy ~2 sub/min ceiling. Above
#       MAX=4 the peak memory crosses 600 MiB which is fine on 2 Gi but the
#       BLAST shard-pod fan-out per E16 node (3-way co-schedule under
#       ELB_OPENAPI_NUM_CPUS=7) caps useful run-parallelism at 3 distinct jobs
#       so further admit-cap bumps do not raise sustained throughput.
OPENAPI_MANIFEST_REVISION = 5
OPENAPI_MANIFEST_REVISION_ANNOTATION = "elb-dashboard/manifest-revision"


def mi_name_for_cluster(subscription_id: str, cluster_name: str) -> str:
    """Return the per-cluster user-assigned managed identity name.

    Deterministic so re-running the deploy for the same cluster reuses the
    same identity (idempotent) while two clusters — even in the same resource
    group — never collide. The ``subscription_id`` + ``cluster_name`` digest
    mirrors ``dns_label_for_cluster`` so both names move together per cluster.
    The result (``id-elb-openapi-<10 hex>`` = 25 chars) stays within the
    3-128 char ARM limit for managed-identity names.
    """
    digest = hashlib.sha256(f"{subscription_id}/{cluster_name}".encode()).hexdigest()[:10]
    return f"{MI_NAME}-{digest}"

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
    "mi_name_for_cluster",
    "pls_config_from_env",
)
