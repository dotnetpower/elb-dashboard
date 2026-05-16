import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";

import { blastApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import { MonitorCard } from "@/components/MonitorCard";
import { useClusterReadiness } from "@/hooks/usePrerequisites";
import { useAutoRefreshInterval } from "@/hooks/useAutoRefresh";

const TERMINAL_PHASES = ["completed", "failed", "submit_failed", "error", "deleted"];
const FAILED_PHASES = ["failed", "submit_failed", "error"];
const MAX_DASHBOARD_JOBS = 5;

/** Map a backend phase string to the dv3-job-row phase class. */
function phaseClass(raw: string | undefined): string {
  const p = (raw || "").toLowerCase();
  if (p === "completed") return "completed";
  if (FAILED_PHASES.includes(p)) return "failed";
  if (p === "deleted") return "deleted";
  if (!p) return "idle";
  return "running";
}

export function JobCard() {
  const refetchInterval = useAutoRefreshInterval();
  const query = useQuery({
    queryKey: ["blast-jobs"],
    queryFn: () => blastApi.listJobs(),
    refetchInterval,
  });
  const cluster = useClusterReadiness();

  const jobs = useMemo(() => query.data?.jobs ?? [], [query.data?.jobs]);
  const running = jobs.filter(
    (j) => !TERMINAL_PHASES.includes(j.phase || j.status),
  ).length;
  const completed = jobs.filter((j) => (j.phase || j.status) === "completed").length;
  const failed = jobs.filter((j) =>
    FAILED_PHASES.includes(j.phase || j.status),
  ).length;

  // Show running jobs first, then most recent, capped at MAX_DASHBOARD_JOBS
  const displayed = useMemo(() => {
    const reversed = [...jobs].reverse();
    const active = reversed.filter((j) => !TERMINAL_PHASES.includes(j.phase || j.status));
    const done = reversed.filter((j) => TERMINAL_PHASES.includes(j.phase || j.status));
    return [...active, ...done].slice(0, MAX_DASHBOARD_JOBS);
  }, [jobs]);

  const hasMore = jobs.length > MAX_DASHBOARD_JOBS;

  // Status:
  //   loading → first fetch
  //   error   → backend failure
  //   ok      → at least one job exists (badge gives a meaningful signal)
  //   idle    → 0 jobs (don't claim "OK" before the user has ever submitted)
  const status = query.isLoading
    ? "loading"
    : query.isError
      ? "error"
      : jobs.length > 0
        ? "ok"
        : "idle";

  return (
    <MonitorCard
      title="BLAST Jobs"
      subtitle={`${jobs.length} total · ${running} active`}
      status={status}
      fetching={query.isFetching}
      lastRefreshed={query.dataUpdatedAt ? new Date(query.dataUpdatedAt) : null}
      onRefresh={() => query.refetch()}
      accentColor="jobs"
      collapsible
      rightSlot={
        cluster.hasRunningCluster ? (
          <Link
            to="/blast/submit"
            className="glass-button glass-button--primary"
            style={{ textDecoration: "none", fontSize: 12 }}
          >
            New search
          </Link>
        ) : (
          <button
            type="button"
            className="glass-button"
            disabled
            title={
              cluster.hasAnyCluster
                ? "AKS cluster is not running — start it on the Dashboard"
                : "Provision an AKS cluster on the Dashboard first"
            }
            style={{ fontSize: 12, cursor: "not-allowed" }}
          >
            New search
          </button>
        )
      }
    >
      {query.isError && (
        <div className="muted" style={{ color: "var(--danger)" }}>
          Failed to load jobs: {formatApiError(query.error, "blast")}
        </div>
      )}
      {query.isLoading && <div className="muted">Loading jobs…</div>}

      {/* 4-cell summary strip */}
      {!query.isLoading && !query.isError && (
        <div className="dv3-cell-grid dv3-cell-grid--4">
          <div className="cell">
            <span className="label">Total</span>
            <div className="value mono">{jobs.length}</div>
          </div>
          <div className={`cell${running > 0 ? " accent" : ""}`}>
            <span className="label">Active</span>
            <div className="value mono">{running}</div>
          </div>
          <div className={`cell${completed > 0 ? " success" : ""}`}>
            <span className="label">Completed</span>
            <div className="value mono">{completed}</div>
          </div>
          <div className={`cell${failed > 0 ? " warn" : ""}`}>
            <span className="label">Failed</span>
            <div className="value mono">{failed}</div>
          </div>
        </div>
      )}

      {!query.isLoading && jobs.length === 0 && !query.isError && (
        <div
          className="muted"
          style={{ marginTop: "var(--space-3)", fontSize: 13 }}
        >
          No jobs yet.
        </div>
      )}

      {displayed.length > 0 && (
        <div className="dv3-jobs-list">
          {displayed.map((job) => {
            const phase = job.phase || job.status;
            const cls = phaseClass(phase);
            return (
              <Link
                key={job.job_id}
                to={`/blast/jobs/${encodeURIComponent(job.job_id)}`}
                className="dv3-job-row"
              >
                <span className={`phase ${cls}`}>{phase || "queued"}</span>
                <span className="name">
                  {job.job_title ||
                    `${job.program ?? ""} · ${(job.db ?? "").split("/").pop() ?? job.job_id}`}
                </span>
                <span className="meta">
                  {job.job_title ? job.job_id.slice(0, 12) : ""}
                </span>
                <span className="right">
                  <ArrowRight size={12} strokeWidth={1.75} />
                </span>
              </Link>
            );
          })}
        </div>
      )}

      {hasMore && (
        <div className="dv3-jobs-cta">
          <Link to="/blast/jobs">
            View all {jobs.length} jobs <ArrowRight size={12} />
          </Link>
        </div>
      )}
    </MonitorCard>
  );
}
