import { CheckCircle2, Loader2 } from "lucide-react";

const formatTime = (s: number) => `${Math.floor(s / 60)}m ${s % 60}s`;

/** Human label for a Celery task phase published by provision_aks. Keeps
 *  the FE friendly even though the task strings stay machine-flavoured. */
const PHASE_LABELS: Record<string, string> = {
  creating_cluster: "Preparing ARM request",
  arm_create_or_update: "Submitting cluster to Azure",
  ensuring_rbac: "Granting role assignments",
  rbac_ensure_failed_nonfatal: "RBAC partially failed (non-fatal)",
  completed: "Finishing up",
  failed: "Failing",
};

function prettifyPhase(phase: string | null | undefined): string | null {
  if (!phase) return null;
  return PHASE_LABELS[phase] ?? phase.replace(/_/g, " ");
}

/** Live "Provisioning..." banner — visible until the cluster appears in the list. */
export function ProvisioningBanner({
  clusterName,
  elapsed,
  nodeCount,
  nodeSku,
  systemNodeCount,
  systemVmSize,
  taskPhase,
}: {
  clusterName: string;
  elapsed: number;
  nodeCount: number;
  nodeSku: string;
  systemNodeCount: number;
  systemVmSize: string;
  /** Celery phase string (e.g. "arm_create_or_update"). When null the
   *  banner just shows the elapsed time — same UX as before this prop. */
  taskPhase?: string | null;
}) {
  const phase = prettifyPhase(taskPhase);
  return (
    <div
      className="glass-card"
      style={{
        padding: "12px 16px",
        marginBottom: "var(--space-3)",
        border: "1px solid rgba(110,159,255,0.25)",
        background: "rgba(110,159,255,0.04)",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Loader2 size={16} className="spin" style={{ color: "var(--accent)" }} />
          <div>
            <div
              style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}
            >
              {clusterName}
            </div>
            <div style={{ fontSize: 11, color: "var(--accent)" }}>
              {phase ? `${phase} · ` : "Provisioning... "}
              {formatTime(elapsed)}
            </div>
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
            blastpool {nodeCount} × {nodeSku}
          </div>
          <div style={{ fontSize: 10, color: "var(--text-faint)" }}>
            systempool {systemNodeCount} × {systemVmSize} · Est. 5–10 min
          </div>
        </div>
      </div>
    </div>
  );
}

export function ProvisionDoneBanner({
  clusterName,
  roleResult,
}: {
  clusterName: string;
  roleResult?: string[] | null;
}) {
  return (
    <div
      style={{
        padding: "8px 12px",
        background: "rgba(106,214,163,0.06)",
        border: "1px solid rgba(106,214,163,0.2)",
        borderRadius: 8,
        fontSize: 12,
        color: "var(--success)",
        marginBottom: "var(--space-3)",
        display: "flex",
        alignItems: "center",
        gap: 6,
      }}
    >
      <CheckCircle2 size={14} /> Cluster <strong>{clusterName}</strong> is ready.
      {roleResult && roleResult.length > 0 && (
        <span className="muted" style={{ fontSize: 11 }}>
          {" "}
          · Roles: {roleResult.join(", ")}
        </span>
      )}
    </div>
  );
}
