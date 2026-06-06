"""Security-posture rule catalog (WAF Security pillar).

Resource-hardening checks for the AKS cluster, Storage account, and ACR registry
against the Azure Well-Architected Framework Security pillar and the Azure
security baselines. Distinct from the "Identity and Security" access-review view
(which answers "what are MY roles"); this answers "is the resource hardened".

Responsibility: Map the rich AKS / Storage / ACR detail snapshots to Security
    findings via the declarative spec framework plus a few two-field predicates.
Edit boundaries: Pure functions + spec data. No Azure SDK, no fetch.
Key entry points: `evaluate_security`.
Risky contracts: Permission-denied / unavailable snapshots MUST yield
    `indeterminate`, never `critical`. `publicNetworkAccess=Disabled` on Storage
    is the charter contract → compliant (ok), not a finding.
Validation: `uv run pytest -q api/tests/test_diagnostics_security_rules.py`.
"""

from __future__ import annotations

from typing import Any

from api.services.diagnostics.models import Finding, ResourceSnapshot
from api.services.diagnostics.rules.common import indeterminate_for, short_name
from api.services.diagnostics.rules.specs import (
    RuleSpec,
    evaluate_specs,
    set_and_not,
    want_false,
    want_true,
)

_CATEGORY = "security"
_PILLAR = "Security"

_DOC_AKS_SEC = "https://learn.microsoft.com/azure/aks/concepts-security"
_DOC_AKS_AAD = "https://learn.microsoft.com/azure/aks/managed-azure-ad"
_DOC_AKS_PRIVATE = "https://learn.microsoft.com/azure/aks/private-clusters"
_DOC_AKS_POLICY = "https://learn.microsoft.com/azure/aks/use-azure-policy"
_DOC_AKS_DEFENDER = (
    "https://learn.microsoft.com/azure/defender-for-cloud/defender-for-containers-introduction"
)
_DOC_AKS_WI = "https://learn.microsoft.com/azure/aks/workload-identity-overview"
_DOC_STORAGE_SEC = "https://learn.microsoft.com/azure/storage/blobs/security-recommendations"
_DOC_STORAGE_TLS = "https://learn.microsoft.com/azure/storage/common/transport-layer-security-configure-minimum-version"
_DOC_STORAGE_NET = "https://learn.microsoft.com/azure/storage/common/storage-network-security"
_DOC_ACR_SEC = (
    "https://learn.microsoft.com/azure/container-registry/container-registry-best-practices"
)


# --------------------------------------------------------------------------- AKS specs

_AKS_SECURITY_SPECS: list[RuleSpec] = [
    RuleSpec(
        id="aks.aad_managed",
        resource_kind="aks",
        pillar=_PILLAR,
        field="aad_managed",
        title_ok="Cluster uses Microsoft Entra integration",
        title_bad="Cluster is not integrated with Microsoft Entra ID",
        detail_ok="AKS-managed Microsoft Entra integration is enabled.",
        detail_bad="The cluster authenticates without managed Microsoft Entra integration.",
        recommendation="Enable AKS-managed Microsoft Entra integration to centralise identity.",
        doc_url=_DOC_AKS_AAD,
        compliant=want_true,
    ),
    RuleSpec(
        id="aks.azure_rbac",
        resource_kind="aks",
        pillar=_PILLAR,
        field="azure_rbac",
        title_ok="Azure RBAC for Kubernetes authorization is enabled",
        title_bad="Azure RBAC for Kubernetes authorization is disabled",
        detail_ok="Cluster authorization is managed through Azure RBAC.",
        detail_bad="The cluster does not use Azure RBAC for Kubernetes authorization.",
        recommendation="Enable Azure RBAC for Kubernetes to manage access with Entra identities.",
        doc_url=_DOC_AKS_AAD,
        compliant=want_true,
    ),
    RuleSpec(
        id="aks.local_accounts_disabled",
        resource_kind="aks",
        pillar=_PILLAR,
        field="disable_local_accounts",
        title_ok="Local Kubernetes accounts are disabled",
        title_bad="Local Kubernetes accounts are enabled",
        detail_ok="Static cluster-admin credentials are disabled; access flows through Entra.",
        detail_bad="Local accounts bypass Entra ID and are a credential-theft risk.",
        recommendation="Disable local accounts so all access is through Microsoft Entra ID.",
        doc_url=_DOC_AKS_AAD,
        bad_severity="warning",
        compliant=want_true,
    ),
    RuleSpec(
        id="aks.network_policy",
        resource_kind="aks",
        pillar=_PILLAR,
        field="network_policy",
        title_ok="A Kubernetes network policy engine is configured",
        title_bad="No Kubernetes network policy engine is configured",
        detail_ok="Pod-to-pod traffic can be controlled with network policies.",
        detail_bad="Without a network policy engine, pod-to-pod traffic is unrestricted.",
        recommendation="Enable Azure or Calico network policy to segment pod traffic.",
        doc_url=_DOC_AKS_SEC,
        compliant=set_and_not("none"),
    ),
    RuleSpec(
        id="aks.azure_policy_addon",
        resource_kind="aks",
        pillar=_PILLAR,
        field="addon_azure_policy",
        title_ok="Azure Policy add-on is enabled",
        title_bad="Azure Policy add-on is not enabled",
        detail_ok="Cluster and pod guardrails can be enforced centrally with Azure Policy.",
        detail_bad="Without the Azure Policy add-on, cluster guardrails are not enforced.",
        recommendation="Enable the Azure Policy add-on for at-scale cluster governance.",
        doc_url=_DOC_AKS_POLICY,
        compliant=want_true,
    ),
    RuleSpec(
        id="aks.defender",
        resource_kind="aks",
        pillar=_PILLAR,
        field="defender_enabled",
        title_ok="Microsoft Defender for Containers is enabled",
        title_bad="Microsoft Defender for Containers is not enabled",
        detail_ok="Runtime threat detection is active for the cluster.",
        detail_bad="The cluster has no runtime threat detection from Defender for Containers.",
        recommendation="Enable Microsoft Defender for Containers for threat detection.",
        doc_url=_DOC_AKS_DEFENDER,
        compliant=want_true,
    ),
    RuleSpec(
        id="aks.workload_identity",
        resource_kind="aks",
        pillar=_PILLAR,
        field="workload_identity",
        title_ok="Microsoft Entra Workload ID is enabled",
        title_bad="Microsoft Entra Workload ID is not enabled",
        detail_ok="Workloads can access Azure resources without stored credentials.",
        detail_bad="Without Workload ID, pods may rely on stored secrets to reach Azure.",
        recommendation="Enable Microsoft Entra Workload ID and the OIDC issuer.",
        doc_url=_DOC_AKS_WI,
        compliant=want_true,
    ),
    RuleSpec(
        id="aks.oidc_issuer",
        resource_kind="aks",
        pillar=_PILLAR,
        field="oidc_issuer_enabled",
        title_ok="OIDC issuer is enabled",
        title_bad="OIDC issuer is not enabled",
        detail_ok="The cluster can federate identities for Workload ID.",
        detail_bad="Without the OIDC issuer, federated Workload ID cannot be used.",
        recommendation="Enable the OIDC issuer to support Microsoft Entra Workload ID.",
        doc_url=_DOC_AKS_WI,
        compliant=want_true,
    ),
    RuleSpec(
        id="aks.keyvault_secrets_provider",
        resource_kind="aks",
        pillar=_PILLAR,
        field="addon_keyvault_secrets",
        title_ok="The Key Vault secrets provider add-on is enabled",
        title_bad="The Key Vault secrets provider add-on is not enabled",
        detail_ok="Secrets can be mounted from Key Vault with the CSI driver.",
        detail_bad="Secrets are not sourced from Key Vault via the CSI driver.",
        recommendation="Enable the Azure Key Vault provider for Secrets Store CSI Driver.",
        doc_url=_DOC_AKS_SEC,
        compliant=want_true,
    ),
    RuleSpec(
        id="aks.identity_managed",
        resource_kind="aks",
        pillar=_PILLAR,
        field="identity_type",
        title_ok="Cluster uses a managed identity",
        title_bad="Cluster does not use a managed identity",
        detail_ok="The cluster control plane authenticates with a managed identity.",
        detail_bad="The cluster may use a service principal, which requires credential rotation.",
        recommendation="Use a managed identity for the cluster to avoid service-principal secrets.",
        doc_url=_DOC_AKS_SEC,
        compliant=lambda v: None if v is None else ("assigned" in str(v).lower()),
    ),
    RuleSpec(
        id="aks.run_command_disabled",
        resource_kind="aks",
        pillar=_PILLAR,
        field="disable_run_command",
        title_ok="The AKS run-command bypass is disabled",
        title_bad="The AKS run-command bypass is enabled",
        detail_ok="The `az aks command invoke` bypass to the API server is disabled.",
        detail_bad="run-command can reach the API server bypassing network controls.",
        recommendation="Disable run-command so cluster access honours network restrictions.",
        doc_url=_DOC_AKS_PRIVATE,
        bad_severity="info",
        compliant=want_true,
    ),
]


def _aks_api_exposure(cluster: dict[str, Any]) -> bool | None:
    """Compliant when the API server is private OR restricted by authorized IPs."""
    private = cluster.get("private_cluster")
    ranges = cluster.get("authorized_ip_ranges") or []
    if private is None and not ranges:
        # No private flag and no IP ranges reported — genuinely unknown on this
        # API version; do not fabricate a verdict.
        return None
    return bool(private) or len(ranges) > 0


_AKS_API_EXPOSURE_SPEC = RuleSpec(
    id="aks.api_server_exposure",
    resource_kind="aks",
    pillar=_PILLAR,
    field="authorized_ip_ranges",
    title_ok="The API server is private or IP-restricted",
    title_bad="The API server is publicly reachable without IP restrictions",
    detail_ok="API server access is limited (private cluster or authorized IP ranges).",
    detail_bad="A public API server with no authorized IP ranges has a large attack surface.",
    recommendation="Use a private cluster, or set API server authorized IP ranges.",
    doc_url=_DOC_AKS_PRIVATE,
    bad_severity="warning",
    compliant_resource=_aks_api_exposure,
)


# ----------------------------------------------------------------------- Storage specs

_STORAGE_SECURITY_SPECS: list[RuleSpec] = [
    RuleSpec(
        id="storage.https_only",
        resource_kind="storage",
        pillar=_PILLAR,
        field="https_only",
        title_ok="Secure transfer (HTTPS only) is required",
        title_bad="Secure transfer (HTTPS only) is not required",
        detail_ok="The account rejects plaintext HTTP requests.",
        detail_bad="The account accepts plaintext HTTP requests.",
        recommendation="Require secure transfer so all requests use HTTPS.",
        doc_url=_DOC_STORAGE_SEC,
        bad_severity="critical",
        compliant=want_true,
    ),
    RuleSpec(
        id="storage.min_tls",
        resource_kind="storage",
        pillar=_PILLAR,
        field="min_tls_version",
        title_ok="Minimum TLS version is 1.2 or higher",
        title_bad="Minimum TLS version is below 1.2",
        detail_ok="Clients must use a modern TLS version.",
        detail_bad="The account allows deprecated TLS 1.0/1.1.",
        recommendation="Set the minimum TLS version to 1.2.",
        doc_url=_DOC_STORAGE_TLS,
        compliant=lambda v: None if v is None else str(v).upper() in {"TLS1_2", "TLS1_3"},
    ),
    RuleSpec(
        id="storage.shared_key_disabled",
        resource_kind="storage",
        pillar=_PILLAR,
        field="allow_shared_key_access",
        title_ok="Shared key authorization is disabled",
        title_bad="Shared key authorization is enabled",
        detail_ok="Only Microsoft Entra authorized requests are permitted.",
        detail_bad="Account-key and SAS access remain possible, widening the attack surface.",
        recommendation="Disable shared key access and use Microsoft Entra ID authorization.",
        doc_url=_DOC_STORAGE_SEC,
        compliant=want_false,
    ),
    RuleSpec(
        id="storage.blob_public_access",
        resource_kind="storage",
        pillar=_PILLAR,
        field="allow_blob_public_access",
        title_ok="Anonymous blob public access is disabled",
        title_bad="Anonymous blob public access is allowed",
        detail_ok="Containers cannot be opened to anonymous read.",
        detail_bad="A container could be configured for anonymous public read.",
        recommendation="Disable blob public access at the account level.",
        doc_url=_DOC_STORAGE_SEC,
        bad_severity="warning",
        compliant=want_false,
    ),
    RuleSpec(
        id="storage.oauth_default",
        resource_kind="storage",
        pillar=_PILLAR,
        field="default_to_oauth",
        title_ok="Microsoft Entra (OAuth) is the default authorization",
        title_bad="Microsoft Entra (OAuth) is not the default authorization",
        detail_ok="Requests default to Entra ID authorization in the portal/tools.",
        detail_bad="The default authorization is not Microsoft Entra ID.",
        recommendation="Set default-to-OAuth so tools prefer Entra ID authorization.",
        doc_url=_DOC_STORAGE_SEC,
        bad_severity="info",
        compliant=want_true,
    ),
    RuleSpec(
        id="storage.cross_tenant_replication",
        resource_kind="storage",
        pillar=_PILLAR,
        field="cross_tenant_replication",
        title_ok="Cross-tenant object replication is disabled",
        title_bad="Cross-tenant object replication is allowed",
        detail_ok="Objects cannot be replicated to a storage account in another tenant.",
        detail_bad="Cross-tenant replication is a potential data-exfiltration path.",
        recommendation="Disable cross-tenant replication unless explicitly required.",
        doc_url=_DOC_STORAGE_SEC,
        compliant=want_false,
    ),
    RuleSpec(
        id="storage.public_network_access",
        resource_kind="storage",
        pillar=_PILLAR,
        field="public_network_access",
        title_ok="Public network access is disabled",
        title_bad="Public network access is enabled",
        detail_ok="The account is reachable only through private endpoints — the charter contract.",
        detail_bad="The account is reachable from public networks.",
        recommendation=(
            "Disable public network access and use private endpoints (see the storage contract)."
        ),
        doc_url=_DOC_STORAGE_NET,
        bad_severity="warning",
        compliant=lambda v: None if v is None else str(v).lower() == "disabled",
        expected_by_charter=True,
    ),
    RuleSpec(
        id="storage.default_network_action",
        resource_kind="storage",
        pillar=_PILLAR,
        field="default_network_action",
        title_ok="The storage firewall defaults to Deny",
        title_bad="The storage firewall defaults to Allow",
        detail_ok="Network access is deny-by-default with explicit allow rules.",
        detail_bad="A default-Allow firewall exposes the account to all networks.",
        recommendation="Set the storage firewall default action to Deny.",
        doc_url=_DOC_STORAGE_NET,
        compliant=lambda v: None if v is None else str(v).lower() == "deny",
    ),
    RuleSpec(
        id="storage.private_endpoints",
        resource_kind="storage",
        pillar=_PILLAR,
        field="private_endpoint_count",
        title_ok="The account has private endpoints",
        title_bad="The account has no private endpoints",
        detail_ok="Clients reach the account over the private network.",
        detail_bad="No private endpoint is configured for private connectivity.",
        recommendation="Add private endpoints so clients connect over the private network.",
        doc_url=_DOC_STORAGE_NET,
        bad_severity="warning",
        compliant=lambda v: None if v is None else int(v) > 0,
    ),
    RuleSpec(
        id="storage.infrastructure_encryption",
        resource_kind="storage",
        pillar=_PILLAR,
        field="infrastructure_encryption",
        title_ok="Infrastructure (double) encryption is enabled",
        title_bad="Infrastructure (double) encryption is not enabled",
        detail_ok="Data is encrypted twice at the infrastructure layer.",
        detail_bad="Only single-layer encryption is configured.",
        recommendation="Enable infrastructure encryption for defence in depth (set at creation).",
        doc_url=_DOC_STORAGE_SEC,
        bad_severity="info",
        compliant=want_true,
    ),
    RuleSpec(
        id="storage.cmk",
        resource_kind="storage",
        pillar=_PILLAR,
        field="cmk",
        title_ok="Encryption uses a customer-managed key",
        title_bad="Encryption uses a Microsoft-managed key",
        detail_ok="The account is encrypted with a customer-managed key in Key Vault.",
        detail_bad="The account uses the default Microsoft-managed key.",
        recommendation="Consider a customer-managed key for greater key control (optional).",
        doc_url=_DOC_STORAGE_SEC,
        bad_severity="info",
        compliant=want_true,
    ),
]


# --------------------------------------------------------------------------- ACR specs

_ACR_SECURITY_SPECS: list[RuleSpec] = [
    RuleSpec(
        id="acr.admin_user_disabled",
        resource_kind="acr",
        pillar=_PILLAR,
        field="admin_user_enabled",
        title_ok="The registry admin user is disabled",
        title_bad="The registry admin user is enabled",
        detail_ok="Access flows through Microsoft Entra identities, not a shared admin account.",
        detail_bad="The shared admin account is a credential-theft risk.",
        recommendation="Disable the admin user and use Microsoft Entra ID / managed identity.",
        doc_url=_DOC_ACR_SEC,
        compliant=want_false,
    ),
    RuleSpec(
        id="acr.public_network_access",
        resource_kind="acr",
        pillar=_PILLAR,
        field="public_network_access",
        title_ok="Registry public network access is disabled",
        title_bad="Registry public network access is enabled",
        detail_ok="The registry is reachable only over private endpoints.",
        detail_bad="The registry is reachable from public networks.",
        recommendation="Disable public network access and use a private endpoint (Premium SKU).",
        doc_url=_DOC_ACR_SEC,
        bad_severity="info",
        compliant=lambda v: None if v is None else str(v).lower() == "disabled",
    ),
    RuleSpec(
        id="acr.anonymous_pull_disabled",
        resource_kind="acr",
        pillar=_PILLAR,
        field="anonymous_pull_enabled",
        title_ok="Anonymous pull is disabled",
        title_bad="Anonymous pull is enabled",
        detail_ok="Image pulls require authentication.",
        detail_bad="Anyone can pull images without authentication.",
        recommendation="Disable anonymous pull so image access requires authentication.",
        doc_url=_DOC_ACR_SEC,
        compliant=want_false,
    ),
    RuleSpec(
        id="acr.quarantine_policy",
        resource_kind="acr",
        pillar=_PILLAR,
        field="quarantine_policy",
        title_ok="Image quarantine policy is enabled",
        title_bad="Image quarantine policy is not enabled",
        detail_ok="Pushed images are quarantined until scanned.",
        detail_bad="Images are immediately pullable without a quarantine gate.",
        recommendation="Enable the quarantine policy to gate unscanned images (Premium SKU).",
        doc_url=_DOC_ACR_SEC,
        bad_severity="info",
        compliant=want_true,
    ),
    RuleSpec(
        id="acr.trust_policy",
        resource_kind="acr",
        pillar=_PILLAR,
        field="trust_policy",
        title_ok="Content trust (image signing) is enabled",
        title_bad="Content trust (image signing) is not enabled",
        detail_ok="Only signed images can be pulled.",
        detail_bad="Unsigned images can be pulled without provenance.",
        recommendation="Enable content trust to require signed images (Premium SKU).",
        doc_url=_DOC_ACR_SEC,
        bad_severity="info",
        compliant=want_true,
    ),
    RuleSpec(
        id="acr.dedicated_data_endpoints",
        resource_kind="acr",
        pillar=_PILLAR,
        field="data_endpoint_enabled",
        title_ok="Dedicated data endpoints are enabled",
        title_bad="Dedicated data endpoints are not enabled",
        detail_ok="Image data is served from registry-specific endpoints, not a shared wildcard.",
        detail_bad="Image data uses shared wildcard endpoints, complicating firewall rules.",
        recommendation=(
            "Enable dedicated data endpoints for tighter egress firewall rules (Premium)."
        ),
        doc_url=_DOC_ACR_SEC,
        bad_severity="info",
        compliant=want_true,
    ),
    RuleSpec(
        id="acr.cmk",
        resource_kind="acr",
        pillar=_PILLAR,
        field="cmk_enabled",
        title_ok="Registry uses a customer-managed key",
        title_bad="Registry uses a Microsoft-managed key",
        detail_ok="The registry is encrypted with a customer-managed key.",
        detail_bad="The registry uses the default Microsoft-managed key.",
        recommendation=(
            "Consider a customer-managed key for greater key control (optional, Premium)."
        ),
        doc_url=_DOC_ACR_SEC,
        bad_severity="info",
        compliant=want_true,
    ),
]


def evaluate_security(snapshots: dict[str, ResourceSnapshot]) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_aks_security(snapshots.get("aks")))
    findings.extend(_storage_security(snapshots.get("storage")))
    findings.extend(_acr_security(snapshots.get("acr")))
    return findings


def _aks_security(snap: ResourceSnapshot | None) -> list[Finding]:
    if snap is None:
        return []
    if not snap.available:
        return [
            indeterminate_for(
                snap,
                category=_CATEGORY,
                pillar=_PILLAR,
                resource_kind="aks",
                id="aks.security",
                title="AKS security posture could not be verified",
                doc_url=_DOC_AKS_SEC,
            )
        ]
    findings: list[Finding] = []
    for cluster in snap.data.get("clusters") or []:
        name = short_name(cluster.get("name"))
        findings.extend(
            evaluate_specs(_AKS_SECURITY_SPECS, cluster, category=_CATEGORY, resource_name=name)
        )
        findings.extend(
            evaluate_specs(
                [_AKS_API_EXPOSURE_SPEC], cluster, category=_CATEGORY, resource_name=name
            )
        )
    return findings


def _storage_security(snap: ResourceSnapshot | None) -> list[Finding]:
    if snap is None:
        return []
    if not snap.available:
        return [
            indeterminate_for(
                snap,
                category=_CATEGORY,
                pillar=_PILLAR,
                resource_kind="storage",
                id="storage.security",
                title="Storage security posture could not be verified",
                doc_url=_DOC_STORAGE_SEC,
            )
        ]
    name = short_name(snap.data.get("name"))
    return evaluate_specs(
        _STORAGE_SECURITY_SPECS, snap.data, category=_CATEGORY, resource_name=name
    )


def _acr_security(snap: ResourceSnapshot | None) -> list[Finding]:
    if snap is None:
        return []
    if not snap.available:
        return [
            indeterminate_for(
                snap,
                category=_CATEGORY,
                pillar=_PILLAR,
                resource_kind="acr",
                id="acr.security",
                title="Container registry security posture could not be verified",
                doc_url=_DOC_ACR_SEC,
            )
        ]
    name = short_name(snap.data.get("name"))
    return evaluate_specs(_ACR_SECURITY_SPECS, snap.data, category=_CATEGORY, resource_name=name)
