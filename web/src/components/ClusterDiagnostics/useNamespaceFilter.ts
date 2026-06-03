/**
 * Client-side namespace filtering for the cluster Workloads tabs.
 *
 * Single-responsibility: derive the namespace option list and the filtered
 * slice from a workload snapshot. Shared by the Pods / Deployments / Jobs
 * panels so they all behave identically (default "all", auto-reset when the
 * selected namespace drains). No React rendering, no I/O.
 */
import { useMemo, useState } from "react";

export interface NamespaceFilterState<T> {
  nsFilter: string;
  setNsFilter: (value: string) => void;
  /** "all" or a namespace that currently has at least one item. */
  effectiveNs: string;
  /** Unique, sorted namespaces present in `items`. */
  namespaces: string[];
  /** `items` filtered to `effectiveNs` ("all" passes everything through). */
  filtered: T[];
}

export function useNamespaceFilter<T extends { namespace: string }>(
  items: T[],
): NamespaceFilterState<T> {
  const [nsFilter, setNsFilter] = useState<string>("all");
  const namespaces = useMemo(
    () => Array.from(new Set(items.map((i) => i.namespace))).sort(),
    [items],
  );
  // Reset a stale filter if the selected namespace no longer has items.
  const effectiveNs =
    nsFilter !== "all" && !namespaces.includes(nsFilter) ? "all" : nsFilter;
  const filtered = useMemo(
    () =>
      effectiveNs === "all"
        ? items
        : items.filter((i) => i.namespace === effectiveNs),
    [items, effectiveNs],
  );
  return { nsFilter, setNsFilter, effectiveNs, namespaces, filtered };
}
