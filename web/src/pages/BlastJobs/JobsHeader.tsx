import { Link } from "react-router-dom";
import { RefreshCw, Search } from "lucide-react";

import type { BlastJobsState } from "./useBlastJobsState";

export interface JobsHeaderProps {
  allJobsLength: number;
  counts: BlastJobsState["counts"];
  cluster: BlastJobsState["cluster"];
  jobsQuery: BlastJobsState["jobsQuery"];
}

export function JobsHeader({
  allJobsLength,
  counts,
  cluster,
  jobsQuery,
}: JobsHeaderProps) {
  return (
    <header
      className="jobs-header"
      style={{
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "space-between",
        gap: 16,
        flexWrap: "wrap",
      }}
    >
      <div className="page-header" style={{ marginBottom: 0 }}>
        <div className="page-header__title">Recent BLAST searches</div>
        <div className="page-header__desc">
          {allJobsLength} total · {counts.running} running · {counts.completed}{" "}
          completed · {counts.failed} failed
        </div>
        {allJobsLength > 0 && (
          <div className="prog" style={{ width: 200, height: 6, marginTop: 6 }}>
            <div style={{ display: "flex", height: "100%" }}>
              <div
                style={{
                  width: `${(counts.completed / allJobsLength) * 100}%`,
                  background: "var(--success)",
                  borderRadius: "2px 0 0 2px",
                }}
              />
              <div
                style={{
                  width: `${(counts.running / allJobsLength) * 100}%`,
                  background: "var(--warning)",
                }}
              />
              <div
                style={{
                  width: `${(counts.failed / allJobsLength) * 100}%`,
                  background: "var(--danger)",
                  borderRadius: "0 2px 2px 0",
                }}
              />
            </div>
          </div>
        )}
      </div>
      <div style={{ display: "flex", gap: "var(--space-3)" }}>
        {cluster.hasRunningCluster || cluster.isLoading || cluster.isError ? (
          <Link
            to="/blast/submit"
            className="glass-button glass-button--primary"
            style={{ textDecoration: "none" }}
          >
            <Search size={13} strokeWidth={1.5} /> New Search
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
            style={{ cursor: "not-allowed" }}
          >
            <Search size={13} strokeWidth={1.5} /> New Search
          </button>
        )}
        <button
          className="glass-button"
          onClick={() => jobsQuery.refetch()}
          disabled={jobsQuery.isFetching}
        >
          <RefreshCw size={14} strokeWidth={1.5} /> Refresh
        </button>
      </div>
    </header>
  );
}
