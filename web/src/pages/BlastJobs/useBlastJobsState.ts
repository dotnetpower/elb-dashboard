import { useCallback, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { blastApi, type BlastJobSummary } from "@/api/endpoints";
import {
  isDashboardJobCompleted,
  isDashboardJobFailed,
  isDashboardJobQueued,
  isDashboardJobRunning,
  toJobRowView,
} from "@/components/cards/ClusterBento/jobMapping";
import { useClusterReadiness } from "@/hooks/usePrerequisites";
import { useScopedBlastJobs } from "@/hooks/useScopedBlastJobs";

import { GROUP_ORDER, getDateGroup, type DateGroup } from "./dateGroup";

export type FilterKind = "all" | "queued" | "running" | "completed" | "failed";

const FILTER_KINDS: ReadonlySet<FilterKind> = new Set([
  "all",
  "queued",
  "running",
  "completed",
  "failed",
]);

function parseFilter(raw: string | null): FilterKind {
  return raw && FILTER_KINDS.has(raw as FilterKind) ? (raw as FilterKind) : "all";
}

export function useBlastJobsState() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  // J1: persist filter/search in URL query string so refresh + deep-links keep state.
  const filter = useMemo<FilterKind>(
    () => parseFilter(searchParams.get("status")),
    [searchParams],
  );
  const search = searchParams.get("q") ?? "";
  const clusterFilter = searchParams.get("cluster") ?? "";
  const setFilter = useCallback(
    (next: FilterKind) => {
      setSearchParams(
        (prev) => {
          const params = new URLSearchParams(prev);
          if (next === "all") params.delete("status");
          else params.set("status", next);
          return params;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );
  const setSearch = useCallback(
    (next: string) => {
      setSearchParams(
        (prev) => {
          const params = new URLSearchParams(prev);
          if (next.trim() === "") params.delete("q");
          else params.set("q", next);
          return params;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );
  const cluster = useClusterReadiness();
  const { jobsQuery, clusterName } = useScopedBlastJobs({
    clusterName: clusterFilter,
    // Recent searches is a history view: list the caller's jobs across every
    // cluster (unless the URL pins one via ?cluster=). Pinning to a single
    // discovered cluster hid the user's recent jobs whenever the fleet was
    // all-Stopped and the alphabetically-first cluster was the stale one.
    autoSelectCluster: false,
    refetchInterval: 20_000,
  });

  const deleteMutation = useMutation({
    mutationFn: (jobId: string) => blastApi.deleteJob(jobId),
    onSuccess: (_data, jobId) => {
      // Drop the row from every cache that lists jobs. Both keys exist:
      //   - ["blast-jobs", ...]              → Dashboard cards, Jobs page
      // Without this, a freshly-deleted row can reappear on the next
      // poll because a cache still serves the stale list.
      queryClient.invalidateQueries({ queryKey: ["blast-jobs"] });
      // Detail caches that reference the deleted id should also be
      // dropped so a navigation back to the job hits 404 instead of
      // showing a stale "deleted" row from cache.
      queryClient.removeQueries({ queryKey: ["blast-job", jobId] });
    },
  });

  const localJobs = useMemo(() => jobsQuery.data?.jobs ?? [], [jobsQuery.data?.jobs]);
  const allJobs = useMemo(() => {
    const merged = [...localJobs];
    merged.sort(
      (a, b) =>
        new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime(),
    );
    return merged;
  }, [localJobs]);

  const degradedNotice = useMemo(() => {
    const data = jobsQuery.data as
      | {
          jobs: BlastJobSummary[];
          degraded?: boolean;
          degraded_reason?: string;
          message?: string;
        }
      | undefined;
    if (!data?.degraded) return null;
    if (allJobs.length > 0) return null;
    return {
      reason: data.degraded_reason ?? "unknown",
      message: data.message ?? "Job state storage is unavailable.",
    };
  }, [jobsQuery.data, allJobs.length]);

  const filtered = useMemo(() => {
    let list = [...allJobs];
    if (filter !== "all") {
      list = list.filter((j) => {
        if (filter === "queued") return isDashboardJobQueued(j);
        if (filter === "running") return isDashboardJobRunning(j);
        if (filter === "failed") return isDashboardJobFailed(j);
        return isDashboardJobCompleted(j);
      });
    }
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter((j) => {
        const view = toJobRowView(j);
        return (
          (j.job_title ?? "").toLowerCase().includes(q) ||
          j.job_id.toLowerCase().includes(q) ||
          (j.program ?? "").toLowerCase().includes(q) ||
          (j.db ?? "").toLowerCase().includes(q) ||
          view.title.toLowerCase().includes(q) ||
          view.query.toLowerCase().includes(q) ||
          view.db.toLowerCase().includes(q) ||
          (j.infrastructure?.cluster_name ?? "").toLowerCase().includes(q)
        );
      });
    }
    return list;
  }, [allJobs, filter, search]);

  const grouped = useMemo(() => {
    const map = new Map<DateGroup, BlastJobSummary[]>();
    for (const g of GROUP_ORDER) map.set(g, []);
    for (const job of filtered) {
      const group = job.created_at ? getDateGroup(job.created_at) : "Earlier";
      map.get(group)!.push(job);
    }
    return GROUP_ORDER.filter((g) => (map.get(g)?.length ?? 0) > 0).map((g) => ({
      label: g,
      jobs: map.get(g)!,
    }));
  }, [filtered]);

  const counts = useMemo(() => {
    const c = { queued: 0, running: 0, completed: 0, failed: 0 };
    allJobs.forEach((j) => {
      if (isDashboardJobCompleted(j)) c.completed++;
      else if (isDashboardJobFailed(j)) c.failed++;
      else if (isDashboardJobQueued(j)) c.queued++;
      else if (isDashboardJobRunning(j)) c.running++;
    });
    return c;
  }, [allJobs]);

  const handleDelete = useCallback((id: string) => setDeleteTarget(id), []);

  return {
    deleteTarget,
    setDeleteTarget,
    filter,
    setFilter,
    search,
    setSearch,
    cluster,
    clusterName,
    jobsQuery,
    deleteMutation,
    allJobs,
    degradedNotice,
    filtered,
    grouped,
    counts,
    handleDelete,
  } as const;
}

export type BlastJobsState = ReturnType<typeof useBlastJobsState>;
