import { useEffect, useState } from "react";
import type { UseQueryResult } from "@tanstack/react-query";

import { aksApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import type { AksClusterSummary } from "@/api/endpoints";
import {
  DEFAULT_AKS_SKU,
  DEFAULT_AKS_SYSTEM_SKU,
} from "@/hooks/useAksSkus";

const DEFAULT_NODE_COUNT = 10;
const DEFAULT_SYSTEM_NODE_COUNT = 1;

export const MAX_SYSTEM_NODE_COUNT = 3;
export const CLUSTER_NAME_RE = /^[a-zA-Z][a-zA-Z0-9-]{1,62}$/;

export type ProvisionStatus = "idle" | "creating" | "done" | "error";

type ClustersQueryData = { clusters: AksClusterSummary[] };

/**
 * Owns all provision-form state + the AKS provision call. Tracks elapsed
 * seconds while creating, polls the AKS list faster while creating, and
 * flips to "done" as soon as the named cluster appears in the list.
 */
export function useClusterProvisioning(args: {
  subscriptionId: string;
  resourceGroup: string;
  region?: string;
  acrResourceGroup?: string;
  acrName?: string;
  storageResourceGroup?: string;
  storageAccount?: string;
  defaultSystemSku?: string;
  closeModal: () => void;
  query: UseQueryResult<ClustersQueryData>;
}) {
  const {
    subscriptionId,
    resourceGroup,
    region,
    acrResourceGroup,
    acrName,
    storageResourceGroup,
    storageAccount,
    defaultSystemSku,
    closeModal,
    query,
  } = args;

  const [clusterName, setClusterName] = useState("elb-cluster");
  const [nodeSku, setNodeSku] = useState(DEFAULT_AKS_SKU);
  const [nodeCount, setNodeCount] = useState(DEFAULT_NODE_COUNT);
  const [systemVmSize, setSystemVmSize] = useState(DEFAULT_AKS_SYSTEM_SKU);
  const [systemNodeCount, setSystemNodeCount] = useState(DEFAULT_SYSTEM_NODE_COUNT);
  const [provStatus, setProvStatus] = useState<ProvisionStatus>("idle");
  const [provError, setProvError] = useState<string | null>(null);
  const [provStart, setProvStart] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);

  // Adopt the backend's system-pool default the first time it loads.
  useEffect(() => {
    if (defaultSystemSku && systemVmSize === DEFAULT_AKS_SYSTEM_SKU) {
      setSystemVmSize(defaultSystemSku);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultSystemSku]);

  // Tick the elapsed counter every 1 s while creating.
  useEffect(() => {
    if (provStatus !== "creating") return;
    const timer = setInterval(
      () => setElapsed(Math.floor((Date.now() - (provStart ?? Date.now())) / 1000)),
      1000,
    );
    return () => clearInterval(timer);
  }, [provStatus, provStart]);

  // Auto-dismiss provStatus after 10 s
  useEffect(() => {
    if (provStatus !== "done") return;
    const t = setTimeout(() => setProvStatus("idle"), 10_000);
    return () => clearTimeout(t);
  }, [provStatus]);

  // While creating, poll the AKS list faster (10 s) to detect the new cluster.
  useEffect(() => {
    if (provStatus !== "creating") return;
    const t = setInterval(() => query.refetch(), 10_000);
    return () => clearInterval(t);
  }, [provStatus, query]);

  // Flip to "done" the moment the named cluster appears in the list.
  useEffect(() => {
    if (provStatus !== "creating" || !query.data?.clusters) return;
    const found = query.data.clusters.find((c) => c.name === clusterName);
    if (found) {
      setProvStatus("done");
    }
  }, [provStatus, query.data, clusterName]);

  const handleProvision = async () => {
    if (!region) return;
    setProvStatus("creating");
    setProvError(null);
    setProvStart(Date.now());
    closeModal();
    try {
      await aksApi.provision({
        subscription_id: subscriptionId,
        resource_group: resourceGroup,
        region,
        cluster_name: clusterName,
        node_sku: nodeSku,
        node_count: nodeCount,
        // Sibling repo's two-pool layout (constants.py):
        //   systempool (mode=System, CriticalAddonsOnly taint)
        //   blastpool  (mode=User, workload=blast taint)
        system_vm_size: systemVmSize,
        system_node_count: systemNodeCount,
        acr_resource_group: acrResourceGroup || "",
        acr_name: acrName || "",
        storage_resource_group: storageResourceGroup || resourceGroup,
        storage_account: storageAccount || "",
      });
    } catch (e) {
      setProvError(formatApiError(e, "aks"));
      setProvStatus("error");
    }
  };

  const clusterNameValid = CLUSTER_NAME_RE.test(clusterName);

  return {
    // form state
    clusterName,
    setClusterName,
    nodeSku,
    setNodeSku,
    nodeCount,
    setNodeCount,
    systemVmSize,
    setSystemVmSize,
    systemNodeCount,
    setSystemNodeCount,
    // status
    provStatus,
    setProvStatus,
    provError,
    setProvError,
    elapsed,
    clusterNameValid,
    handleProvision,
  };
}
