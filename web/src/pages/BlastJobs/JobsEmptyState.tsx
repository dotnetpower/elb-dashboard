import { Link } from "react-router-dom";

import { DegradedNotice } from "@/components/DegradedNotice";

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
      <p className="muted">No BLAST searches yet.</p>
      <p
        className="muted"
        style={{ maxWidth: 520, margin: "var(--space-2) auto 0", fontSize: 13 }}
      >
        Your searches run on your own AKS cluster and storage — never on a
        shared NCBI queue, so there is no public rate limit or wait line.
      </p>
      {degradedNotice && (
        <div
          style={{
            margin: "var(--space-3) auto var(--space-4)",
            maxWidth: 520,
            textAlign: "left",
          }}
        >
          {/* D2: shared degraded notice keeps copy + recovery hints consistent
              across cards (Jobs / Storage / Sidecars / Analytics). */}
          <DegradedNotice
            reason={degradedNotice.reason}
            message={degradedNotice.message}
            scope="Job listing"
          />
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
        {search ? `No searches matching "${search}"` : `No ${filter} searches.`}
      </p>
    </section>
  );
}
