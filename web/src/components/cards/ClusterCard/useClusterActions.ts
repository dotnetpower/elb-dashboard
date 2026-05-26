import { useEffect, useState } from "react";
import type { UseQueryResult } from "@tanstack/react-query";

import { aksApi, tasksApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import type { AksClusterSummary, CeleryTaskStatus } from "@/api/endpoints";
import { DB_CATALOG } from "@/components/cards/storageDbCatalog";
import { readAutoWarmupDbs } from "@/components/cards/storage/autoWarmupPrefs";

type ClustersQueryData = { clusters: AksClusterSummary[] };

export type ClusterTransitionKind = "starting" | "stopping" | "deleting";
type PersistedTransition = {
  action: ClusterTransitionKind;
  deadline: number;
  taskId?: string;
};

// Persist the transition map so the "Cluster is starting…" banner and the
// 10 s fast-poll survive a page reload while the async Celery task is still
// running. The deadline auto-evicts a stuck entry (e.g. the start task
// failed silently) so the card never gets pinned in starting/stopping mode
// forever.
const TRANSITION_STORAGE_KEY = "elb-cluster-transitions";
const TRANSITION_TTL_MS = 10 * 60_000; // 10 min — Azure AKS start typically completes in 1–3 min.

function transitionVerb(action: ClusterTransitionKind): string {
  if (action === "starting") return "Start";
  if (action === "stopping") return "Stop";
  return "Delete";
}

function persistedKey(subscriptionId: string, resourceGroup: string): string {
  return `${TRANSITION_STORAGE_KEY}:${subscriptionId}:${resourceGroup}`;
}

function readPersistedTransitions(
  subscriptionId: string,
  resourceGroup: string,
): Map<string, ClusterTransitionKind> {
  if (typeof window === "undefined") return new Map();
  if (!subscriptionId || !resourceGroup) return new Map();
  try {
    const raw = window.localStorage.getItem(persistedKey(subscriptionId, resourceGroup));
    if (!raw) return new Map();
    const parsed = JSON.parse(raw) as Record<string, PersistedTransition>;
    const now = Date.now();
    const out = new Map<string, ClusterTransitionKind>();
    for (const [name, entry] of Object.entries(parsed)) {
      if (entry && typeof entry === "object" && entry.deadline > now) {
        if (
          entry.action === "starting" ||
          entry.action === "stopping" ||
          entry.action === "deleting"
        ) {
          out.set(name, entry.action);
        }
      }
    }
    return out;
  } catch {
    return new Map();
  }
}

function readPersistedTransitionTaskIds(
  subscriptionId: string,
  resourceGroup: string,
): Map<string, string> {
  if (typeof window === "undefined") return new Map();
  if (!subscriptionId || !resourceGroup) return new Map();
  try {
    const raw = window.localStorage.getItem(persistedKey(subscriptionId, resourceGroup));
    if (!raw) return new Map();
    const parsed = JSON.parse(raw) as Record<string, PersistedTransition>;
    const now = Date.now();
    const out = new Map<string, string>();
    for (const [name, entry] of Object.entries(parsed)) {
      if (entry?.deadline > now && entry.taskId) out.set(name, entry.taskId);
    }
    return out;
  } catch {
    return new Map();
  }
}

function readPersistedTransitionDeadlines(
  subscriptionId: string,
  resourceGroup: string,
): Map<string, number> {
  if (typeof window === "undefined") return new Map();
  if (!subscriptionId || !resourceGroup) return new Map();
  try {
    const raw = window.localStorage.getItem(persistedKey(subscriptionId, resourceGroup));
    if (!raw) return new Map();
    const parsed = JSON.parse(raw) as Record<string, PersistedTransition>;
    const now = Date.now();
    const out = new Map<string, number>();
    for (const [name, entry] of Object.entries(parsed)) {
      if (entry?.deadline > now) out.set(name, entry.deadline);
    }
    return out;
  } catch {
    return new Map();
  }
}

function writePersistedTransitions(
  subscriptionId: string,
  resourceGroup: string,
  map: Map<string, ClusterTransitionKind>,
  taskIds: Map<string, string>,
  deadlines: Map<string, number>,
): void {
  if (typeof window === "undefined") return;
  if (!subscriptionId || !resourceGroup) return;
  try {
    const key = persistedKey(subscriptionId, resourceGroup);
    if (map.size === 0) {
      window.localStorage.removeItem(key);
      return;
    }
    const payload: Record<string, PersistedTransition> = {};
    for (const [name, action] of map) {
      payload[name] = {
        action,
        deadline: deadlines.get(name) ?? Date.now() + TRANSITION_TTL_MS,
        taskId: taskIds.get(name),
      };
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
  const [actionInfo, setActionInfo] = useState<string | null>(null);
  const [transitioning, setTransitioning] = useState<Map<string, ClusterTransitionKind>>(
    () => readPersistedTransitions(subscriptionId, resourceGroup),
  );
  const [transitionTaskIds, setTransitionTaskIds] = useState<Map<string, string>>(() =>
    readPersistedTransitionTaskIds(subscriptionId, resourceGroup),
  );
  const [transitionDeadlines, setTransitionDeadlines] = useState<Map<string, number>>(
    () => readPersistedTransitionDeadlines(subscriptionId, resourceGroup),
  );
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  // Persist whenever the in-memory map changes so a reload survives.
  useEffect(() => {
    writePersistedTransitions(
      subscriptionId,
      resourceGroup,
      transitioning,
      transitionTaskIds,
      transitionDeadlines,
    );
  }, [
    subscriptionId,
    resourceGroup,
    transitioning,
    transitionTaskIds,
    transitionDeadlines,
  ]);

  // If the parent re-targets a different subscription/RG, reload the persisted
  // map for that scope instead of carrying stale entries across scopes.
  useEffect(() => {
    setTransitioning(readPersistedTransitions(subscriptionId, resourceGroup));
    setTransitionTaskIds(readPersistedTransitionTaskIds(subscriptionId, resourceGroup));
    setTransitionDeadlines(
      readPersistedTransitionDeadlines(subscriptionId, resourceGroup),
    );
  }, [subscriptionId, resourceGroup]);

  const rememberTransition = (
    name: string,
    action: ClusterTransitionKind,
    taskId?: string,
  ) => {
    setTransitioning((prev) => new Map(prev).set(name, action));
    setTransitionDeadlines((prev) =>
      new Map(prev).set(name, Date.now() + TRANSITION_TTL_MS),
    );
    setTransitionTaskIds((prev) => {
      const next = new Map(prev);
      if (taskId) next.set(name, taskId);
      else next.delete(name);
      return next;
    });
  };

  const clearTransition = (name: string) => {
    setTransitioning((prev) => {
      if (!prev.has(name)) return prev;
      const next = new Map(prev);
      next.delete(name);
      return next;
    });
    setTransitionTaskIds((prev) => {
      if (!prev.has(name)) return prev;
      const next = new Map(prev);
      next.delete(name);
      return next;
    });
    setTransitionDeadlines((prev) => {
      if (!prev.has(name)) return prev;
      const next = new Map(prev);
      next.delete(name);
      return next;
    });
  };

  const handleDelete = async (name: string) => {
    setActionError(null);
    setActionLoading(`delete-${name}`);
    try {
      // Sub-wide list: a cluster's actual RG may differ from the card's
      // anchor RG (`resourceGroup` prop). Route the delete to the row's
      // own RG, falling back to the anchor RG only if the row vanished
      // between render and click.
      const cluster = query.data?.clusters.find((item) => item.name === name);
      const targetRg = cluster?.resource_group || resourceGroup;
      const response = await aksApi.delete(subscriptionId, targetRg, name);
      rememberTransition(name, "deleting", response.task_id);
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
      // Per-row RG (see handleDelete above for the same reasoning).
      const cluster = query.data?.clusters.find((item) => item.name === name);
      const targetRg = cluster?.resource_group || resourceGroup;
      if (action === "start") {
        const databases = [...readAutoWarmupDbs()];
        const programs = Object.fromEntries(
          databases.map((dbName) => {
            const catalog = DB_CATALOG.find((item) => item.value === dbName);
            return [dbName, catalog?.type === "prot" ? "blastp" : "blastn"];
          }),
        );
        const response = await aksApi.start(subscriptionId, targetRg, name, {
          subscription_id: subscriptionId,
          resource_group: targetRg,
          cluster_name: name,
          storage_account: storageAccount || "",
          storage_resource_group: storageResourceGroup || targetRg,
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
        rememberTransition(name, "starting", response.task_id);
      } else {
        const response = await aksApi.stop(subscriptionId, targetRg, name);
        rememberTransition(name, "stopping", response.task_id);
      }
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
        setTransitionTaskIds((prev) => {
          if (!prev.has(name)) return prev;
          const ids = new Map(prev);
          ids.delete(name);
          return ids;
        });
        setTransitionDeadlines((prev) => {
          if (!prev.has(name)) return prev;
          const deadlines = new Map(prev);
          deadlines.delete(name);
          return deadlines;
        });
        continue;
      }
      const reached =
        expected === "starting"
          ? cluster.power_state === "Running"
          : expected === "stopping"
            ? cluster.power_state === "Stopped"
            : false;
      if (reached) {
        next.delete(name);
        changed = true;
        setTransitionTaskIds((prev) => {
          if (!prev.has(name)) return prev;
          const ids = new Map(prev);
          ids.delete(name);
          return ids;
        });
        setTransitionDeadlines((prev) => {
          if (!prev.has(name)) return prev;
          const deadlines = new Map(prev);
          deadlines.delete(name);
          return deadlines;
        });
      }
    }
    if (changed) setTransitioning(next);
  }, [query.data, transitioning]);

  // Poll lifecycle task status so the UI can stop saying Starting / Stopping /
  // Deleting when the worker task actually failed or was lost.
  useEffect(() => {
    if (transitioning.size === 0) return;
    let cancelled = false;
    const poll = async () => {
      const now = Date.now();
      for (const [name, action] of transitioning) {
        const deadline = transitionDeadlines.get(name);
        if (deadline && now > deadline) {
          clearTransition(name);
          setActionError(
            `${transitionVerb(action)} timed out before Azure reported completion.`,
          );
          continue;
        }
        const taskId = transitionTaskIds.get(name);
        if (!taskId) continue;
        try {
          const task = await tasksApi.status(taskId);
          if (cancelled) return;
          const status = task.status as CeleryTaskStatus;
          if (status === "FAILURE" || status === "REVOKED") {
            clearTransition(name);
            setActionError(`${transitionVerb(action)} failed: ${task.error || status}`);
            void query.refetch();
          } else if (status === "SUCCESS") {
            clearTransition(name);
            if (action === "deleting") {
              const result = task.result as
                | {
                    resource_group_status?: string;
                    resource_group?: string;
                  }
                | null
                | undefined;
              if (result?.resource_group_status === "deleted" && result.resource_group) {
                setActionInfo(
                  `Cluster ${name} deleted. Resource group ${result.resource_group} was empty and has also been removed.`,
                );
              } else if (result?.resource_group_status === "retained" && result.resource_group) {
                setActionInfo(
                  `Cluster ${name} deleted. Resource group ${result.resource_group} kept (other resources still present).`,
                );
              }
            }
            void query.refetch();
          }
        } catch {
          // A transient status read failure should not create its own false
          // failure. The transition deadline above still bounds lost tasks.
        }
      }
    };
    void poll();
    const timer = setInterval(poll, 5_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [transitioning, transitionTaskIds, transitionDeadlines, query]);

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

  // Auto-dismiss actionInfo after 8s (matches error pattern).
  useEffect(() => {
    if (!actionInfo) return;
    const t = setTimeout(() => setActionInfo(null), 8_000);
    return () => clearTimeout(t);
  }, [actionInfo]);

  return {
    actionLoading,
    actionError,
    setActionError,
    actionInfo,
    setActionInfo,
    transitioning,
    handleStartStop,
    handleDelete,
    deleteTarget,
    setDeleteTarget,
  };
}
