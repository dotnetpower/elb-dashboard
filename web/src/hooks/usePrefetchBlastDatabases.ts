/**
 * usePrefetchBlastDatabases — warm the React Query cache for the New Search
 * database listing while the user is still on the Dashboard (or hovering the
 * "New Search" nav link).
 *
 * `GET /api/blast/databases` enumerates the whole `blast-db` Storage container
 * and reads per-DB metadata blobs, so on a workspace with many databases the
 * New Search "Choose Search Set" step shows a loading skeleton for a noticeable
 * beat on first open. Firing the same query with the same key the page uses
 * lets New Search pick the result straight from cache when it mounts.
 *
 * The query key intentionally omits cluster topology (`numNodes` / `machineType`
 * are sent as `0` / `""`). That matches the key `useDbWithWarmupPlan` uses on
 * first render, before the cluster picker has resolved — so the page's initial
 * mount is a cache hit. The backend computes `warmup_plan` per request on top of
 * a cached base listing, so the eventual cluster-scoped refetch is cheap (it
 * hits the backend catalogue cache, not Storage) and never re-pays the
 * enumeration.
 *
 * Any failure is swallowed silently — the page retries with its own UX.
 */
import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { blastApi } from "@/api/endpoints";

export interface PrefetchBlastDatabasesInput {
  /** Active subscription. Empty string skips the prefetch. */
  subscriptionId: string;
  /** Workload Storage account hosting the `blast-db` container. */
  storageAccount: string;
  /** Workload resource group (the route validates it before any Storage call). */
  workloadResourceGroup: string;
}

interface PrefetchClient {
  prefetchQuery: (options: {
    queryKey: unknown[];
    queryFn: () => unknown;
    staleTime?: number;
    retry?: number;
  }) => Promise<unknown>;
}

/** Stale window for the prefetched entry. Must be ≥ the page query's staleTime
 *  so the page treats the prefetched data as fresh on mount and skips a refetch. */
export const BLAST_DATABASES_PREFETCH_STALE_MS = 120_000;

/**
 * Fire the topology-free database listing prefetch. Exported separately from
 * the hook so event handlers (e.g. a nav-link hover) can call it imperatively
 * with a `QueryClient` instance.
 */
export function prefetchBlastDatabasesQuery(
  qc: PrefetchClient,
  cfg: PrefetchBlastDatabasesInput,
): Promise<unknown> {
  const { subscriptionId, storageAccount, workloadResourceGroup } = cfg;
  if (!subscriptionId || !storageAccount || !workloadResourceGroup) {
    return Promise.resolve();
  }
  return qc.prefetchQuery({
    queryKey: ["blast-databases", subscriptionId, storageAccount, 0, ""],
    queryFn: () =>
      blastApi.listDatabases(subscriptionId, storageAccount, workloadResourceGroup),
    staleTime: BLAST_DATABASES_PREFETCH_STALE_MS,
    retry: 1,
  });
}

/**
 * Dashboard-side hook: prefetch the database listing once the workspace config
 * is known. Re-fires only when the identifying config fields change.
 */
export function usePrefetchBlastDatabases(cfg: PrefetchBlastDatabasesInput): void {
  const qc = useQueryClient();
  const { subscriptionId, storageAccount, workloadResourceGroup } = cfg;
  useEffect(() => {
    void prefetchBlastDatabasesQuery(qc, {
      subscriptionId,
      storageAccount,
      workloadResourceGroup,
    });
  }, [qc, subscriptionId, storageAccount, workloadResourceGroup]);
}
