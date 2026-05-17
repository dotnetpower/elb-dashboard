/**
 * Detect AKS-managed (node) resource groups.
 *
 * Azure Kubernetes Service auto-creates a separate resource group to hold
 * the cluster's worker-node infrastructure (VMSS, NICs, disks, NSGs, …).
 * By default this RG is named `MC_<workloadRG>_<clusterName>_<region>` and
 * carries the `aks-managed-cluster-name` tag. Users must never select that
 * RG as a workspace because the dashboard cannot manage it independently
 * of the parent AKS resource.
 *
 * Detection priority:
 * 1. `aks-managed-cluster-name` tag (definitive — set by AKS itself).
 * 2. `MC_` name prefix (default convention; covers the case where the tag
 *    list arrives empty or is filtered out by RBAC).
 */
export function isAksManagedResourceGroup(rg: {
  name: string;
  tags?: Record<string, string>;
}): boolean {
  const tags = rg.tags ?? {};
  if (tags["aks-managed-cluster-name"]) return true;
  // Default node-RG naming convention. Users who pass --node-resource-group
  // with a non-MC_ name will fall through, but the tag check above still
  // catches them.
  if (rg.name.startsWith("MC_")) return true;
  return false;
}
