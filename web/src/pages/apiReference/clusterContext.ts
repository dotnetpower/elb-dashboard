import type { AksClusterSummary } from "@/api/endpoints";

export interface ApiReferenceClusterContext {
  cluster: AksClusterSummary | undefined;
  clusterName: string;
  resourceGroup: string;
}

export function resolveApiReferenceClusterContext({
  clusters,
  anchorResourceGroup,
}: {
  clusters: AksClusterSummary[];
  anchorResourceGroup: string;
}): ApiReferenceClusterContext {
  const cluster = clusters[0];
  return {
    cluster,
    clusterName: cluster?.name ?? "",
    resourceGroup: cluster?.resource_group ?? anchorResourceGroup,
  };
}