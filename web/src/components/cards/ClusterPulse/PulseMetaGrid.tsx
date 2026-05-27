/**
 * PulseMetaGrid — grid of meta cells shown when the row is open.
 *
 * Live signals (CPU peak, Mem peak, DBs) are gated by `operational`
 * so stopped / transitioning clusters do not show stale or empty
 * cells. Dashboard-wide `/api/blast` p95 + 5xx live in the parent
 * ClusterCard header instead — they are NOT a per-cluster signal.
 */

import { MetaCell } from "./atoms";

interface Props {
  region: string;
  k8sVersion: string;
  nodeCountLabel: string;
  dbCountsLabel: string;
  cpuPct: number | null;
  memPct: number | null;
  metricsDegraded: boolean;
  /** False when the cluster is stopped / transitioning / failed to
   *  provision. Hides live-data cells (CPU peak, Mem peak, DBs) so
   *  the operator does not see an empty placeholder that looks like
   *  "0%" or "0 visible". */
  operational: boolean;
}

function pctTone(pct: number | null): string | undefined {
  if (pct == null) return undefined;
  if (pct >= 0.85) return "var(--danger)";
  if (pct >= 0.7) return "var(--warning)";
  return undefined;
}

export function PulseMetaGrid({
  region,
  k8sVersion,
  nodeCountLabel,
  dbCountsLabel,
  cpuPct,
  memPct,
  metricsDegraded,
  operational,
}: Props) {
  return (
    <div
      className="pulse-meta-grid"
      style={{
        padding: "7px 10px 8px 10px",
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(72px, 1fr))",
        gap: "6px 10px",
      }}
    >
      <MetaCell
        label="Region"
        value={region}
        tooltip="Azure region the cluster is provisioned in"
      />
      <MetaCell
        label="K8s"
        value={k8sVersion}
        tooltip="Kubernetes control-plane version"
      />
      <MetaCell
        label="Nodes"
        value={nodeCountLabel}
        tooltip="Ready nodes across all pools"
      />
      {operational && (
        <MetaCell
          label="DBs"
          value={dbCountsLabel}
          tooltip="Pre-baked BLAST databases reachable from this cluster"
        />
      )}
      {operational && (
        <MetaCell
          label="CPU peak"
          value={
            metricsDegraded
              ? "—"
              : cpuPct == null
                ? "—"
                : `${Math.round(cpuPct * 100)}%`
          }
          tone={pctTone(cpuPct)}
          tooltip={
            metricsDegraded
              ? "Node metrics unavailable"
              : "Highest CPU% across user-pool nodes right now"
          }
        />
      )}
      {operational && (
        <MetaCell
          label="Mem peak"
          value={
            metricsDegraded
              ? "—"
              : memPct == null
                ? "—"
                : `${Math.round(memPct * 100)}%`
          }
          tone={pctTone(memPct)}
          tooltip={
            metricsDegraded
              ? "Node metrics unavailable"
              : "Highest memory% across user-pool nodes right now"
          }
        />
      )}
    </div>
  );
}
