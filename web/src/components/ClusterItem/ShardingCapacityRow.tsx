import type { AksClusterSummary } from "@/api/endpoints";

import type { ActiveSubmission } from "./useClusterActiveSubmissions";

/**
 * Sharding capacity + active-submission lines — informational, only on
 * Running clusters. Renames the row to make the static-capacity meaning
 * explicit and surfaces a follow-up runtime line so users can tell
 * "infra ceiling" apart from "is my BLAST submission done?".
 */
export function ShardingCapacityRow({
  agentPools,
  tracking,
  submissions,
}: {
  agentPools: NonNullable<AksClusterSummary["agent_pools"]>;
  tracking: boolean;
  submissions: ActiveSubmission[];
}) {
  const userPool = agentPools.find(
    (p) => (p.mode ?? "").toLowerCase() !== "system",
  );
  if (!userPool) return null;
  const nodes = userPool.enable_auto_scaling
    ? userPool.max_count ?? userPool.count ?? 0
    : userPool.count ?? 0;
  if (!nodes) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div className="dv3-shard-capacity">
        <span className="lead">Sharding capacity</span>
        <code>up to {nodes} parallel jobs</code>
        <span>·</span>
        <code>{userPool.vm_size ?? "?"}</code>
        <span
          className="muted"
          title="This is the infrastructure ceiling. elastic-blast picks the actual shard count per submit; we cap each submit at 10 jobs to keep ARM throttling out of the critical path."
        >
          · max 10 jobs per submit · static capacity
        </span>
      </div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          fontSize: 10,
          color: "var(--text-muted)",
          paddingLeft: 2,
        }}
      >
        <span
          style={{
            fontSize: 9,
            textTransform: "uppercase",
            letterSpacing: "0.06em",
          }}
        >
          Active
        </span>
        {!tracking && (
          <span title="BLAST job tracking is not configured yet (api/blast/jobs returned a degraded response). Sharding-capacity above is the static ceiling.">
            · submission tracking unavailable
          </span>
        )}
        {tracking && submissions.length === 0 && (
          <span>· no active BLAST submission</span>
        )}
        {tracking && submissions.length > 0 && (
          <span style={{ color: "var(--accent)" }}>
            · {submissions.length} submission
            {submissions.length === 1 ? "" : "s"} running
            {submissions[0].phase
              ? ` (${submissions[0].phase}${submissions.length > 1 ? ", …" : ""})`
              : ""}
          </span>
        )}
      </div>
    </div>
  );
}
