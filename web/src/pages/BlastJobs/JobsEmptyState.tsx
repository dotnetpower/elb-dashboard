import { Link } from "react-router-dom";
import { AlertTriangle } from "lucide-react";

import type { BlastJobsState } from "./useBlastJobsState";

export interface NoJobsEmptyProps {
  cluster: BlastJobsState["cluster"];
  degradedNotice: BlastJobsState["degradedNotice"];
}

export function NoJobsEmpty({ cluster, degradedNotice }: NoJobsEmptyProps) {
  return (
    <section
      className="glass-card jobs-empty"
      style={{ textAlign: "center", padding: "var(--space-7)" }}
    >
      <p className="muted">No BLAST jobs yet.</p>
      {degradedNotice && (
        <div
          style={{
            margin: "var(--space-3) auto var(--space-4)",
            maxWidth: 520,
            padding: "10px 14px",
            background: "rgba(240,198,116,0.08)",
            border: "1px solid rgba(240,198,116,0.25)",
            borderRadius: 8,
            fontSize: 12,
            textAlign: "left",
            color: "var(--text-primary)",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              marginBottom: 4,
            }}
          >
            <AlertTriangle size={12} style={{ color: "var(--warning)" }} />
            <strong
              style={{
                fontSize: 11,
                letterSpacing: "0.04em",
                textTransform: "uppercase",
              }}
            >
              Job listing degraded · {degradedNotice.reason}
            </strong>
          </div>
          <div className="muted" style={{ fontSize: 11, lineHeight: 1.4 }}>
            {degradedNotice.message}
          </div>
        </div>
      )}
      {cluster.hasRunningCluster || cluster.isLoading || cluster.isError ? (
        <Link
          to="/blast/submit"
          className="glass-button glass-button--primary"
          style={{ marginTop: "var(--space-4)", textDecoration: "none" }}
        >
          Submit your first search
        </Link>
      ) : (
        <div style={{ marginTop: "var(--space-4)" }}>
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
            Submit your first search
          </button>
          <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>
            {cluster.hasAnyCluster
              ? "AKS cluster is not running."
              : "No AKS cluster yet."}{" "}
            <Link to="/" style={{ color: "var(--accent)" }}>
              Go to Dashboard
            </Link>{" "}
            to provision one.
          </div>
        </div>
      )}
    </section>
  );
}

export interface NoFilteredEmptyProps {
  search: string;
  filter: string;
}

export function NoFilteredEmpty({ search, filter }: NoFilteredEmptyProps) {
  return (
    <section
      className="glass-card jobs-empty"
      style={{ textAlign: "center", padding: "var(--space-5)" }}
    >
      <p className="muted">
        {search ? `No jobs matching "${search}"` : `No ${filter} jobs.`}
      </p>
    </section>
  );
}
