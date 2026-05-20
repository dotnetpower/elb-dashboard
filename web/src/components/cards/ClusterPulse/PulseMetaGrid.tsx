/**
 * PulseMetaGrid — 4-col grid of meta cells shown when the row is open.
 */

import { MetaCell } from "./atoms";
import { fmtMs } from "./helpers";

interface Props {
  region: string;
  k8sVersion: string;
  nodeCountLabel: string;
  dbCountsLabel: string;
  cpuPct: number | null;
  memPct: number | null;
  apiP95Ms: number | null;
  apiErrors: number;
  metricsDegraded: boolean;
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
  apiP95Ms,
  apiErrors,
  metricsDegraded,
}: Props) {
  return (
    <div
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
      <MetaCell
        label="DBs"
        value={dbCountsLabel}
        tooltip="Pre-baked BLAST databases reachable from this cluster"
      />
      <MetaCell
        label="CPU peak"
        value={cpuPct == null ? "—" : `${Math.round(cpuPct * 100)}%`}
        tone={pctTone(cpuPct)}
        tooltip="Highest CPU% across user-pool nodes right now"
      />
      <MetaCell
        label="Mem peak"
        value={memPct == null ? "—" : `${Math.round(memPct * 100)}%`}
        tone={pctTone(memPct)}
        tooltip="Highest memory% across user-pool nodes right now"
      />
      <MetaCell
        label="API p95"
        value={apiP95Ms == null ? "—" : fmtMs(apiP95Ms)}
        tone={
          apiP95Ms == null
            ? undefined
            : apiP95Ms > 2000
              ? "var(--danger)"
              : apiP95Ms > 1000
                ? "var(--warning)"
                : undefined
        }
        tooltip="/api/blast latency p95 over the last 15 minutes"
      />
      <MetaCell
        label="Errors 15m"
        value={metricsDegraded ? "—" : apiErrors.toString()}
        tone={apiErrors > 0 ? "var(--danger)" : undefined}
        tooltip={
          metricsDegraded
            ? "Metrics store unavailable"
            : "/api/blast 5xx responses over the last 15 minutes"
        }
      />
    </div>
  );
}
