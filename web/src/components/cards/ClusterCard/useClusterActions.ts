import { useEffect, useState } from "react";
import type { UseQueryResult } from "@tanstack/react-query";

import { aksApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import type { AksClusterSummary } from "@/api/endpoints";
import { DB_CATALOG } from "@/components/cards/storageDbCatalog";
import { readAutoWarmupDbs } from "@/components/cards/storage/autoWarmupPrefs";

type ClustersQueryData = { clusters: AksClusterSummary[] };

type TransitionKind = "starting" | "stopping";
type PersistedTransition = { action: TransitionKind; deadline: number };

// Persist the transition map so the "Cluster is starting…" banner and the
// 10 s fast-poll survive a page reload while the async Celery task is still
// running. The deadline auto-evicts a stuck entry (e.g. the start task
// failed silently) so the card never gets pinned in starting/stopping mode
// forever.
const TRANSITION_STORAGE_KEY = "elb-cluster-transitions";
const TRANSITION_TTL_MS = 10 * 60_000; // 10 min — Azure AKS start typically completes in 1–3 min.

function persistedKey(subscriptionId: string, resourceGroup: string): string {
  return `${TRANSITION_STORAGE_KEY}:${subscriptionId}:${resourceGroup}`;
}

function readPersistedTransitions(
  subscriptionId: string,
  resourceGroup: string,
): Map<string, TransitionKind> {
  if (typeof window === "undefined") return new Map();
  if (!subscriptionId || !resourceGroup) return new Map();
  try {
    const raw = window.localStorage.getItem(persistedKey(subscriptionId, resourceGroup));
    if (!raw) return new Map();
    const parsed = JSON.parse(raw) as Record<string, PersistedTransition>;
    const now = Date.now();
    const out = new Map<string, TransitionKind>();
    for (const [name, entry] of Object.entries(parsed)) {
      if (entry && typeof entry === "object" && entry.deadline > now) {
        if (entry.action === "starting" || entry.action === "stopping") {
          out.set(name, entry.action);
        }
      }
    }
    return out;
  } catch {
    return new Map();
  }
}

function writePersistedTransitions(
  subscriptionId: string,
  resourceGroup: string,
  map: Map<string, TransitionKind>,
): void {
  if (typeof window === "undefined") return;
  if (!subscriptionId || !resourceGroup) return;
  try {
    const key = persistedKey(subscriptionId, resourceGroup);
    if (map.size === 0) {
      window.localStorage.removeItem(key);
      return;
    }
    const deadline = Date.now() + TRANSITION_TTL_MS;
    const payload: Record<string, PersistedTransition> = {};
    for (const [name, action] of map) {
      payload[name] = { action, deadline };
    }
    window.localStorage.setItem(key, JSON.stringify(payload));
  } catch {
    // Quota / private mode — fall back to in-memory only.
  }
}

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
    Map<string, TransitionKind>
  >(() => readPersistedTransitions(subscriptionId, resourceGroup));
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  // Persist whenever the in-memory map changes so a reload survives.
  useEffect(() => {
    writePersistedTransitions(subscriptionId, resourceGroup, transitioning);
  }, [subscriptionId, resourceGroup, transitioning]);

  // If the parent re-targets a different subscription/RG, reload the persisted
  // map for that scope instead of carrying stale entries across scopes.
  useEffect(() => {
    setTransitioning(readPersistedTransitions(subscriptionId, resourceGroup));
  }, [subscriptionId, resourceGroup]);

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
