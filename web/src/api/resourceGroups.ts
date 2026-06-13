/**
 * Shared resource-group query coordination.
 *
 * `GET /api/arm/.../resource-groups` was being fetched twice on the Dashboard:
 * once by the `ResourcePicker` "Workload RG" dropdown (via its inline fetcher
 * under the `["arm-rgs", sub]` key) and once by `ClusterCard` under the
 * `["arm", "resource-groups", sub]` key. Same upstream ARM call, two cache
 * entries, two ~1-1.5 s round trips competing for the single api sidecar on
 * first paint.
 *
 * This module pins ONE canonical query key + staleTime for the raw RG listing
 * and exposes an imperative `fetchResourceGroups` that the picker fetchers call
 * through the shared `QueryClient`. Because every reader now resolves the same
 * key, TanStack Query dedupes concurrent first-paint requests into a single
 * in-flight fetch and serves the rest from cache. The picker fetchers still map
 * the raw rows into their own `Item[]` shape after the shared fetch resolves,
 * so their UI contract is unchanged.
 *
 * Note: the pickers keep their OWN distinct outer query key (`["arm-rgs", sub]`)
 * for the mapped `Item[]` result. Only the raw RG fetch inside their fetcher is
 * shared via the canonical key — sharing the outer key would collide a mapped
 * `Item[]` cache entry with ClusterCard's raw `ArmResourceGroup[]` entry.
 */
import type { QueryClient } from "@tanstack/react-query";

import { armProxyApi, type ArmResourceGroup } from "@/api/endpoints";

/** Canonical cache key for the raw resource-group listing of one subscription. */
export function resourceGroupsQueryKey(subscriptionId: string): readonly unknown[] {
  return ["arm", "resource-groups", subscriptionId];
}

/** Shared stale window. RGs change rarely; one minute keeps first-paint readers
 *  (picker + ClusterCard) on a single fetch without surfacing stale names. */
export const RESOURCE_GROUPS_STALE_MS = 60_000;

/**
 * Fetch (or reuse the cached) raw resource-group listing for a subscription via
 * the shared canonical key. Concurrent callers within the stale window share a
 * single in-flight ARM round trip instead of each issuing their own.
 */
export function fetchResourceGroups(
  queryClient: QueryClient,
  subscriptionId: string,
): Promise<ArmResourceGroup[]> {
  return queryClient.fetchQuery({
    queryKey: resourceGroupsQueryKey(subscriptionId),
    queryFn: () => armProxyApi.listResourceGroups(subscriptionId),
    staleTime: RESOURCE_GROUPS_STALE_MS,
  });
}
