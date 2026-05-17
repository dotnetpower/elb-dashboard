import { useCallback, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { blastApi, type BlastJobSummary } from "@/api/endpoints";
import { useClusterReadiness } from "@/hooks/usePrerequisites";

import {
  FAILED_PHASES,
  GROUP_ORDER,
  TERMINAL_PHASES,
  getDateGroup,
  type DateGroup,
} from "./dateGroup";

export type FilterKind = "all" | "running" | "completed" | "failed";

export function useBlastJobsState() {
  const queryClient = useQueryClient();
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterKind>("all");
  const [search, setSearch] = useState("");
  const cluster = useClusterReadiness();

  const jobsQuery = useQuery({
    queryKey: ["blast-jobs"],
    queryFn: () => blastApi.listJobs(),
    refetchInterval: 20_000,
  });

  const deleteMutation = useMutation({
    mutationFn: (jobId: string) => blastApi.deleteJob(jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["blast-jobs"] });
    },
  });

  const localJobs = useMemo(
    () => jobsQuery.data?.jobs ?? [],
    [jobsQuery.data?.jobs],
  );
  const allJobs = useMemo(() => {
    const merged = [...localJobs];
    merged.sort(
      (a, b) =>
        new Date(b.created_at || 0).getTime() -
        new Date(a.created_at || 0).getTime(),
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
        const phase = j.phase || j.status;
        if (filter === "running") return !TERMINAL_PHASES.includes(phase);
        if (filter === "failed") return FAILED_PHASES.includes(phase);
        return phase === filter;
      });
    }
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter(
        (j) =>
          (j.job_title ?? "").toLowerCase().includes(q) ||
          j.job_id.toLowerCase().includes(q) ||
          (j.program ?? "").toLowerCase().includes(q) ||
          (j.db ?? "").toLowerCase().includes(q) ||
          (j.infrastructure?.cluster_name ?? "").toLowerCase().includes(q),
      );
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
    const c = { running: 0, completed: 0, failed: 0 };
    allJobs.forEach((j) => {
      const p = j.phase || j.status;
      if (p === "completed") c.completed++;
      else if (FAILED_PHASES.includes(p)) c.failed++;
      else if (p !== "deleted") c.running++;
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
