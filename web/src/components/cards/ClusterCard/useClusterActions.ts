import { useEffect, useState } from "react";
import type { UseQueryResult } from "@tanstack/react-query";

import { aksApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import type { AksClusterSummary } from "@/api/endpoints";
import { DB_CATALOG } from "@/components/cards/storageDbCatalog";
import { readAutoWarmupDbs } from "@/components/cards/storage/autoWarmupPrefs";

type ClustersQueryData = { clusters: AksClusterSummary[] };

/**
 * Owns start / stop / delete handlers and the in-flight `transitioning`
 * Map that drives the per-cluster animated chip + faster polling. Clears
 * a transition the moment the actual `power_state` reaches the expected
 * target (e.g. start → Running, stop → Stopped).
 *
 * Auto-dismisses any action error after 8 s.
 */
export function useClusterActions(args: {
  subscriptionId: string;
  resourceGroup: string;
  query: UseQueryResult<ClustersQueryData>;
  storageAccount?: string;
  storageResourceGroup?: string;
  acrResourceGroup?: string;
  acrName?: string;
  region?: string;
  terminalResourceGroup?: string;
  terminalVmName?: string;
}) {
  const {
    subscriptionId,
    resourceGroup,
    query,
    storageAccount,
    storageResourceGroup,
    acrResourceGroup,
    acrName,
    region,
    terminalResourceGroup,
    terminalVmName,
  } = args;

  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [transitioning, setTransitioning] = useState<
    Map<string, "starting" | "stopping">
  >(new Map());
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const handleDelete = async (name: string) => {
    setActionError(null);
    setActionLoading(`delete-${name}`);
    try {
      await aksApi.delete(subscriptionId, resourceGroup, name);
      query.refetch();
    } catch (e) {
      setActionError(`Delete failed: ${formatApiError(e, "aks")}`);
    } finally {
      setDeleteTarget(null);
      setActionLoading(null);
    }
  };

  const handleStartStop = async (name: string, action: "start" | "stop") => {
    setActionError(null);
    setActionLoading(`${action}-${name}`);
    try {
      if (action === "start") {
        const cluster = query.data?.clusters.find((item) => item.name === name);
        const databases = [...readAutoWarmupDbs()];
        const programs = Object.fromEntries(
          databases.map((dbName) => {
            const catalog = DB_CATALOG.find((item) => item.value === dbName);
            return [dbName, catalog?.type === "prot" ? "blastp" : "blastn"];
          }),
        );
        await aksApi.start(subscriptionId, resourceGroup, name, {
          subscription_id: subscriptionId,
          resource_group: resourceGroup,
          cluster_name: name,
          storage_account: storageAccount || "",
          storage_resource_group: storageResourceGroup || resourceGroup,
          region: region || cluster?.region || "",
          databases,
          programs,
          enabled: databases.length > 0 && Boolean(storageAccount),
          acr_resource_group: acrResourceGroup,
          acr_name: acrName,
          terminal_resource_group: terminalResourceGroup,
          terminal_vm_name: terminalVmName,
          machine_type: cluster?.node_sku || undefined,
          num_nodes: cluster?.node_count || undefined,
        });
      } else {
        await aksApi.stop(subscriptionId, resourceGroup, name);
      }
      setTransitioning((prev) =>
        new Map(prev).set(name, action === "start" ? "starting" : "stopping"),
      );
    } catch (e) {
      setActionError(`${action} failed: ${formatApiError(e, "aks")}`);
    } finally {
      setActionLoading(null);
    }
  };

  // Clear transition state when actual power_state reaches target.
  useEffect(() => {
    if (transitioning.size === 0 || !query.data?.clusters) return;
    const next = new Map(transitioning);
    let changed = false;
    for (const [name, expected] of transitioning) {
      const cluster = query.data.clusters.find((c) => c.name === name);
      if (!cluster) {
        next.delete(name);
        changed = true;
        continue;
      }
      const reached =
        expected === "starting"
          ? cluster.power_state === "Running"
          : cluster.power_state === "Stopped";
      if (reached) {
        next.delete(name);
        changed = true;
      }
    }
    if (changed) setTransitioning(next);
  }, [query.data, transitioning]);

  // Poll faster (10s) while clusters are transitioning.
  const isTransitioning = transitioning.size > 0;
  useEffect(() => {
    if (!isTransitioning) return;
    const t = setInterval(() => query.refetch(), 10_000);
    return () => clearInterval(t);
  }, [isTransitioning, query]);

  // Auto-dismiss actionError after 8s.
  useEffect(() => {
    if (!actionError) return;
    const t = setTimeout(() => setActionError(null), 8_000);
    return () => clearTimeout(t);
  }, [actionError]);

  return {
    actionLoading,
    actionError,
    setActionError,
    transitioning,
    handleStartStop,
    handleDelete,
    deleteTarget,
    setDeleteTarget,
  };
}
