import { useCallback, useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { blastApi } from "@/api/endpoints";

export type HitSortBy =
  | "relevance"
  | "evalue"
  | "bitscore"
  | "pident"
  | "qcovs"
  | "length";
export type HitSortDir = "asc" | "desc";

const DEFAULT_PAGE_SIZE = 100;
const PAGE_SIZE_OPTIONS = [10, 50, 100, 250] as const;

export interface BlastAnalyticsFilters {
  queryFilter: string;
  subjectFilter: string;
  organismFilter: string;
  minIdentity: number;
  maxIdentity: number;
  minQueryCover: number;
  maxQueryCover: number;
  maxEvalue: number;
  sortBy: HitSortBy;
  sortDir: HitSortDir;
  pageSize: number;
}

const FILTER_DEFAULTS: BlastAnalyticsFilters = {
  queryFilter: "",
  subjectFilter: "",
  organismFilter: "",
  minIdentity: 0,
  maxIdentity: 100,
  minQueryCover: 0,
  maxQueryCover: 100,
  maxEvalue: 10,
  sortBy: "relevance",
  sortDir: "asc",
  pageSize: DEFAULT_PAGE_SIZE,
};

export interface UseBlastAnalyticsStateArgs {
  jobId: string;
  subscriptionId: string;
  storageAccount: string;
  resourceGroup: string;
  /** Whether the active tab actually needs alignment data. Saves a round-trip when the user is on Files / Run details. */
  enabled?: boolean;
}

/**
 * Owns the aggregate + alignments queries, the filter state, and the
 * "applied" vs "pending" filter snapshot so we can mimic NCBI's explicit
 * [Filter] [Reset] buttons (changes don't refetch until the user clicks
 * Apply). Shared by every tab that needs hit data.
 */
export function useBlastAnalyticsState(args: UseBlastAnalyticsStateArgs) {
  const { jobId, subscriptionId, storageAccount, resourceGroup, enabled = true } = args;

  const [pending, setPending] = useState<BlastAnalyticsFilters>(FILTER_DEFAULTS);
  const [applied, setApplied] = useState<BlastAnalyticsFilters>(FILTER_DEFAULTS);
  const [page, setPage] = useState<number>(1);
  const [selectedHits, setSelectedHits] = useState<Set<string>>(new Set());

  const filtersDirty = useMemo(() => !filtersEqual(pending, applied), [pending, applied]);

  const updatePending = useCallback(
    <K extends keyof BlastAnalyticsFilters>(key: K, value: BlastAnalyticsFilters[K]) => {
      setPending((previous) => ({ ...previous, [key]: value }));
    },
    [],
  );

  const applyFilters = useCallback(() => {
    setApplied(pending);
    setPage(1);
    setSelectedHits(new Set());
  }, [pending]);

  /**
   * Apply a partial filter change *immediately*, bypassing the
   * pending-then-Apply model. Used by interactions where the user's
   * intent is "do it now": clicking a sortable column header, or
   * jumping from the Graphic Summary into the Alignments tab with the
   * clicked query already narrowed.
   *
   * Both `applied` and `pending` get the same patch through pure
   * functional updaters, so unrelated pending edits the user typed in
   * the filter bar survive (they stay pending) but the immediate
   * change (sort, query narrow) takes effect right away.
   */
  const applyImmediate = useCallback((patch: Partial<BlastAnalyticsFilters>) => {
    setApplied((previous) => ({ ...previous, ...patch }));
    setPending((previous) => ({ ...previous, ...patch }));
    setPage(1);
    setSelectedHits(new Set());
  }, []);

  const resetFilters = useCallback(() => {
    setPending(FILTER_DEFAULTS);
    setApplied(FILTER_DEFAULTS);
    setPage(1);
    setSelectedHits(new Set());
  }, []);

  const hasResources = Boolean(jobId && subscriptionId && storageAccount);

  const statsQuery = useQuery({
    queryKey: ["blast-aggregate", jobId, subscriptionId, storageAccount, resourceGroup],
    queryFn: () =>
      blastApi.resultsAggregate(jobId, subscriptionId, storageAccount, resourceGroup),
    enabled: enabled && hasResources,
    staleTime: 60_000,
  });

  const alignQuery = useQuery({
    queryKey: [
      "blast-alignments",
      jobId,
      subscriptionId,
      storageAccount,
      resourceGroup,
      ...analyticsFilterQueryKey(applied),
      page,
    ],
    queryFn: () =>
      blastApi.resultsAlignments(jobId, subscriptionId, storageAccount, resourceGroup, {
        page,
        page_size: applied.pageSize,
        query_id: applied.queryFilter || undefined,
        subject_id: applied.subjectFilter || undefined,
        organism: applied.organismFilter || undefined,
        min_identity: applied.minIdentity > 0 ? applied.minIdentity : undefined,
        min_query_cover: applied.minQueryCover > 0 ? applied.minQueryCover : undefined,
        max_evalue: applied.maxEvalue,
        sort_by: applied.sortBy,
        sort_dir: applied.sortDir,
      }),
    enabled: enabled && hasResources,
    staleTime: 60_000,
  });

  const pageCount = alignQuery.data?.pages ?? 0;
  useEffect(() => {
    if (pageCount > 0 && page > pageCount) {
      setPage(pageCount);
    }
  }, [page, pageCount]);

  // Toggle one hit in/out of the selection set (used by bulk-select bar).
  const toggleHit = useCallback((hitKey: string) => {
    setSelectedHits((previous) => {
      const next = new Set(previous);
      if (next.has(hitKey)) {
        next.delete(hitKey);
      } else {
        next.add(hitKey);
      }
      return next;
    });
  }, []);

  const setSelectionFromKeys = useCallback((keys: string[]) => {
    setSelectedHits(new Set(keys));
  }, []);

  const clearSelection = useCallback(() => {
    setSelectedHits(new Set());
  }, []);

  // Filtered/applied alignments — same hits the table renders, used by the
  // post-filter pages (Graphic Summary, Taxonomy, bulk actions) so they
  // reflect the user's current narrowing.
  const alignments = alignQuery.data?.alignments ?? [];

  return {
    statsQuery,
    alignQuery,
    alignments,
    queryIds: alignQuery.data?.query_ids ?? [],
    page,
    pageCount,
    setPage,
    pending,
    applied,
    filtersDirty,
    updatePending,
    applyFilters,
    applyImmediate,
    resetFilters,
    selectedHits,
    toggleHit,
    setSelectionFromKeys,
    clearSelection,
    pageSizeOptions: PAGE_SIZE_OPTIONS,
  } as const;
}

export type BlastAnalyticsState = ReturnType<typeof useBlastAnalyticsState>;

export function analyticsFilterQueryKey(filters: BlastAnalyticsFilters) {
  return [
    filters.queryFilter,
    filters.subjectFilter,
    filters.organismFilter,
    filters.minIdentity,
    filters.maxIdentity,
    filters.minQueryCover,
    filters.maxQueryCover,
    filters.maxEvalue,
    filters.sortBy,
    filters.sortDir,
    filters.pageSize,
  ] as const;
}

function filtersEqual(a: BlastAnalyticsFilters, b: BlastAnalyticsFilters): boolean {
  return (
    a.queryFilter === b.queryFilter &&
    a.subjectFilter === b.subjectFilter &&
    a.organismFilter === b.organismFilter &&
    a.minIdentity === b.minIdentity &&
    a.maxIdentity === b.maxIdentity &&
    a.minQueryCover === b.minQueryCover &&
    a.maxQueryCover === b.maxQueryCover &&
    a.maxEvalue === b.maxEvalue &&
    a.sortBy === b.sortBy &&
    a.sortDir === b.sortDir &&
    a.pageSize === b.pageSize
  );
}

/** Stable key for a hit row — used for selection + React list keys. */
export function hitKey(hit: {
  qseqid: string;
  sseqid: string;
  qstart: unknown;
  sstart: unknown;
}): string {
  return `${hit.qseqid}|${hit.sseqid}|${String(hit.qstart)}|${String(hit.sstart)}`;
}
