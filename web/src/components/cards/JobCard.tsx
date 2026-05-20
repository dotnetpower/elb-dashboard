import { useMemo } from "react";
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";

import { formatApiError } from "@/api/client";
import { BlastJobIdentity } from "@/components/cards/BlastJobIdentity";
import { MonitorCard } from "@/components/MonitorCard";
import {
  compareJobsNewestFirst,
  isDashboardJobActive,
  isDashboardJobCompleted,
  isDashboardJobFailed,
  toJobRowView,
} from "@/components/cards/ClusterBento/jobMapping";
import { useClusterReadiness } from "@/hooks/usePrerequisites";
import { useAutoRefreshInterval } from "@/hooks/useAutoRefresh";
import { useScopedBlastJobs } from "@/hooks/useScopedBlastJobs";

const MAX_DASHBOARD_JOBS = 5;

export function JobCard() {
  const refetchInterval = useAutoRefreshInterval();
  const { jobsQuery: query, clusterName } = useScopedBlastJobs({ refetchInterval });
  const cluster = useClusterReadiness();

  const jobs = useMemo(() => query.data?.jobs ?? [], [query.data?.jobs]);
  const running = jobs.filter(isDashboardJobActive).length;
  const completed = jobs.filter(isDashboardJobCompleted).length;
  const failed = jobs.filter(isDashboardJobFailed).length;

  // Show running jobs first, then most recent, capped at MAX_DASHBOARD_JOBS.
  const displayed = useMemo(() => {
    const newestFirst = [...jobs].sort(compareJobsNewestFirst);
    const active = newestFirst.filter(isDashboardJobActive);
    const done = newestFirst.filter((j) => !isDashboardJobActive(j));
    return [...active, ...done].slice(0, MAX_DASHBOARD_JOBS);
  }, [jobs]);

  const hasMore = jobs.length > MAX_DASHBOARD_JOBS;
  const jobsHref = clusterName
    ? `/blast/jobs?cluster=${encodeURIComponent(clusterName)}`
    : "/blast/jobs";

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
        <div className="muted" style={{ marginTop: "var(--space-3)", fontSize: 13 }}>
          No jobs yet.
        </div>
      )}

      {displayed.length > 0 && (
        <div className="dv3-jobs-list">
          {displayed.map((job) => {
            const view = toJobRowView(job);
            const cls =
              view.state === "Completed"
                ? "completed"
                : view.state === "Failed"
                  ? "failed"
                  : view.state === "Unknown"
                    ? "idle"
                    : "running";
            return (
              <Link
                key={job.job_id}
                to={`/blast/jobs/${encodeURIComponent(job.job_id)}`}
                className="dv3-job-row"
              >
                <span className={`phase ${cls}`}>{view.state}</span>
                <BlastJobIdentity
                  className="name"
                  title={view.title}
                  fallbackTitle={job.job_id}
                  program={view.program}
                  db={view.db}
                  query={view.query}
                  clusterName={view.clusterName}
                />
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
          <Link to={jobsHref}>
            View all {jobs.length} jobs <ArrowRight size={12} />
          </Link>
        </div>
      )}
    </MonitorCard>
  );
}
