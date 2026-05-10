import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { blastApi } from "@/api/endpoints";
import { MonitorCard } from "@/components/MonitorCard";
import { useRefreshCountdown } from "@/hooks/useRefreshCountdown";
import { statusColor } from "@/constants";

export function JobCard() {
  const query = useQuery({
    queryKey: ["blast-jobs"],
    queryFn: () => blastApi.listJobs(),
    refetchInterval: 30_000,
  });

  const jobs = query.data?.jobs ?? [];
  const recent = [...jobs].reverse().slice(0, 5);
  const running = jobs.filter(
    (j) => !["completed", "failed", "submit_failed", "error", "deleted"].includes(j.phase || j.status),
  ).length;

  const status = query.isLoading
    ? "loading"
    : query.isError
      ? "error"
      : "ok";

  return (
    <MonitorCard
      title="BLAST Jobs"
      subtitle={`${jobs.length} total · ${running} active`}
      status={status}
      refreshCountdown={useRefreshCountdown(query.dataUpdatedAt, 30_000)}
      refreshInterval={30_000}
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
        <div className="muted">Failed: {(query.error as Error).message}</div>
      )}
      {query.isLoading && (
        <div className="muted">Loading jobs...</div>
      )}
      {!query.isLoading && recent.length === 0 && !query.isError && (
        <div className="muted">No jobs yet.</div>
      )}
      {recent.length > 0 && (
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
          {recent.map((job) => {
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
                  <span style={{ flex: 1, fontSize: 13, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {job.job_title || job.job_id}
                  </span>
                  <span
                    className="muted"
                    style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.04em" }}
                  >
                    {phase}
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
      {jobs.length > 5 && (
        <Link
          to="/blast/jobs"
          className="muted"
          style={{ display: "block", marginTop: "var(--space-3)", fontSize: 12 }}
        >
          View all {jobs.length} jobs
        </Link>
      )}
    </MonitorCard>
  );
}
