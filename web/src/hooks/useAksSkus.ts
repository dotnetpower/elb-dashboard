import { useQuery } from "@tanstack/react-query";

import { aksApi, type AksSku, type AksAvailableSkusResponse } from "@/api/endpoints";

export const DEFAULT_AKS_SKU = "Standard_E16s_v5";
/** Mirrors sibling repo constants.py::ELB_DFLT_AZURE_SYSTEM_VM_SIZE. */
export const DEFAULT_AKS_SYSTEM_SKU = "Standard_D2s_v3";
export const DEFAULT_AKS_SYSTEM_NODE_COUNT = 2;

const FALLBACK_AKS_SKUS: AksSku[] = [
  {
    name: DEFAULT_AKS_SYSTEM_SKU,
    vCPUs: 2,
    memoryGiB: 8,
    category: "general",
    series: "D-v3",
    hourlyUsd: 0.096,
    role: "system",
    group: "system",
  },
  {
    name: DEFAULT_AKS_SKU,
    vCPUs: 16,
    memoryGiB: 128,
    category: "memory",
    series: "E-v5",
    hourlyUsd: 1.008,
    role: "blast",
    group: "memory-v5",
  },
];

/** Fallback group labels used only when the API response omits them
 *  (legacy backend or offline dev). Keys mirror SKU_GROUP_LABELS in
 *  api/services/aks_skus.py. */
const FALLBACK_GROUP_LABELS: Record<string, string> = {
  system: "System pool (D-series, 2–4 vCPU)",
  hpc: "HPC — InfiniBand (HB / HC)",
  "memory-v5": "Memory-optimised — E v5",
  "memory-bs-v5": "Memory-optimised + NVMe — E bs v5",
  "memory-v3": "Memory-optimised — E v3",
  general: "General purpose — D v3",
  "storage-v3": "Storage-optimised — L v3",
  "storage-as-v3": "Storage-optimised — L as v3",
};

const FALLBACK_GROUP_ORDER: string[] = [
  "system",
  "hpc",
  "memory-v5",
  "memory-bs-v5",
  "memory-v3",
  "general",
  "storage-v3",
  "storage-as-v3",
];

export function formatAksSkuOption(sku: AksSku): string {
  const price = sku.hourlyUsd > 0 ? ` · $${sku.hourlyUsd.toFixed(2)}/hr` : "";
  return `${sku.name} (${sku.vCPUs} vCPUs, ${sku.memoryGiB} GB)${price}`;
}

export function describeAksSku(sku: AksSku | undefined): string {
  if (!sku) return "";
  return `${sku.vCPUs} cores, ${sku.memoryGiB} GB RAM, ${sku.series} ${sku.category}`;
}

export interface AksSkuGroup {
  /** Stable id, e.g. "memory-v5". */
  id: string;
  /** Human-friendly label, e.g. "Memory-optimised — E v5". */
  label: string;
  skus: AksSku[];
}

/** Filter a SKU list to those usable by the given pool, then split into
 *  ordered <optgroup>-ready buckets. ``role`` controls which SKUs are
 *  eligible:
 *
 *  * ``system`` — only `role` = system / both
 *  * ``blast``  — only `role` = blast / both
 */
export function groupAksSkus(
  skus: AksSku[],
  pool: "system" | "blast",
  groupOrder: string[],
  groupLabels: Record<string, string>,
): AksSkuGroup[] {
  const eligible = skus.filter((s) =>
    pool === "system" ? s.role !== "blast" : s.role !== "system",
  );
  const byGroup = new Map<string, AksSku[]>();
  for (const sku of eligible) {
    const list = byGroup.get(sku.group) ?? [];
    list.push(sku);
    byGroup.set(sku.group, list);
  }
  const ordered: AksSkuGroup[] = [];
  const seen = new Set<string>();
  for (const id of groupOrder) {
    const list = byGroup.get(id);
    if (list && list.length > 0) {
      ordered.push({ id, label: groupLabels[id] ?? id, skus: list });
      seen.add(id);
    }
  }
  // Tail: any group missing from groupOrder (defensive against new groups
  // a freshly-deployed backend exposes before the SPA bundle catches up).
  for (const [id, list] of byGroup) {
    if (!seen.has(id) && list.length > 0) {
      ordered.push({ id, label: groupLabels[id] ?? id, skus: list });
    }
  }
  return ordered;
}

export function useAksSkus({ enabled = true }: { enabled?: boolean } = {}) {
  const query = useQuery({
    queryKey: ["aks-skus"],
    queryFn: () => aksApi.listSkus(),
    enabled,
    staleTime: 600_000,
  });

  const skus = query.data?.skus ?? FALLBACK_AKS_SKUS;
  const defaultSku = query.data?.default_sku ?? query.data?.default ?? DEFAULT_AKS_SKU;
  const defaultSystemSku =
    query.data?.default_system_sku ?? DEFAULT_AKS_SYSTEM_SKU;
  const defaultSystemNodeCount =
    query.data?.default_system_node_count ?? DEFAULT_AKS_SYSTEM_NODE_COUNT;
  const groupLabels = query.data?.group_labels ?? FALLBACK_GROUP_LABELS;
  const groupOrder = query.data?.group_order ?? FALLBACK_GROUP_ORDER;

  return {
    ...query,
    skus,
    defaultSku,
    defaultSystemSku,
    defaultSystemNodeCount,
    groupLabels,
    groupOrder,
  };
}

/** Per-region availability snapshot for the AKS SKU dropdown.
 *
 * The static allow-list (`useAksSkus`) is the universe of SKUs
 * `elastic-blast` understands. This hook intersects that with the live
 * Azure listing for `(subscriptionId, region)` so the dropdown can grey
 * out anything blocked by `NotAvailableForSubscription` / `QuotaId` /
 * etc. *before* the user wastes a 70 s ARM round trip discovering it
 * the hard way.
 *
 * Returns:
 * - `availableSet`: `Set<string>` of SKU names usable in the region.
 *   When `degraded` is true, the set contains every allow-list SKU
 *   (so the dropdown is permissive, not over-restrictive — better to
 *   fall through to the canonical ARM error than to hide everything).
 * - `unavailableMap`: SKU name → reason (e.g. `NotAvailableForSubscription`)
 *   so the dropdown option can render a tooltip explaining *why* a row
 *   is disabled.
 * - `isLoading`/`isFetching`: forward from the underlying query so the
 *   modal can show a "Checking availability…" hint while the user picks.
 */
export function useAksAvailableSkus(args: {
  subscriptionId: string | undefined;
  region: string | undefined;
  /** SKUs to fall back to when no region/subscription is known yet. */
  allSkus: AksSku[];
}) {
  const { subscriptionId, region, allSkus } = args;
  const enabled = Boolean(subscriptionId && region);
  const query = useQuery({
    queryKey: ["aks-available-skus", subscriptionId ?? "", region ?? ""],
    queryFn: () =>
      aksApi.availableSkus(subscriptionId ?? "", region ?? ""),
    enabled,
    // Region SKU availability changes very rarely (Azure rolls SKUs out
    // by region quarterly at most). Long stale time keeps the dropdown
    // snappy as the user opens/closes the modal.
    staleTime: 600_000,
  });

  const data: AksAvailableSkusResponse | undefined = query.data;
  // Default permissive: when we don't have data yet (or the API
  // degraded), allow every allow-listed SKU.
  const availableSet = new Set<string>(
    data && !data.degraded
      ? data.available
      : allSkus.map((s) => s.name),
  );
  const unavailableMap = new Map<string, string>();
  if (data) {
    for (const row of data.unavailable) {
      unavailableMap.set(row.name, row.reason ?? "Restricted");
    }
  }
  return {
    availableSet,
    unavailableMap,
    degraded: data?.degraded ?? false,
    isLoading: query.isLoading,
    isFetching: query.isFetching,
    refetch: query.refetch,
  };
}
