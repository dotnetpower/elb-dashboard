import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";

import { blastApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import { MonitorCard } from "@/components/MonitorCard";
import { statusColor } from "@/constants";

const TERMINAL_PHASES = ["completed", "failed", "submit_failed", "error", "deleted"];
const MAX_DASHBOARD_JOBS = 5;

export function JobCard() {
  const query = useQuery({
    queryKey: ["blast-jobs"],
    queryFn: () => blastApi.listJobs(),
    refetchInterval: 30_000,
  });

  const jobs = useMemo(() => query.data?.jobs ?? [], [query.data?.jobs]);
  const running = jobs.filter(
    (j) => !TERMINAL_PHASES.includes(j.phase || j.status),
  ).length;

  // Show running jobs first, then most recent, capped at MAX_DASHBOARD_JOBS
  const displayed = useMemo(() => {
    const reversed = [...jobs].reverse();
    const active = reversed.filter((j) => !TERMINAL_PHASES.includes(j.phase || j.status));
    const done = reversed.filter((j) => TERMINAL_PHASES.includes(j.phase || j.status));
    return [...active, ...done].slice(0, MAX_DASHBOARD_JOBS);
  }, [jobs]);

  const hasMore = jobs.length > MAX_DASHBOARD_JOBS;

  const status = query.isLoading ? "loading" : query.isError ? "error" : "ok";

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
        <Link
          to="/blast/submit"
          className="glass-button glass-button--primary"
          style={{ textDecoration: "none", fontSize: 12 }}
        >
          New search
        </Link>
      }
    >
      {query.isError && (
        <div className="muted" style={{ color: "var(--danger)" }}>
          Failed to load jobs: {formatApiError(query.error, "blast")}
        </div>
      )}
      {query.isLoading && <div className="muted">Loading jobs...</div>}
      {!query.isLoading && jobs.length === 0 && !query.isError && (
        <div className="muted">No jobs yet.</div>
      )}
      {displayed.length > 0 && (
        <ul
          style={{
            padding: 0,
            margin: 0,
            listStyle: "none",
            display: "flex",
            flexDirection: "column",
            gap: "var(--space-2)",
          }}
        >
          {displayed.map((job) => {
            const phase = job.phase || job.status;
            const color = statusColor(phase);
            return (
              <li key={job.job_id}>
                <Link
                  to={`/blast/jobs/${encodeURIComponent(job.job_id)}`}
                  className="glass-card"
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "var(--space-3)",
                    padding: "var(--space-2) var(--space-3)",
                    textDecoration: "none",
                    color: "inherit",
                  }}
                >
                  <span
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: 999,
                      background: color,
                      flexShrink: 0,
                    }}
                  />
                  <span
                    style={{
                      flex: 1,
                      fontSize: 13,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {job.job_title ||
                      `${job.program ?? ""} · ${(job.db ?? "").split("/").pop() ?? job.job_id}`}
                  </span>
                  {job.job_title && (
                    <span className="muted" style={{ fontSize: 10, flexShrink: 0 }}>
                      {job.job_id.slice(0, 12)}
                    </span>
                  )}
                  <span
                    className="muted"
                    style={{
                      fontSize: 11,
                      textTransform: "uppercase",
                      letterSpacing: "0.04em",
                    }}
                  >
                    {phase}
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}

      {hasMore && (
        <Link
          to="/blast/jobs"
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 4,
            marginTop: "var(--space-2)",
            fontSize: 11,
            color: "var(--accent)",
            textDecoration: "none",
          }}
        >
          View all {jobs.length} jobs <ArrowRight size={12} />
        </Link>
      )}
    </MonitorCard>
  );
}
